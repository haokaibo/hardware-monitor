"""Shared fixtures: a fake host tree built under a temporary root.

The collectors read through ``hwmon.sysfs``, which prefixes every path with
``HWMON_ROOT``. Pointing that at a temp directory is how the whole suite runs
without real hardware — prefer this over mocking individual sysfs reads.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hwmon import sysfs

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def w(root, rel, content=""):
    """Write a file at ``root/rel``, creating parent directories."""
    p = Path(root) / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(content))
    return p


def link(root, rel, target_rel):
    """Create a symlink inside the fake tree, as sysfs does for class/ entries."""
    p = Path(root) / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(Path(root) / target_rel.lstrip("/")), p)


# Columns 4 and 5 are idle and iowait; both count as idle: total 1000, idle 800.
STAT1 = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\ncpu1 50 0 50 350 50 0 0 0 0 0\nintr 1\n"
STAT2 = "cpu  150 0 150 780 120 0 0 0 0 0\ncpu0 75 0 75 390 60 0 0 0 0 0\ncpu1 75 0 75 390 60 0 0 0 0 0\nintr 2\n"


def build_host(root, mnt):
    """Build a fake host tree: CPU / memory / 1 AMD dGPU + 1 iGPU + 3 disks."""
    w(root, "/proc/stat", STAT1)
    w(root, "/proc/cpuinfo", "processor\t: 0\nmodel name\t: Fake CPU 9000\n")
    w(
        root,
        "/proc/meminfo",
        "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n"
        "SwapTotal:       4096 kB\nSwapFree:        1024 kB\n",
    )
    for i in (0, 1):
        w(root, f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", 2000000)
    w(root, "/sys/class/hwmon/hwmon0/name", "k10temp")
    w(root, "/sys/class/hwmon/hwmon0/temp1_input", 45000)
    w(root, "/sys/class/hwmon/hwmon0/temp1_label", "Tctl")
    w(root, "/sys/class/hwmon/hwmon1/name", "nvme")  # not a CPU sensor; must be ignored
    w(root, "/sys/class/hwmon/hwmon1/temp1_input", 99000)

    # GPU: card0 = discrete; card1 = iGPU (512 MB, must be skipped); card2 = non-AMD;
    # card0-DP-1 = connector directory
    d = "/sys/class/drm/card0/device"
    w(root, d + "/vendor", "0x1002")
    w(root, d + "/mem_info_vram_total", 34342961152)
    w(root, d + "/mem_info_vram_used", 1073741824)
    w(root, d + "/gpu_busy_percent", 3)
    w(root, d + "/product_name", "Fake Radeon")
    w(root, d + "/current_link_speed", "32.0 GT/s PCIe")
    w(root, d + "/current_link_width", 16)
    w(root, d + "/max_link_speed", "32.0 GT/s PCIe")
    w(root, d + "/max_link_width", 16)
    w(root, d + "/hwmon/hwmon3/temp1_input", 29000)
    w(root, d + "/hwmon/hwmon3/temp1_label", "edge")
    w(root, d + "/hwmon/hwmon3/power1_average", 18000000)
    w(root, d + "/hwmon/hwmon3/power1_cap", 250000000)
    w(root, "/sys/class/drm/card1/device/vendor", "0x1002")
    w(root, "/sys/class/drm/card1/device/mem_info_vram_total", 536870912)
    w(root, "/sys/class/drm/card2/device/vendor", "0x10de")
    w(root, "/sys/class/drm/card2/device/mem_info_vram_total", 34342961152)
    (Path(root) / "sys/class/drm/card0-DP-1").mkdir(parents=True, exist_ok=True)

    # disks: nvme0n1 (p1 = boot, p2 -> dm-0 = data) / sda (HDD, sda1) / sdb (unmounted SSD)
    for n, rot in (("nvme0n1", 0), ("sda", 1), ("sdb", 0)):
        w(root, f"/sys/block/{n}/queue/rotational", rot)
        w(root, f"/sys/block/{n}/size", 2000000)
        w(root, f"/sys/block/{n}/device/model", f"Model-{n}")
        link(root, f"/sys/class/block/{n}", f"/sys/devices/{n}")
        (Path(root) / f"sys/devices/{n}").mkdir(parents=True, exist_ok=True)
    w(root, "/sys/block/nvme0n1/device/hwmon1/temp1_input", 30000)
    w(root, "/sys/block/nvme0n1/device/hwmon1/temp1_label", "Composite")
    w(root, "/sys/block/nvme0n1/device/hwmon1/temp2_input", 35000)
    w(root, "/sys/block/nvme0n1/device/hwmon1/temp2_label", "Sensor 1")
    w(root, "/sys/block/sda/device/hwmon0/temp1_input", 33000)
    for part, disk in (("nvme0n1p1", "nvme0n1"), ("nvme0n1p2", "nvme0n1"), ("sda1", "sda")):
        w(root, f"/sys/devices/{disk}/{part}/partition", 1)
        link(root, f"/sys/class/block/{part}", f"/sys/devices/{disk}/{part}")
    for ignored in ("loop0", "dm-0", "md0"):  # disks() must skip these in /sys/block
        w(root, f"/sys/block/{ignored}/size", 1)
    (Path(root) / "sys/class/block/dm-0/slaves").mkdir(parents=True, exist_ok=True)
    link(root, "/sys/class/block/dm-0/slaves/nvme0n1p2", "/sys/class/block/nvme0n1p2")

    for sub in ("boot", "data", "hdd", "hdd-bind", "snap", "ram"):
        (Path(mnt) / sub).mkdir(parents=True, exist_ok=True)
    w(
        root,
        "/proc/mounts",
        f"/dev/nvme0n1p1 {mnt}/boot vfat rw 0 0\n"
        f"/dev/dm-0 {mnt}/data ext4 rw 0 0\n"
        f"/dev/sda1 {mnt}/hdd ext4 rw 0 0\n"
        f"/dev/sda1 {mnt}/hdd-bind ext4 rw 0 0\n"  # same device mounted twice: counted once
        f"/dev/loop0 {mnt}/snap squashfs ro 0 0\n"  # squashfs: ignored
        f"tmpfs {mnt}/ram tmpfs rw 0 0\n",  # not under /dev/: ignored
    )


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    """A fake host tree with ``sysfs.ROOT`` pointed at it.

    Returns an object exposing ``root`` and ``mnt`` plus a ``write`` helper, and
    clears the GPU/label env vars so tests start from a known state.
    """
    root = str(tmp_path / "root")
    mnt = str(tmp_path / "mnt")
    build_host(root, mnt)
    monkeypatch.setattr(sysfs, "ROOT", root)
    for var in ("GPU_NAME", "INCLUDE_IGPU", "AGENT_LABEL", "AGENT_TOKEN", "AGENT_URL", "PORT"):
        monkeypatch.delenv(var, raising=False)

    class Host:
        pass

    host = Host()
    host.root = root
    host.mnt = mnt
    host.write = lambda rel, content="": w(root, rel, content)
    host.path = lambda rel: Path(root) / rel.lstrip("/")
    return host


class _Server:
    """Run an HTTP server in a thread; stop() is idempotent and safe at teardown."""

    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def stop(self):
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None


@pytest.fixture
def http_server():
    """Factory fixture: ``http_server(HandlerClass) -> proxy exposing .port/.stop()``."""
    started = []

    def make(handler):
        s = _Server(handler)
        started.append(s)
        return s

    yield make
    for s in started:
        s.stop()


class FakeAgent:
    """Fake agent the web client proxies to: records headers, returns a configurable body."""

    def __init__(self, status=200, payload=None, counter=False, http_server=None):
        outer = self
        self.seen = []
        self.status = status
        self.payload = payload
        self.counter = counter
        self.n = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                import json

                outer.seen.append(dict(self.headers))
                outer.n += 1
                data = dict(outer.payload or {})
                if outer.counter:
                    data["ts"] = 1000.0 + outer.n  # ts advances per request, simulating live sampling
                body = json.dumps(data).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http_server(Handler)
        self.port = self.server.port

    def stop(self):
        self.server.stop()


def free_port() -> int:
    """A port that was free a moment ago; used to simulate a downed agent."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_checkout_server(http_server, cfg, state_provider):
    """Build a server with the real (bound) handler on an ephemeral port.

    ``make_server`` needs a concrete port, but tests must not fight over one, so
    the config is given ``port=0``. The handler class is extracted from a
    throwaway bind and handed to the ``http_server`` fixture, which re-binds on
    its own ephemeral port.
    """
    from hwmon.server import make_server

    httpd = make_server(cfg, state_provider, host="127.0.0.1")
    handler = httpd.RequestHandlerClass
    httpd.server_close()
    return http_server(handler)
