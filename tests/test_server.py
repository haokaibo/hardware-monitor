"""HTTP surface tests: agent routes, web proxying, and static file handling."""

from __future__ import annotations

import http.client
import json

import pytest

from hwmon import contract
from hwmon.config import Config
from hwmon.server import ERR_AGENT_UNREACHABLE

from conftest import FIXTURES, FakeAgent, free_port, make_checkout_server

SAMPLE = json.loads((FIXTURES / "sample_metrics.json").read_text())


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, body


def config(mode, *, token="", agent_url="", version="1.0.0"):
    # port defaults to 0 so the fixture can bind an ephemeral port
    return Config(mode=mode, token=token, version=version, port=0, agent_url=agent_url, label="host-a")


@pytest.fixture
def agent(http_server):
    """An agent-mode server whose state is fixed for the test."""
    return make_checkout_server(http_server, config("agent"), lambda: {"cpu": {"usage": 1}, "ts": 1.0})


def test_healthz(agent):
    st, h, body = get(agent.port, "/healthz")
    assert (st, body) == (200, b"ok")
    assert h["content-type"] == "text/plain"
    assert h["cache-control"] == "no-store"


def test_metrics_without_token_configured(agent):
    st, h, body = get(agent.port, "/api/metrics")
    assert st == 200
    assert json.loads(body)["ts"] == 1.0
    assert h["cache-control"] == "no-store"
    assert "application/json" in h["content-type"]


@pytest.mark.parametrize("headers", [{}, {"X-Token": "wrong"}, {"X-Token": "é"}, {"X-Token": ""}])
def test_metrics_token_rejected(http_server, headers):
    srv = make_checkout_server(http_server, config("agent", token="s3cret"), lambda: {"ts": 1.0})
    st, _, body = get(srv.port, "/api/metrics", headers)
    assert st == 401
    assert json.loads(body) == {"error": "unauthorized"}


def test_metrics_token_accepted(http_server):
    srv = make_checkout_server(http_server, config("agent", token="s3cret"), lambda: {"ts": 1.0})
    assert get(srv.port, "/api/metrics", {"X-Token": "s3cret"})[0] == 200


def test_healthz_needs_no_token(http_server):
    srv = make_checkout_server(http_server, config("agent", token="s3cret"), lambda: {})
    assert get(srv.port, "/healthz")[0] == 200


def test_api_info_reports_version(http_server):
    srv = make_checkout_server(http_server, config("agent", version="1.2.99"), lambda: {})
    st, _, body = get(srv.port, "/api/info")
    assert (st, json.loads(body)) == (200, {"version": "1.2.99"})


def test_agent_does_not_serve_pages(agent):
    assert get(agent.port, "/")[0] == 404
    assert get(agent.port, "/nope")[0] == 404


# ---------------------------------------------------------------- web mode


@pytest.fixture
def web(http_server):
    """A web-mode server proxying to a fake agent with live data."""
    fake = FakeAgent(payload=SAMPLE, counter=True, http_server=http_server)
    srv = make_checkout_server(
        http_server, config("web", token="tok", agent_url=f"http://127.0.0.1:{fake.port}"), lambda: {}
    )
    srv.fake = fake
    return srv


def test_serves_index_and_favicon(web):
    st, h, body = get(web.port, "/")
    assert st == 200
    assert h["content-type"] == "text/html; charset=utf-8"
    assert b'id="cpu-rows"' in body
    assert get(web.port, "/?x=1")[0] == 200  # query string ignored
    st, h, _ = get(web.port, "/favicon.svg")
    assert (st, h["content-type"]) == (200, "image/svg+xml")


@pytest.mark.parametrize(
    "path", ["/secret.txt", "/pyproject.toml", "/index.html", "/../secret.txt", "/%2e%2e/secret.txt", "/nope"]
)
def test_only_whitelisted_paths_are_served(web, path):
    assert get(web.port, path)[0] == 404


def test_proxies_metrics_and_forwards_token(web):
    st, _, body = get(web.port, "/api/metrics")
    assert st == 200
    assert contract.check_payload(json.loads(body)) == []
    assert web.fake.seen[-1].get("X-Token") == "tok"


def test_agent_error_becomes_502_with_message(web):
    web.fake.status = 401
    st, _, body = get(web.port, "/api/metrics")
    assert st == 502
    assert "401" in json.loads(body)["error"]


def test_agent_down_becomes_502(http_server):
    srv = make_checkout_server(
        http_server, config("web", token="tok", agent_url=f"http://127.0.0.1:{free_port()}"), lambda: {}
    )
    st, _, body = get(srv.port, "/api/metrics")
    assert st == 502
    assert json.loads(body)["error"].startswith(ERR_AGENT_UNREACHABLE)


def test_missing_agent_url_becomes_502(http_server):
    srv = make_checkout_server(http_server, config("web"), lambda: {})
    st, _, body = get(srv.port, "/api/metrics")
    assert st == 502
    assert "AGENT_URL" in json.loads(body)["error"]


def test_error_prefix_matches_frontend_contract():
    # index.html replaces this exact prefix for the EN locale.
    html = (FIXTURES.parent.parent / "src/hwmon/static/index.html").read_text()
    assert ERR_AGENT_UNREACHABLE in html
