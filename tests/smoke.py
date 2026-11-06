#!/usr/bin/env python3
"""Post-deploy smoke test. Usage:
    python3 tests/smoke.py http://127.0.0.1:8080 [--token T] [--expect-index index.html]
Checks: service is up / index.html equals the repo copy / /api/metrics conforms to the
contract / data is advancing.
Exits 1 on FAIL; WARN does not affect the exit code. Standard library only."""
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


def run(base, token="", expect_index=None, retries=15, delay=2.0, fresh_wait=2.5, out=print,
        page_only=False):
    fails, warns = [], []

    def ok(msg):
        out(f"PASS  {msg}")

    def fail(msg):
        fails.append(msg)
        out(f"FAIL  {msg}")

    def warn(msg):
        warns.append(msg)
        out(f"WARN  {msg}")

    # 1. wait until ready (a freshly restarted container needs a few seconds)
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

    # 2. page
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

    # 2.5 version (a new build per push; only warn when an older app.py lacks the endpoint)
    try:
        st, _, body = fetch(base, "/api/info")
        v = json.loads(body).get("version", "") if st == 200 else ""
    except (URLError, OSError, ValueError):
        st, v = 0, ""
    if st == 200 and v:
        ok(f"version: {v}")
    else:
        warn(f"/api/info returned HTTP {st} (old app.py without /api/info?)")

    if page_only:
        # CI without a reachable agent: everything above is page/service integrity only
        out(f"INFO  page-only mode: skipped /api/metrics contract ({len(fails)} fail, {len(warns)} warn)")
        return 1 if fails else 0

    # 3. data contract (STATE may still be empty right after agent start, so retry)
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

        # 4. data is advancing: judged by whether ts moves, not by comparing two clocks
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

    # 5. favicon (404 when the image did not COPY it; warn only)
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
    ap.add_argument("--expect-index", help="path to the repo index.html; compared with the live content by sha256")
    ap.add_argument("--page-only", action="store_true",
                    help="skip /api/metrics checks (no agent reachable, e.g. a bare CI build)")
    ap.add_argument("--retries", type=int, default=15)
    ap.add_argument("--delay", type=float, default=2.0)
    a = ap.parse_args()
    sys.exit(run(a.base_url, a.token, a.expect_index, a.retries, a.delay, page_only=a.page_only))


if __name__ == "__main__":
    main()
