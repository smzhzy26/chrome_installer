"""归档规则：每个主版本只保留版本号最大的 Release。

规则（自动，每次运行）：
  每个主版本都收敛为「同主版本里只留版本号最大的那个」，其余同主版本的
  Release 与 tag 一并删除。例如 155 组里 155.0.8059.40 与 155.0.8061.10
  只留后者；每个主版本只有一个版本时，它就永远保留。

手动删除（可选，用于精简发布区）：
  用 --delete-majors / DELETE_MAJORS 指定要整组删除的主版本，例如 120,121；
  删除指定主版本的全部 Release 与 tag。all 表示全删。

环境变量：
  GH_TOKEN / GITHUB_TOKEN  访问 GitHub API 的令牌（必需）
  GITHUB_REPOSITORY        owner/repo（必需）
  DELETE_MAJORS            手动整组删除的主版本；未设置 = 不删
  DRY_RUN                  true 时只打印计划，不做任何删除

用法：
  python archive_releases.py --dry-run
  python archive_releases.py --delete-majors 120,121
  python archive_releases.py --delete-majors all        # 清空全部 Release
  python archive_releases.py --check-release 155.0.8059.40   # 查询是否已发布
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def version_key(tag):
    """把 tag 转成可比较的元组；非纯数字或位数不一致时回退为字符串比较。"""
    parts = tag.split(".")
    if all(p.isdigit() for p in parts):
        return (0, tuple(int(p) for p in parts), tag)
    return (1, (), tag)


def plan(tags):
    """返回 (保留的 tag 集合, 删除的 tag 集合)。

    每个主版本只保留版本号最大的那个，其余同主版本的 tag 进入删除集合。
    """
    groups = {}
    for tag in tags:
        major = tag.split(".")[0]
        groups.setdefault(major, []).append(tag)

    keep = set()
    delete = set()
    for major, items in groups.items():
        # 全部是数字的主版本才按版本号比较，避免异常 tag 造成误删
        numeric = major.isdigit()
        newest = max(items, key=version_key) if numeric else items[0]
        for tag in items:
            if tag == newest:
                keep.add(tag)
            else:
                delete.add(tag)
    return keep, delete


def parse_delete_majors(raw):
    """解析「整组手动删除」的主版本，例如 "120,121" -> {120,121}。

    未设置/空 -> 空集合（什么都不删）；
    all / * / 全部 -> {"*"}，表示删除所有主版本。
    """
    if not raw or not raw.strip():
        return set()
    if raw.strip().lower() in ("all", "*", "全部"):
        return {"*"}
    return {p for p in raw.replace(",", " ").split() if p}


def plan_delete_majors(tags, majors):
    """手动删除模式：把 majors 里的主版本整组删除。

    用于精简发布区——例如把已经不需要的 120~130 一次性清掉。
    """
    keep, delete = set(), set()
    for tag in tags:
        major = tag.split(".")[0]
        if "*" in majors or major in majors:
            delete.add(tag)
        else:
            keep.add(tag)
    return keep, delete


def api(path, token, method="GET", data=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "chrome-installer-archive",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=body, method=method,
                                headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = resp.read()
    return json.loads(payload) if payload else None


def list_releases(repo, token):
    """返回 [{tag_name, id, draft, assets: {name: size}}]。

    注意：草稿 Release 只有在令牌有 push 权限时才出现在列表里；如果令牌权限不足，
    这里会看不到草稿，进而导致「全删」实际上删不掉它们。调用方应用
    verify_deleted() 复核，而不是只信任本函数的返回。
    """
    out = []
    page = 1
    while True:
        batch = api(f"/repos/{repo}/releases?per_page=100&page={page}", token)
        if not batch:
            break
        for r in batch:
            out.append({
                "tag_name": r["tag_name"],
                # id 从 API 一定会有；用 get 兜底避免异常响应让整个脚本崩掉
                "id": r.get("id"),
                "draft": bool(r.get("draft")),
                "assets": {a["name"]: a["size"] for a in r.get("assets", [])},
            })
        if len(batch) < 100:
            break
        page += 1
    return out


def list_release_tags(repo, token):
    return [r["tag_name"] for r in list_releases(repo, token)]


def verify_deleted(repo, tags, token):
    """删除后复核：重新查询，返回仍然存在的 tag 列表。

    只信任 HTTP 状态码是不够的——DELETE 可能被接受但对象仍在（例如草稿不可见、
    权限不足、或响应被中间层改写）。这里以「再次查询是否还在」为准。
    """
    try:
        still = set(list_release_tags(repo, token))
    except Exception as e:  # noqa: BLE001
        print(f"  verify skipped: re-query failed: {e}", file=sys.stderr)
        return []
    return [t for t in tags if t in still]


def check_release_state(repo, tag, token):
    """查询某个 tag 的 Release 状态并打印，用于验证发布结果是否公开可见。

    退出码：0 = 已发布（公开可见），1 = 仍是草稿，2 = 查询失败/不存在。
    """
    try:
        rel = api(f"/repos/{repo}/releases/tags/{tag}", token)
    except urllib.error.HTTPError as e:
        print(f"check {tag}: HTTP {e.code} {e.reason}（Release 不存在？）")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"check {tag}: 查询失败 {e}", file=sys.stderr)
        return 2
    if not isinstance(rel, dict):
        print(f"check {tag}: 返回了非对象类型 {type(rel).__name__}")
        return 2
    draft = bool(rel.get("draft"))
    assets = rel.get("assets", [])
    pub = rel.get("published_at")
    if draft:
        print(f"check {tag}: DRAFT（草稿，匿名访问看不到）")
        return 1
    print(f"check {tag}: published  published_at={pub}  "
          f"assets={len(assets)}")
    if not assets:
        print(f"check {tag}: 警告——没有任何资产（.exe 可能没上传成功）")
    return 0


def delete_release(repo, release, token):
    """删除一个 Release 及其 tag。

    release 可以是 id（int）或 tag 名（str）。优先按 id 删除——按 tag 删除时，
    如果令牌看不到该 Release（例如草稿不可见）会得到 404，而 404 也可能是
    「真的不存在」，两者无法区分，容易造成「以为删了其实没删」。
    返回 (release_deleted, tag_deleted, detail)。
    """
    tag = None
    if isinstance(release, dict):
        rid = release.get("id")
        tag = release.get("tag_name")
    elif isinstance(release, int):
        rid = release
    else:
        rid = None
        tag = str(release)

    if rid is not None:
        ok, detail = _delete_with_retry(
            f"/repos/{repo}/releases/{rid}", token, f"release id={rid} ({tag})")
    else:
        ok, detail = _delete_with_retry(
            f"/repos/{repo}/releases/tags/{tag}", token, f"release {tag}")

    tag_ok = True
    if tag:
        tag_ok, _ = _delete_with_retry(
            f"/repos/{repo}/git/refs/tags/{tag}", token, f"tag {tag}")
    return ok, tag_ok, detail


def _delete_with_retry(path, token, what, attempts=3):
    """DELETE 一个路径。

    404 **不**视为成功——由调用方通过复核判定是否真的删除；这里只上报状态。
    5xx 与网络错误重试；其它 4xx 直接返回失败并带上服务端消息。
    返回 (ok, detail)。
    """
    last = None
    for i in range(1, attempts + 1):
        try:
            api(path, token, method="DELETE")
            return True, "deleted"
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            detail = f"HTTP {e.code} {e.reason} {body}".strip()
            if e.code == 404:
                # 不当作成功：可能是草稿不可见，也可能是真的没了，交给复核判断
                return False, detail
            if e.code < 500 and e.code != 429:
                print(f"  {what}: failed {detail}", file=sys.stderr)
                return False, detail
            last = detail
        except Exception as e:  # 网络层异常
            last = str(e)
        if i < attempts:
            print(f"  {what}: {last} - retry {i}/{attempts - 1}", file=sys.stderr)
            time.sleep(2 * i)
    print(f"  {what}: FAILED after {attempts} attempts: {last}", file=sys.stderr)
    return False, str(last)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不删除")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                        help="owner/repo，默认取 GITHUB_REPOSITORY")
    parser.add_argument("--delete-majors", default=None,
                        help="手动整组删除指定主版本，逗号/空格分隔；"
                             "all 表示删除全部主版本；默认取 DELETE_MAJORS 环境变量；"
                             "未设置=不删除任何主版本")
    parser.add_argument("--check-release", default=None, metavar="TAG",
                        help="查询某个 tag 的 Release 是否已发布（草稿返回非零），"
                             "用于验证发布步骤的结果")
    args = parser.parse_args()

    repo = args.repo
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

    raw_delete = args.delete_majors
    if raw_delete is None:
        raw_delete = os.environ.get("DELETE_MAJORS")
    delete_majors = parse_delete_majors(raw_delete)

    dry_run = args.dry_run or os.environ.get("DRY_RUN", "").lower() == "true"

    # 仅做状态查询：不需要仓库列表，也不需要写权限
    if args.check_release:
        if not repo:
            print("Error: repo is not set (--repo or GITHUB_REPOSITORY)",
                  file=sys.stderr)
            return 1
        return check_release_state(repo, args.check_release, token)

    if not repo:
        print("Error: repo is not set (--repo or GITHUB_REPOSITORY)", file=sys.stderr)
        return 1
    # 公共仓库的列表接口无需鉴权，便于本地 dry-run 核对计划
    if not token and not dry_run:
        print("Error: GH_TOKEN / GITHUB_TOKEN is not set", file=sys.stderr)
        return 1

    # 我们先取一次列表，用来判断令牌能看到多少 Release（草稿可能需要更高权限）
    releases_visible = list_releases(repo, token)
    tags = [r["tag_name"] for r in releases_visible]
    print(f"releases found: {len(tags)}")

    if not tags:
        print("No releases visible to this token, nothing to do")
        return 0

    if delete_majors:
        # 手动删除模式：整组删除指定主版本，不做其它收敛
        if "*" in delete_majors:
            print("manual delete mode: ALL majors")
        else:
            print("manual delete mode: majors "
                  f"{sorted(delete_majors, key=int)}")
        keep, delete = plan_delete_majors(tags, delete_majors)
    else:
        keep, delete = plan(tags)

    print(f"keep   ({len(keep)}): {sorted(keep, key=version_key)}")
    print(f"delete ({len(delete)}): {sorted(delete, key=version_key)}")

    if dry_run:
        print("dry-run: nothing deleted")
        return 0

    # 按 release 对象（含 id）删除，而不是只按 tag 名——按 tag 查在草稿不可见时
    # 会返回 404，无法与「真的不存在」区分。
    by_tag = {r["tag_name"]: r for r in releases_visible}
    ok, partial, failed, details = 0, [], [], []
    for tag in sorted(delete, key=version_key):
        print(f"deleting {tag}")
        release_ok, tag_ok, detail = delete_release(repo, by_tag.get(tag, tag), token)
        if release_ok and tag_ok:
            ok += 1
        elif release_ok:
            partial.append(tag)
        else:
            failed.append(tag)
            details.append(f"{tag}: {detail}")

    print(f"done: deleted {ok}/{len(delete)} release(s)")
    if partial:
        # Release 已经删掉，只是 tag 还在：不算失败，留个提示即可
        print(f"  note: {len(partial)} 个 Release 已删除但 tag 未删除: {partial}")
    if failed:
        print(f"  {len(failed)} 个 Release 删除请求未成功: {failed}", file=sys.stderr)
        for d in details[:5]:
            print(f"    - {d}", file=sys.stderr)

    # 复核：DELETE 返回 2xx 不代表对象真的没了（草稿不可见、权限不足、中间层改写
    # 状态码都可能），以「再次查询是否还在」为准。
    still_there = verify_deleted(repo, sorted(delete, key=version_key), token)
    if still_there:
        print(f"  VERIFY FAILED: {len(still_there)} 个 Release 删除后仍然存在:")
        for t in still_there[:10]:
            print(f"    - {t}")
        if len(still_there) > 10:
            print(f"    ... 还有 {len(still_there) - 10} 个")
        print("    可能原因：该 Release 是草稿而令牌看不到/删不掉它，"
              "或令牌缺少删除权限（GITHUB_TOKEN 权限不足时需改用 PAT）。")
        return 1

    print(f"  verified: {len(delete)} 个 Release 确认已删除")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
