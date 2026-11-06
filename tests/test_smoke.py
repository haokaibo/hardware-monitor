"""The smoke script itself: it must pass on a healthy deploy and catch regressions."""

from __future__ import annotations

import json

import pytest

from hwmon.config import Config

import smoke
from conftest import FIXTURES, FakeAgent, free_port, make_checkout_server

SAMPLE = json.loads((FIXTURES / "sample_metrics.json").read_text())


@pytest.fixture
def deploy(http_server, tmp_path):
    """A web server proxying a fake agent, plus a local index.html to compare against."""
    index = tmp_path / "index.html"
    index.write_text((FIXTURES.parent.parent / "src/hwmon/static/index.html").read_text())
    fake = FakeAgent(payload=SAMPLE, counter=True, http_server=http_server)
    cfg = Config(mode="web", token="", version="1.0.0", port=0,
                 agent_url=f"http://127.0.0.1:{fake.port}", label="host-a")
    srv = make_checkout_server(http_server, cfg, lambda: {})
    srv.fake = fake
    srv.index = index
    srv.base = f"http://127.0.0.1:{srv.port}"
    return srv


def run_smoke(deploy, log, **kw):
    kw = {"retries": 2, "delay": 0.05, "fresh_wait": 0.05, "out": log.append, **kw}
    return smoke.run(deploy.base, **kw)


def test_passes_on_healthy_deploy(deploy):
    log: list[str] = []
    assert run_smoke(deploy, log, expect_index=str(deploy.index)) == 0, "\n".join(log)
    assert any("matches" in line for line in log)


def test_page_only_skips_metrics(deploy):
    log: list[str] = []
    assert run_smoke(deploy, log, page_only=True) == 0, "\n".join(log)
    assert any("page-only" in line for line in log)
    # no PASS/FAIL/WARN line may come from the metrics section
    assert not any(line.split()[0] in ("PASS", "FAIL", "WARN") and "metrics" in line for line in log)


def test_detects_stale_index(deploy, tmp_path):
    log: list[str] = []
    other = tmp_path / "newer.html"
    other.write_text("<html>newer</html>")
    assert run_smoke(deploy, log, expect_index=str(other)) == 1
    assert any("differs" in line for line in log)


def test_detects_stuck_agent(deploy):
    deploy.fake.counter = False  # ts stops advancing
    log: list[str] = []
    assert run_smoke(deploy, log) == 1
    assert any("not advancing" in line for line in log)


def test_detects_contract_break(deploy):
    broken = json.loads(json.dumps(SAMPLE))
    del broken["mem"]["used"]
    deploy.fake.payload = broken
    log: list[str] = []
    assert run_smoke(deploy, log) == 1
    assert any("mem: missing 'used'" in line for line in log)


def test_detects_unreachable_agent(http_server, tmp_path):
    cfg = Config(mode="web", token="", version="1.0.0", port=0,
                 agent_url=f"http://127.0.0.1:{free_port()}", label="host-a")
    srv = make_checkout_server(http_server, cfg, lambda: {})
    log: list[str] = []
    assert smoke.run(f"http://127.0.0.1:{srv.port}", retries=2, delay=0.05,
                     fresh_wait=0.05, out=log.append) == 1


def test_detects_missing_page(http_server):
    """A server that answers /healthz but has no dashboard must fail the smoke test."""
    from http.server import BaseHTTPRequestHandler

    class OnlyHealth(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"ok" if self.path == "/healthz" else b"{}"
            self.send_response(200 if self.path == "/healthz" else 404)
            self.send_header("Content-Type", "text/plain" if self.path == "/healthz" else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http_server(OnlyHealth)
    log: list[str] = []
    assert smoke.run(f"http://127.0.0.1:{srv.port}", retries=2, delay=0.05,
                     fresh_wait=0.05, out=log.append) == 1
    # urllib turns a 404 into an exception, so this surfaces as "not reachable"
    assert any("not reachable" in line or "not the hwmon dashboard" in line for line in log)


def test_server_down_is_reported():
    log: list[str] = []
    assert smoke.run(f"http://127.0.0.1:{free_port()}", retries=1, delay=0.01,
                     fresh_wait=0.01, out=log.append) == 1
    assert any("not ready" in line for line in log)


def test_cli_exposes_page_only_flag():
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        with pytest.raises(SystemExit) as exc:
            smoke.main(["--help"])
    assert exc.value.code == 0
    assert "--page-only" in buf.getvalue()
