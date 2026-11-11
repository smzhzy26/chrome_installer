"""核心回归测试：归档规则（每主版本留最大号）+ 手动删除 + 复核 + spec 安全默认。

运行：python tests_core.py
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent
fails = []


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(name, got, want):
    if got == want:
        print(f"PASS {name}")
    else:
        fails.append(name)
        print(f"FAIL {name}\n     got : {got}\n     want: {want}")


# ============ archive_releases ============
a = load("arch", "archive_releases.py")

TAGS = ["120.0.6099.225", "121.0.6167.185", "129.0.6668.59", "129.0.6668.101",
        "139.0.7258.128", "139.0.7258.155", "149.0.7827.201", "155.0.8059.40"]

# --- 归档：每个主版本留最大号 ---
keep, delete = a.plan(TAGS)
check("归档：每主版本留最大号",
      (sorted(keep), sorted(delete)),
      (sorted(["120.0.6099.225", "121.0.6167.185", "129.0.6668.101",
               "139.0.7258.155", "149.0.7827.201", "155.0.8059.40"]),
       sorted(["129.0.6668.59", "139.0.7258.128"])))

# --- 每个主版本只有一个版本时全部保留（当前仓库的状态）---
single = ["127.0.6533.120", "128.0.6613.138", "129.0.6668.101", "137.0.7151.120",
          "138.0.7204.184", "139.0.7258.155", "147.0.7727.138", "148.0.7778.217",
          "149.0.7827.201", "155.0.8059.40"]
keep, delete = a.plan(single)
check("归档：每主版本单版本时全保留", (sorted(keep), sorted(delete)),
      (sorted(single), []))

# --- 同主版本多版本时只留最大号 ---
keep, delete = a.plan(single + ["156.0.8078.17", "156.0.8090.1"])
check("归档：156 两个版本只留最大号，历史 10 个不动",
      (len(keep), sorted(delete)),
      (11, ["156.0.8078.17"]))

# --- 保护机制已移除 ---
check("archive 无 parse_majors", hasattr(a, "parse_majors"), False)
check("archive 无 DEFAULT_KEEP_MAJORS", hasattr(a, "DEFAULT_KEEP_MAJORS"), False)
check("plan 只接受一个参数", a.plan.__code__.co_argcount, 1)

# --- parse_delete_majors ---
for raw, want in [(None, set()), ("", set()), ("120,121", {"120", "121"}),
                  ("all", {"*"}), ("全部", {"*"})]:
    check(f"parse_delete_majors({raw!r})", a.parse_delete_majors(raw), want)

# --- 手动整组删除 ---
keep, delete = a.plan_delete_majors(TAGS, {"149"})
check("手动删除指定主版本",
      ("149.0.7827.201" in delete, "149.0.7827.201" in keep), (True, False))

# --- 删除端到端：按 id 删除 + 复核 ---
ID2TAG = {i: t for i, t in enumerate(TAGS, start=1)}
EXISTING = set(TAGS)


class FakeResp:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {}

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *x):
        return False


def fake_urlopen(req, timeout=None):
    url = req.full_url
    if "/releases?per_page" in url:
        return FakeResp([{"tag_name": t, "id": i, "draft": False}
                         for i, t in ID2TAG.items() if t in EXISTING])
    if req.get_method() == "DELETE":
        if "/git/refs/tags/" in url:
            return FakeResp({})
        rid = url.rsplit("/", 1)[-1]
        if rid.isdigit():
            tag = ID2TAG.get(int(rid))
            if tag:
                EXISTING.discard(tag)
        return FakeResp({})
    return FakeResp({})


a.urllib.request.urlopen = fake_urlopen
os.environ.update(GITHUB_REPOSITORY="o/r", GH_TOKEN="t", DELETE_MAJORS="all")
sys.argv = ["archive_releases.py"]
rc = a.main()
check("删除端到端：all 清空并复核通过", (rc, sorted(EXISTING)), (0, []))

# --- 复核能抓出「状态码撒谎」---
EXISTING.clear()
EXISTING.update(TAGS)


def fake_no_effect(req, timeout=None):
    url = req.full_url
    if "/releases?per_page" in url:
        return FakeResp([{"tag_name": t, "id": i, "draft": False}
                         for i, t in ID2TAG.items() if t in EXISTING])
    return FakeResp({})   # DELETE 返回 200 但不删除


a.urllib.request.urlopen = fake_no_effect
rc = a.main()
check("删除端到端：复核抓出未真正删除", (rc, len(EXISTING)), (1, len(TAGS)))

# ============ mirror_releases ============
m = load("mirror", "mirror_releases.py")

for raw, want in [("none", []), ("-", []), ("all", ["all"]),
                  ("129,139", ["129", "139"]), ("129 139", ["129", "139"])]:
    check(f"parse_specs({raw!r})", m.parse_specs(raw), want)

check("as_asset_map(None)", m.as_asset_map(None), {})
check("as_asset_map(垃圾字符串)", m.as_asset_map("oops"), {})
check("as_asset_map(正常 list)",
      m.as_asset_map([{"name": "a", "size": 1}]),
      {"a": {"name": "a", "size": 1}})

up = "https://uploads.github.com/repos/o/r/releases/9/assets{?name,label}"
check("upload_endpoint 默认 uploads 主机",
      m.upload_endpoint(up),
      "https://uploads.github.com/repos/o/r/releases/9/assets")
check("upload_endpoint 可切到 api 主机",
      m.upload_endpoint(up, m.API),
      "https://api.github.com/repos/o/r/releases/9/assets")

# --- 不再有 --limit ---
captured = {}
orig_parse = argparse.ArgumentParser.parse_args


def spy(self, *args, **kw):
    captured["parser"] = self
    raise SystemExit(0)


argparse.ArgumentParser.parse_args = spy
try:
    sys.argv = ["mirror_releases.py", "--dry-run"]
    m.main()
except SystemExit:
    pass
finally:
    argparse.ArgumentParser.parse_args = orig_parse
opts = [s for act in captured["parser"]._actions for s in act.option_strings]
check("mirror 已无 --limit", "--limit" in opts, False)
check("mirror 仍有 --spec/--dry-run",
      ("--spec" in opts, "--dry-run" in opts), (True, True))

# --- retry 预算 ---
calls = {"n": 0}


def always_fail():
    calls["n"] += 1
    raise urllib.error.URLError(OSError(-2, "Name or service not known"))


t0 = time.monotonic()
try:
    m.retry("x", always_fail, attempts=10, base_sleep=1, max_seconds=3)
except urllib.error.URLError:
    pass
check("retry 预算生效（<30s 内放弃）", time.monotonic() - t0 < 30, True)

# --- ensure_published ---
PATCHES = []


def fake_api(path, token=None, method="GET", data=None, headers=None, stream=None):
    if method == "PATCH":
        PATCHES.append(path)
        return {"id": 1, "draft": False}
    return {"id": 1, "draft": False}


m.api = fake_api
check("ensure_published 已发布时不动",
      m.ensure_published({"id": 1, "draft": False}, "o/r", "t")["draft"], False)
check("ensure_published 草稿会 PATCH",
      (m.ensure_published({"id": 1, "draft": True}, "o/r", "t")["draft"], len(PATCHES)),
      (False, 1))

print()
if fails:
    print(f"=== {len(fails)} 项失败: {fails} ===")
    sys.exit(1)
print("=== 全部通过 ===")
