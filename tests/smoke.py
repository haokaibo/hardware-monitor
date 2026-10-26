#!/usr/bin/env python3
"""部署后冒烟测试。用法:
    python3 tests/smoke.py http://127.0.0.1:8080 [--token T] [--expect-index index.html]
检查: 服务就绪 / 页面是仪表盘 / 线上 index.html 与仓库一致 / /api/metrics 符合契约 / 数据在更新。
有 FAIL 时退出码为 1; WARN 不影响退出码。只依赖标准库。"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract  # noqa: E402


def fetch(base, path, token="", timeout=5):
    req = Request(base.rstrip("/") + path, headers={"X-Token": token} if token else {})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def run(base, token="", expect_index=None, retries=15, delay=2.0, fresh_wait=2.5, out=print):
    fails, warns = [], []

    def ok(msg):
        out(f"PASS  {msg}")

    def fail(msg):
        fails.append(msg)
        out(f"FAIL  {msg}")

    def warn(msg):
        warns.append(msg)
        out(f"WARN  {msg}")

    # 1. 等服务就绪 (容器刚重启时需要几秒)
    last = "no response"
    for _ in range(retries):
        try:
            st, _, body = fetch(base, "/healthz")
            if st == 200 and body == b"ok":
                break
            last = f"HTTP {st}"
        except (URLError, OSError) as e:
            last = str(e)
        time.sleep(delay)
    else:
        fail(f"/healthz not ready after {retries} tries ({last})")
        return 1
    ok("/healthz")

    # 2. 页面
    st, ctype, body = fetch(base, "/")
    page = body.decode("utf-8", "replace")
    if st != 200:
        fail(f"/ returned HTTP {st}")
    elif "text/html" not in ctype:
        fail(f"/ content-type is {ctype!r}, expected text/html")
    elif 'id="cpu-rows"' not in page:
        fail('/ is not the hwmon dashboard (marker id="cpu-rows" missing)')
    else:
        ok("/ serves the dashboard")
    if expect_index:
        want = hashlib.sha256(Path(expect_index).read_bytes()).hexdigest()
        if want == hashlib.sha256(body).hexdigest():
            ok(f"deployed index.html matches {expect_index}")
        else:
            fail(f"deployed index.html differs from {expect_index} (stale deploy or cached Docker layer?)")

    # 2.5 版本号 (每次 push 一个新版本; 旧版 app.py 没有该接口时只警告)
    try:
        st, _, body = fetch(base, "/api/info")
        v = json.loads(body).get("version", "") if st == 200 else ""
    except (URLError, OSError, ValueError):
        st, v = 0, ""
    if st == 200 and v:
        ok(f"version: {v}")
    else:
        warn(f"/api/info returned HTTP {st} (old app.py without /api/info?)")

    # 3. 数据契约 (agent 刚启动时 STATE 可能还是空的, 重试)
    st, body, m = 0, b"", None
    for _ in range(retries):
        try:
            st, _, body = fetch(base, "/api/metrics", token)
            m = json.loads(body)
        except (URLError, OSError, ValueError):
            m = None
        if st == 200 and isinstance(m, dict) and "cpu" in m:
            break
        time.sleep(delay)
    if st != 200 or not isinstance(m, dict) or "cpu" not in m:
        fail(f"/api/metrics not usable (HTTP {st}): {body[:200]!r}")
    else:
        problems = contract.check_payload(m, require_gpu=False)
        for p in problems:
            fail(f"/api/metrics contract: {p}")
        if not problems:
            ok("/api/metrics matches the contract")
        if not m.get("gpus"):
            warn("/api/metrics has no GPU entry")
        if m.get("error"):
            warn(f"agent reports an error: {m['error']}")

        # 4. 数据在更新 (用 ts 是否前进判断, 不依赖两台机器的时钟是否同步)
        ts1 = m.get("ts")
        time.sleep(fresh_wait)
        try:
            _, _, body2 = fetch(base, "/api/metrics", token)
            ts2 = json.loads(body2).get("ts")
        except (URLError, OSError, ValueError):
            ts2 = None
        if ts1 is not None and ts2 is not None and ts2 > ts1:
            ok("agent data is updating")
        else:
            fail(f"agent ts not advancing ({ts1} -> {ts2}); sampling loop stuck?")

    # 5. favicon (镜像里没 COPY 时 404, 只警告)
    st, _, _ = fetch(base, "/favicon.svg")
    if st == 200:
        ok("/favicon.svg")
    else:
        warn(f"/favicon.svg returned HTTP {st} (add it to the Dockerfile COPY line)")

    out(f"{len(fails)} failed, {len(warns)} warnings")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base_url")
    ap.add_argument("--token", default="")
    ap.add_argument("--expect-index", help="仓库里的 index.html 路径; 与线上内容的 sha256 比对")
    ap.add_argument("--retries", type=int, default=15)
    ap.add_argument("--delay", type=float, default=2.0)
    a = ap.parse_args()
    sys.exit(run(a.base_url, a.token, a.expect_index, a.retries, a.delay))


if __name__ == "__main__":
    main()
