"""HTTP surface shared by the agent and the web client.

Routes
------
``/healthz``      plain ``ok``; no auth, used by Docker and the smoke test
``/api/info``     ``{"version": ...}``; no auth
``/api/metrics``  agent: returns the sampled state (token required when set)
                  web:   proxies the agent, 502 when it is unreachable
``/``             web only: the dashboard
``/favicon.svg``  web only

The agent never serves static files and the web client never samples hardware;
which handler is installed is decided once at startup.
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.request import Request, urlopen

PROXY_TIMEOUT = 3.0

# The frontend keys its English substitution off this prefix; keep in sync with
# the `reach` string and `localizeErr` regex in static/index.html.
ERR_AGENT_UNREACHABLE = "无法连接 agent"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def _static(name: str) -> bytes | None:
    """Read a bundled static asset; None when it is missing from the package."""
    try:
        return (resources.files("hwmon") / "static" / name).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "hwmon"
    sys_version = ""

    # Injected by make_server(). These MUST stay plain class attributes on a
    # *class* (not instance attributes): Python would otherwise bind a callable
    # stored on the instance as a method and pass `self` into it.
    config = None  # type: ignore[assignment]
    state_provider = None  # type: ignore[assignment]

    def log_message(self, *args) -> None:  # noqa: D102 - silence access logs
        pass

    # -- helpers ---------------------------------------------------------

    def send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, payload) -> None:
        self.send(code, json.dumps(payload).encode())

    def authorized(self) -> bool:
        """Constant-time token check; open when the agent has no token set."""
        expected = self.config.token
        if not expected:
            return True
        got = self.headers.get("X-Token", "")
        return hmac.compare_digest(got.encode(), expected.encode())

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?")[0]

        if path == "/healthz":
            return self.send(200, b"ok", "text/plain")
        if path == "/api/info":
            return self.send_json(200, {"version": self.config.version})
        if path == "/api/metrics":
            return self.metrics()

        if path in STATIC_FILES and not self.config.is_agent:
            name, ctype = STATIC_FILES[path]
            body = _static(name)
            # Missing asset -> 404, never a dropped connection (nginx would
            # surface that as a 502).
            return self.send(200 if body is not None else 404, body or b"{}", ctype if body else "application/json")

        self.send(404, b"{}")

    def metrics(self) -> None:
        if self.config.is_agent:
            if not self.authorized():
                return self.send_json(401, {"error": "unauthorized"})
            return self.send_json(200, self.state_provider())

        if not self.config.agent_url:
            return self.send_json(502, {"error": f"{ERR_AGENT_UNREACHABLE}: AGENT_URL is not set"})
        try:
            req = Request(
                self.config.agent_url + "/api/metrics",
                headers={"X-Token": self.config.token},
            )
            with urlopen(req, timeout=PROXY_TIMEOUT) as resp:
                return self.send(200, resp.read())
        except Exception as e:  # noqa: BLE001 - any upstream failure becomes a 502
            return self.send_json(502, {"error": f"{ERR_AGENT_UNREACHABLE}: {e}"})


def make_server(config, state_provider, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Build a server bound to the configured port.

    ``state_provider`` is a zero-argument callable returning the agent payload
    (call it per request so the state is read at request time, not at bind time);
    the web client ignores it because it proxies instead of sampling.
    """

    class Bound(Handler):
        pass

    Bound.config = config
    # staticmethod keeps `provider()` from receiving the handler as an argument
    # regardless of how it is looked up.
    Bound.state_provider = staticmethod(state_provider)
    return ThreadingHTTPServer((host, config.port), Bound)
