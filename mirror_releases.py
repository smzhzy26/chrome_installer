"""从来源仓库把指定版本的 Release（含资产）镜像到本仓库。

用途：历史主版本的 Release 被误删后，可从来源仓库原样恢复，
资产按来源流式下载后重新上传，不写入 git 历史。

用法：
  # 只列出计划，不做任何写操作（公共仓库无需令牌）
  python mirror_releases.py --spec missing --spec 139 --dry-run
  # 真正执行（需要 GH_TOKEN / GITHUB_TOKEN，且对目标仓库有写权限）
  python mirror_releases.py --spec missing

--spec 支持：
  missing          所有「目标有缺失或不完整」的主版本最大号
  <tag>            精确版本号，例如 149.0.7827.201
  <major> / <major>.   该主版本在来源中的最大号，例如 139 或 139.

环境变量：
  GITHUB_REPOSITORY  目标仓库 owner/repo（必需）
  SOURCE_REPO        来源仓库，默认 rnamoy/chrome_installer
  GH_TOKEN/GITHUB_TOKEN  写操作所需令牌
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
CHUNK = 8 * 1024 * 1024
RETRY_COUNT = int(os.environ.get("RETRY_COUNT", "6"))
RETRY_SLEEP = float(os.environ.get("RETRY_SLEEP", "5"))
# 单个资产的重试总预算（秒）。6 次尝试 × 每次最多 60s 退避，正常边界约 3 分钟；
# 留到 600s 是为了容忍 DNS 抖动，同时避免退化成几十分钟的假死。
RETRY_MAX_SECONDS = float(os.environ.get("RETRY_MAX_SECONDS", "600"))
UPLOAD_TIMEOUT = int(os.environ.get("UPLOAD_TIMEOUT", "3600"))


def retryable(e):
    """判断异常是否值得重试：网络层错误、5xx、以及被限流(403)都重试；
    其它 4xx（如 404/422）是确定性错误，重试没有意义。"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code in (403, 429)
    if isinstance(e, urllib.error.URLError):
        return True
    if isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return True
    return False


def curl_is_transient(e):
    """curl 通道抛出的 RuntimeError 一律视为可重试（curl 已把确定性错误过滤掉）。"""
    return isinstance(e, RuntimeError) or retryable(e)


def retry(what, func, attempts=None, base_sleep=None, max_seconds=None,
          predicate=None):
    """带退避的重试。

    退避序列取自 retry-after 之外的固定步长，并且整体受 max_seconds 约束——
    否则「每个资产 N 次尝试 × 每次 M 次重试」会退化成几十分钟的死循环。
    """
    attempts = attempts or RETRY_COUNT
    base_sleep = RETRY_SLEEP if base_sleep is None else base_sleep
    max_seconds = RETRY_MAX_SECONDS if max_seconds is None else max_seconds
    predicate = predicate or retryable
    started = time.monotonic()
    last = None
    for i in range(1, attempts + 1):
        try:
            return func()
        except Exception as e:
            last = e
            elapsed = time.monotonic() - started
            if i == attempts or not predicate(e):
                break
            if elapsed >= max_seconds:
                print(f"    giving up on {what} after {elapsed:.0f}s "
                      f"(budget {max_seconds:.0f}s)", file=sys.stderr, flush=True)
                break
            sleep_for = min(base_sleep * (2 ** (i - 1)), 60)
            print(f"    retry {i}/{attempts - 1} for {what} after error: {e} "
                  f"(sleep {sleep_for:.0f}s, elapsed {elapsed:.0f}s)",
                  file=sys.stderr, flush=True)
            time.sleep(sleep_for)
    raise last


def version_key(tag):
    parts = tag.split(".")
    if all(p.isdigit() for p in parts):
        return (0, tuple(int(p) for p in parts), tag)
    return (1, (), tag)


def api(path, token=None, method="GET", data=None, headers=None, stream=None):
    hdrs = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "chrome-installer-mirror",
    }
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})

    body = json.dumps(data).encode() if data is not None else stream

    def once():
        req = urllib.request.Request(API + path, data=body, method=method, headers=hdrs)
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = resp.read()
        return json.loads(payload) if payload else None

    return retry(f"{method} {path}", once)


def list_releases(repo, token=None):
    """返回 {tag: {"id":..., "assets": {name: size}}}"""
    out = {}
    page = 1
    while True:
        batch = api(f"/repos/{repo}/releases?per_page=100&page={page}", token)
        if not batch:
            break
        for r in batch:
            out[r["tag_name"]] = {
                "id": r["id"],
                "assets": {a["name"]: a["size"] for a in r.get("assets", [])},
            }
        if len(batch) < 100:
            break
        page += 1
    return out


def max_per_major(tags):
    best = {}
    for t in tags:
        major = t.split(".")[0]
        if not major.isdigit():
            continue
        if major not in best or version_key(t) > version_key(best[major]):
            best[major] = t
    return best


def parse_specs(raw):
    """把 spec 输入拆成 spec 列表：支持逗号、空格、换行分隔，
    例如 "129, 139 149\\n154"。

    none/no/off/- 表示什么都不做（用于工作流注册这类只想空跑的场景）。
    """
    parts = [p for p in re.split(r"[,\s]+", raw or "") if p]
    if len(parts) == 1 and parts[0].lower() in ("none", "no", "off", "-", "无"):
        return []
    return parts


def is_release_tag(name):
    """只接受 x.y.z.w 形式的版本 tag，避免把 Archive 之类的 tag 当成版本处理。"""
    parts = name.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def release_is_complete(tag, source_repo, target_assets, token):
    """目标 Release 是否已经完整拥有源 Release 的全部资产。

    比对采用「架构无关的基名 + 大小」：来源 rnamoy 的 <tag>_xxx.exe 与本仓库
    已有的 x64_<tag>_xxx.exe 视为同一个资产（实测大小一致），因此已镜像过的
    版本不会被判定为不完整而重复下载。

    target_assets 为 None 表示没有目标 Release 信息，无法判断完整性。
    只看资产列表，不依赖 tag 是否存在——孤儿 tag、已创建但没有资产的空 Release
    都会被判定为不完整。
    """
    if target_assets is None:
        return False
    src = api(f"/repos/{source_repo}/releases/tags/{tag}", token)
    src_map = {a["name"]: {"name": a["name"], "size": a["size"]}
               for a in src.get("assets", [])}
    if not src_map:
        return True
    return not assets_to_fetch(src_map, target_assets)


def resolve_specs(specs, source, target, source_repo=None, token=None):
    """把 --spec 解析成待镜像的 tag 列表（保持输入顺序，去重）。

    支持 missing（Release 不存在）、incomplete（存在但资产不全）、all（两者之和）、
    以及精确 tag 或主版本号。
    """
    src_max = max_per_major(source)
    majors = [(major, tag) for major, tag in src_max.items()]

    wanted, unknown = [], []
    for spec in specs:
        if spec in ("missing", "incomplete", "all"):
            wanted.append(spec)
        elif is_release_tag(spec):
            if spec in source:
                wanted.append(spec)
            else:
                print(f"Warning: source has no release {spec}", file=sys.stderr)
        elif spec.rstrip(".").isdigit():
            major = spec.rstrip(".")
            if major in src_max:
                wanted.append(src_max[major])
            else:
                print(f"Warning: source has no release for major {major}",
                      file=sys.stderr)
        else:
            print(f"Warning: cannot resolve spec {spec!r}", file=sys.stderr)

    # 只有批量 spec（missing/incomplete/all）才需要核完整性，精确 tag 直接处理
    bulk = any(s in ("missing", "incomplete", "all") for s in wanted)
    candidates = [t for _, t in majors] if bulk else []

    complete = {}
    if bulk and not source_repo:
        print("Warning: source_repo not given, treating every candidate release "
              "as incomplete", file=sys.stderr)
    for tag in sorted(set(candidates), key=version_key):
        if source_repo:
            complete[tag] = release_is_complete(
                tag, source_repo,
                target[tag].get("assets", {}) if tag in target else None, token)
        else:
            complete[tag] = False

    def absent(tag):
        """Release 完全不存在"""
        return tag not in target

    todo = []
    for spec in wanted:
        if spec == "missing":
            todo.extend(t for _, t in majors if absent(t))
        elif spec == "incomplete":
            todo.extend(t for _, t in majors if not absent(t) and not complete.get(t, True))
        elif spec == "all":
            todo.extend(t for _, t in majors if not complete.get(t, True))
        else:
            todo.append(spec)

    seen, unique = set(), []
    for tag in todo:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return unique


UPLOADS_HOST = "https://uploads.github.com"


def upload_endpoint(upload_url, host=UPLOADS_HOST):
    """返回资产上传地址。

    GitHub 的资产上传端点是 uploads.github.com（api.github.com 上这个路径恒为
    404：实测无效 token 时前者回 401、后者回 404）。这里默认使用 uploads 主机，
    仅把它作为可切换的主机名，供 DNS 抖动时换域名重试。
    """
    base = upload_url.split("{")[0]
    return re.sub(r"^https://[^/]+", host, base)


def curl_path():
    return shutil.which("curl")


def upload_with_curl(url, file_path, ctype, token, timeout=UPLOAD_TIMEOUT):
    """用 curl 上传资产。

    curl 对 DNS 抖动、连接复用、重定向的处理比 urllib 稳健，且 runner 自带；
    这里让 curl 自己先重试 3 次，外层再兜底重试。
    """
    cmd = [
        curl_path(), "-sS", "--fail", "--location",
        "--retry", "3", "--retry-delay", "5", "--retry-all-errors",
        "--connect-timeout", "30", "--max-time", str(timeout),
        "-X", "POST",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/vnd.github+json",
        "-H", f"Content-Type: {ctype}",
        "-H", "User-Agent: chrome-installer-mirror",
        "--data-binary", f"@{file_path}",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"curl exit {proc.returncode}: "
                           f"{detail[-1] if detail else 'no output'}")
    return proc.stdout


def download_with_curl(url, dest, timeout=UPLOAD_TIMEOUT):
    """下载失败时的兜底通道，同样交给 curl。"""
    cmd = [
        curl_path(), "-sS", "--fail", "--location",
        "--retry", "3", "--retry-delay", "5", "--retry-all-errors",
        "--connect-timeout", "30", "--max-time", str(timeout),
        "-H", "User-Agent: chrome-installer-mirror",
        "-o", dest, url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"curl exit {proc.returncode}: "
                           f"{detail[-1] if detail else 'no output'}")
    return os.path.getsize(dest)


def download(url, dest, token=None, limit=None):
    """流式下载到本地文件，返回 (字节数, content-type)。

    limit 仅用于测试：最多下载 N 字节。
    """
    headers = {"User-Agent": "chrome-installer-mirror"}
    req = urllib.request.Request(url, headers=headers)
    size = 0
    last_report = -1
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        ctype = resp.headers.get("content-type", "application/octet-stream")
        total = resp.headers.get("content-length")
        while True:
            if limit is not None and size >= limit:
                break
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            if limit is not None:
                chunk = chunk[:limit - size]
            f.write(chunk)
            size += len(chunk)
            # 每 10% 打一行，避免 \r 进度在 Actions 日志里刷屏
            if total:
                pct = size * 10 // int(total)
                if pct != last_report:
                    last_report = pct
                    print(f"    downloading {min(pct * 10, 100):3d}% "
                          f"({size}/{total})", flush=True)
    return size, ctype


def as_asset_map(value, where=""):
    """把 API 返回的 assets 归一化成 {name: entry}。

    正常情况是 list[dict]，但为了防止上游/中间层返回异常形状（字符串、None、
    被序列化的 JSON 等）导致 `TypeError: string indices must be integers`
    让整个 job 崩溃，这里做一次宽容归一化：
      - None -> {}
      - dict -> 原样（可能是 {name: {"name":..., "size":...}}）
      - list -> 按 name 索引
      - str  -> 尝试 json.loads，失败则视为 {}
    任何无法识别的形状会打印一条 warning 并返回 {}。
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        # {name: size} 这种简化形式也要能识别
        if all(not isinstance(v, dict) for v in value.values()):
            return {k: {"name": k, "size": v} for k, v in value.items()}
        return value
    if isinstance(value, list):
        out = {}
        for item in value:
            if isinstance(item, dict) and "name" in item:
                out[item["name"]] = item
            else:
                print(f"  warning: unexpected asset entry {where}: {item!r}",
                      file=sys.stderr)
        return out
    if isinstance(value, str):
        try:
            return as_asset_map(json.loads(value), where)
        except (ValueError, TypeError):
            print(f"  warning: asset list is a string {where}: {value[:200]!r}",
                  file=sys.stderr)
            return {}
    print(f"  warning: unsupported asset list type {where}: "
          f"{type(value).__name__}", file=sys.stderr)
    return {}


def base_asset_name(name):
    """把资产名归一化到「无架构前缀」的形式。

    来源仓库 rnamoy 只发 x64，且文件名不带架构前缀；而本仓库历史的同名版本
    带 x64_ 前缀。两者其实是同一个文件（实测大小逐字节一致），所以比对时要
    把前缀视为等价，否则会重复下载几 GiB 数据。
    """
    return re.sub(r"^(x86|x64|arm64)_", "", name)


def assets_to_fetch(src_assets, tgt_assets):
    """返回还需要上传的 {name: asset}。

    以「架构无关的基名 + 大小」判定等价：来源的 <tag>_xxx.exe 与本仓库已有的
    x64_<tag>_xxx.exe 视为同一个资产。另外，某个来源资产只要目标里已有任意
    「同一主版本」的 x64 文件且大小一致，也认为等价（文件名可能来自不同来源）。
    """
    by_base = {}
    for n, a in tgt_assets.items():
        size = a.get("size") if isinstance(a, dict) else a
        by_base.setdefault(base_asset_name(n), set()).add(size)

    pending = {}
    for n, a in src_assets.items():
        size = a.get("size") if isinstance(a, dict) else a
        if size in by_base.get(base_asset_name(n), set()):
            continue
        pending[n] = a
    return pending


def ensure_published(release, target_repo, token):
    """确保 Release 是「已发布」而不是草稿。

    草稿 Release 只有有写权限的人可见，对公开存档没有意义；实测创建时虽然传了
    draft=false，仍可能出现草稿，所以这里显式校验并 PATCH 发布。
    """
    if not isinstance(release, dict):
        return release
    if not release.get("draft"):
        print(f"  release state: published (id={release.get('id')})")
        return release

    print(f"  release state: DRAFT -> publishing (id={release.get('id')})")
    patched = api(
        f"/repos/{target_repo}/releases/{release['id']}",
        token,
        method="PATCH",
        data={"draft": False},
    )
    if isinstance(patched, dict) and not patched.get("draft"):
        print("  release published")
        return patched
    print("  warning: release is still a draft after PATCH", file=sys.stderr)
    return patched if isinstance(patched, dict) else release


def mirror_tag(tag, source_repo, target_repo, token, dry_run=False, max_bytes=None,
               target_release=None, target_assets=None):
    src = api(f"/repos/{source_repo}/releases/tags/{tag}", token)
    if not isinstance(src, dict):
        raise RuntimeError(
            f"source API for {tag} returned {type(src).__name__}, "
            f"expected an object: {str(src)[:200]!r}")

    target = target_release
    # 防御：任何非 dict 的 target（例如误传 tag 字符串）都按「目标不存在」处理，
    # 避免 TypeError: string indices must be integers 让整个 job 崩掉。
    if target is not None and not isinstance(target, dict):
        print(f"  warning: ignoring non-dict target for {tag}: "
              f"type={type(target).__name__} repr={str(target)[:200]!r}",
              file=sys.stderr)
        target = None
    if target is None:
        try:
            target = api(f"/repos/{target_repo}/releases/tags/{tag}", token)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        if target is not None and not isinstance(target, dict):
            print(f"  warning: target API for {tag} returned "
                  f"{type(target).__name__}, details: {str(target)[:200]!r}",
                  file=sys.stderr)
            target = None

    src_assets = as_asset_map(src.get("assets"), f"(source {tag})")
    tgt_assets = as_asset_map((target or {}).get("assets"), f"(target {tag})")
    if target_assets:
        tgt_assets = target_assets
    # 目标的 size 统一成整数，便于与源比较
    tgt_sizes = {n: (a.get("size") if isinstance(a, dict) else a)
                 for n, a in tgt_assets.items()}

    print(f"  source assets : {sorted(src_assets)}")
    print(f"  target assets : {sorted(tgt_assets)}")

    if dry_run:
        print(f"  [dry-run] would ensure release {tag} exists with "
              f"{len(src_assets)} asset(s); upload script/body too")
        return 0

    if target is None:
        release = api(
            f"/repos/{target_repo}/releases",
            token,
            method="POST",
            data={
                "tag_name": tag,
                "name": src.get("name") or tag,
                "body": (src.get("body") or "")
                + f"\n\n> Mirrored from {source_repo} release {tag}.",
                "draft": False,
                "prerelease": False,
            },
        )
        # 诊断：把服务端返回的关键字段原样打印，便于排查「传了 draft=false 却仍是草稿」
        print(f"  created release {tag} (id={release.get('id')}) "
              f"draft={release.get('draft')!r} prerelease={release.get('prerelease')!r} "
              f"tag_name={release.get('tag_name')!r} published_at={release.get('published_at')!r}")
    else:
        release = target
        print(f"  release {tag} already exists (id={release.get('id')}) "
              f"draft={release.get('draft')!r} "
              f"published_at={release.get('published_at')!r}")

    # 草稿 Release 只有仓库有写权限的人能看到/下载，对公开存档没有意义。
    # 这里统一校验并发布（创建时已传 draft=false，但实测出现过仍是草稿的情况）。
    release = ensure_published(release, target_repo, token)

    upload_url = release.get("upload_url") or api(
        f"/repos/{target_repo}/releases/tags/{tag}", token
    )["upload_url"]
    print(f"  upload endpoint: {upload_endpoint(upload_url)}")

    failed = []
    # 架构等价判定：来源的无前缀文件名 == 本仓库已有的 x64_ 前缀文件（同一文件），
    # 避免把已镜像过的版本按文件名差异重复下载几个 GiB。
    pending = assets_to_fetch(src_assets, tgt_assets)
    skipped = [n for n in src_assets if n not in pending]
    for name in sorted(skipped):
        print(f"  asset {name}: already present (等价资产), skip")
    if not pending:
        print(f"  all {len(src_assets)} asset(s) already present, nothing to upload")

    for name, asset in sorted(pending.items()):
        existing_size = tgt_sizes.get(name)
        if existing_size is not None and existing_size != asset["size"]:
            print(f"  asset {name}: size mismatch "
                  f"({existing_size} != {asset['size']}), replacing")
            existing_id = tgt_assets.get(name, {}).get("id")
            if existing_id:
                api(f"/repos/{target_repo}/releases/assets/{existing_id}",
                    token, method="DELETE")

        tmpdir = tempfile.mkdtemp(prefix="mirror-")
        tmp = os.path.join(tmpdir, name)
        done = False
        try:
            if max_bytes:
                print(f"  asset {name}: test mode, downloading only {max_bytes} bytes")
            else:
                print(f"  asset {name}: downloading ({asset['size']} bytes)")
            size, ctype = retry(
                f"download {name}",
                lambda: download(asset["browser_download_url"], tmp, limit=max_bytes))

            # 上传前硬校验：字节数必须与源资产声明一致，避免上传半截文件
            actual = os.path.getsize(tmp)
            if actual != asset["size"]:
                raise RuntimeError(
                    f"local file is {actual} bytes, source asset declares "
                    f"{asset['size']} bytes")

            # 上传用 Content-Length 必须与文件一致，这里统一从文件重新取一次大小
            size = actual
            print(f"  asset {name}: uploading ({size} bytes, {ctype})")

            # 上传通道：官方端点 uploads.github.com 优先（api.github.com 上该路径
            # 恒为 404），curl 负责传输，没有 curl 时退回 urllib。
            attempted = []

            def via_curl(host_base):
                attempted.append(f"curl:{host_base}")
                return upload_with_curl(
                    f"{host_base}?name={urllib.parse.quote(name)}",
                    tmp, ctype, token)

            def via_urllib(host_base):
                attempted.append(f"urllib:{host_base}")
                with open(tmp, "rb") as f:
                    return api(f"{host_base}?name={urllib.parse.quote(name)}",
                               token, method="POST",
                               headers={"Content-Type": ctype}, stream=f)

            if curl_path():
                runner = via_curl
            else:
                print("  curl not found, falling back to urllib", file=sys.stderr)
                runner = via_urllib

            def do_upload():
                # 官方上传端点优先，遇到 DNS 抖动时再换 api.github.com 兜底
                last_error = None
                for host in (UPLOADS_HOST, API):
                    host_base = upload_endpoint(upload_url, host)
                    try:
                        return retry(f"upload {name} via {host_base}",
                                     lambda hb=host_base: runner(hb),
                                     predicate=curl_is_transient)
                    except Exception as e:  # noqa: BLE001
                        last_error = e
                        print(f"  asset {name}: {host_base} failed ({e}), "
                              f"trying alternate host", file=sys.stderr)
                raise last_error

            # 兜底重试一次：内部已有「两个主机 × 6 次退避」，外层再重复会拉长最坏耗时
            retry(f"upload {name}", do_upload, attempts=1,
                  predicate=curl_is_transient)
            print(f"  asset {name}: done ({','.join(attempted)})")
            done = True
        except Exception as e:  # 单个资产失败不影响其它资产，重跑可续传
            failed.append(f"{name}: {e}")
            print(f"  asset {name}: FAILED ({e})", file=sys.stderr)
        finally:
            if done:
                shutil.rmtree(tmpdir, ignore_errors=True)
            else:
                # 保留已下载的文件，便于失败后排查或减少重复下载
                print(f"  asset {name}: partial download kept at {tmp}", file=sys.stderr)

    if failed:
        print(f"  {len(failed)} asset(s) failed:", file=sys.stderr)
        for item in failed:
            print(f"    - {item}", file=sys.stderr)
        return 1

    # 上传完成后再确认一次发布状态：实测出现过创建/上传后仍是草稿的情况，
    # 这里做最后一道校验，确保匿名访问可见。
    try:
        final = api(f"/repos/{target_repo}/releases/tags/{tag}", token)
        if isinstance(final, dict) and final.get("draft"):
            print(f"  final check: {tag} is a draft, publishing again")
            ensure_published(final, target_repo, token)
        elif isinstance(final, dict):
            print(f"  final check: {tag} published "
                  f"(assets={len(final.get('assets', []))})")
    except Exception as e:  # noqa: BLE001
        print(f"  final check skipped: {e}", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", action="append", default=[],
                        help="all | missing | incomplete | <tag> | <major>，"
                             "可重复或用逗号分隔的列表，例如 129,139")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                        help="目标仓库 owner/repo")
    parser.add_argument("--source", default=os.environ.get(
        "SOURCE_REPO", "rnamoy/chrome_installer"), help="来源仓库 owner/repo")
    parser.add_argument("--dry-run", action="store_true", help="只列出计划")
    parser.add_argument("--max-bytes", type=int, default=None,
                        help="仅测试用：每个资产只取前 N 字节")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    # 默认按「主版本的 Release 缺失或资产不完整」处理，避免已创建但没资产的 Release
    # 被判定为「已存在」而跳过（这正是上一轮 34 个 job 全部空跑的原因）
    # 未提供 spec 时视为 all；但显式传入 none/- 等「什么都不做」的取值必须保持为空，
    # 否则会退化成 all —— 一次误触发就会把几十个主版本全量拉下来。
    raw_specs = os.environ.get("SPEC") or " ".join(args.spec) or "all"
    specs = parse_specs(raw_specs)
    if not specs and not raw_specs.strip():
        specs = ["all"]
    print(f"spec input: {raw_specs!r} -> {specs}")
    if not specs:
        print("Nothing requested (spec resolved to empty), exiting")
        return 0
    if not args.repo:
        print("Error: repo is not set (--repo or GITHUB_REPOSITORY)", file=sys.stderr)
        return 1
    if not token and not args.dry_run and not args.max_bytes:
        print("Error: GH_TOKEN / GITHUB_TOKEN is not set", file=sys.stderr)
        return 1

    print(f"source: {args.source}")
    print(f"target: {args.repo}")
    source = list_releases(args.source, token)
    target = list_releases(args.repo, token)
    print(f"source releases: {len(source)}, target releases: {len(target)}")

    tags = resolve_specs(specs, source, target, source_repo=args.source, token=token)
    print(f"resolved tags ({len(tags)}): {tags}")
    if not tags:
        print("Nothing to mirror")
        return 0

    rc = 0
    for tag in tags:
        print(f"\n=== {tag}")
        rc |= mirror_tag(tag, args.source, args.repo, token,
                         dry_run=args.dry_run, max_bytes=args.max_bytes,
                         target_release=target.get(tag))
    print(f"\nfinished, {len(tags)} tag(s), exit={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
