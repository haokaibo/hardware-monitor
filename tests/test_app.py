"""Unit tests for app.py (stdlib unittest). Run: python3 -m unittest discover -s tests -v"""
import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
import app  # noqa: E402
import contract  # noqa: E402
import smoke  # noqa: E402

SAMPLE = json.loads((HERE / "fixtures" / "sample_metrics.json").read_text())


# ---------------------------------------------------------------- helpers
def w(root, rel, content=""):
    p = Path(root) / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(content))
    return p


def link(root, rel, target_rel):
    p = Path(root) / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(Path(root) / target_rel.lstrip("/")), p)


# columns 4 and 5 are idle and iowait; both count as idle: total 1000, idle 800 (700 + 100)
STAT1 = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\ncpu1 50 0 50 350 50 0 0 0 0 0\nintr 1\n"
STAT2 = "cpu  150 0 150 780 120 0 0 0 0 0\ncpu0 75 0 75 390 60 0 0 0 0 0\ncpu1 75 0 75 390 60 0 0 0 0 0\nintr 2\n"


def build_host(root, mnt):
    """Build a fake host tree under root: CPU / memory / 1 AMD dGPU + 1 iGPU + 3 disks."""
    w(root, "/proc/stat", STAT1)
    w(root, "/proc/cpuinfo", "processor\t: 0\nmodel name\t: Fake CPU 9000\n")
    w(root, "/proc/meminfo", "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n"
                              "SwapTotal:       4096 kB\nSwapFree:        1024 kB\n")
    for i in (0, 1):
        w(root, f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", 2000000)
    w(root, "/sys/class/hwmon/hwmon0/name", "k10temp")
    w(root, "/sys/class/hwmon/hwmon0/temp1_input", 45000)
    w(root, "/sys/class/hwmon/hwmon0/temp1_label", "Tctl")
    w(root, "/sys/class/hwmon/hwmon1/name", "nvme")          # not a CPU sensor; must be ignored
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

    # disks: nvme0n1 (p1 = boot, p2 -> dm-0 = data) / sda (HDD, sda1) / sdb (SATA SSD, unmounted)
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
    for ignored in ("loop0", "dm-0", "md0"):                # disks() must skip these in /sys/block
        w(root, f"/sys/block/{ignored}/size", 1)
    (Path(root) / "sys/class/block/dm-0/slaves").mkdir(parents=True, exist_ok=True)
    link(root, "/sys/class/block/dm-0/slaves/nvme0n1p2", "/sys/class/block/nvme0n1p2")

    for sub in ("boot", "data", "hdd", "hdd-bind", "snap", "ram"):
        (Path(mnt) / sub).mkdir(parents=True, exist_ok=True)
    w(root, "/proc/mounts",
      f"/dev/nvme0n1p1 {mnt}/boot vfat rw 0 0\n"
      f"/dev/dm-0 {mnt}/data ext4 rw 0 0\n"
      f"/dev/sda1 {mnt}/hdd ext4 rw 0 0\n"
      f"/dev/sda1 {mnt}/hdd-bind ext4 rw 0 0\n"             # same device mounted twice: counted once
      f"/dev/loop0 {mnt}/snap squashfs ro 0 0\n"             # squashfs: ignored
      f"tmpfs {mnt}/ram tmpfs rw 0 0\n")                     # not under /dev/: ignored


class FakeHost(unittest.TestCase):
    """Base class: build a fake host per test case and point app.ROOT at it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = str(Path(tmp.name) / "root")
        self.mnt = str(Path(tmp.name) / "mnt")
        build_host(self.root, self.mnt)
        for patcher in (mock.patch.object(app, "ROOT", self.root),
                        mock.patch.object(app, "_prev", {}),
                        mock.patch.dict(os.environ, {}, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for k in ("GPU_NAME", "INCLUDE_IGPU", "AGENT_LABEL"):
            os.environ.pop(k, None)


# ---------------------------------------------------------------- parsers
class ParserTests(unittest.TestCase):
    def test_gen_mapping(self):
        self.assertEqual(app._gen("2.5 GT/s PCIe"), 1)
        self.assertEqual(app._gen("16.0 GT/s PCIe"), 4)
        self.assertEqual(app._gen("5.0 GT/s PCIe"), 2)
        self.assertEqual(app._gen("32.0 GT/s PCIe"), 5)
        self.assertEqual(app._gen("2.5 GT/s PCIe (downgraded)"), 1)
        self.assertIsNone(app._gen("Unknown"))
        self.assertIsNone(app._gen(None))

    def test_num(self):
        self.assertEqual(app._num("42"), 42)
        self.assertIsNone(app._num(None))
        self.assertIsNone(app._num("n/a"))

    def test_pcie_bandwidth(self):
        with tempfile.TemporaryDirectory() as d:
            for speed, width, gen, gbps in (("16.0 GT/s PCIe", 16, 4, 32), ("32.0 GT/s PCIe", 16, 5, 64),
                                            ("8.0 GT/s PCIe", 4, 3, 4)):
                w(d, "current_link_speed", speed)
                w(d, "current_link_width", width)
                p = app.pcie(d)
                self.assertEqual((p["gen"], p["width"], p["gbps"]), (gen, width, gbps), speed)

    def test_pcie_unknown_has_no_bandwidth(self):
        with tempfile.TemporaryDirectory() as d:
            w(d, "current_link_speed", "Unknown")
            w(d, "current_link_width", 16)
            p = app.pcie(d)
            self.assertIsNone(p["gen"])
            self.assertNotIn("gbps", p)
        self.assertEqual(app.pcie("/nonexistent")["gen"], None)       # a missing directory must not raise

    def test_temps(self):
        with tempfile.TemporaryDirectory() as d:
            w(d, "temp1_input", 41500)
            w(d, "temp1_label", "Tctl")
            w(d, "temp2_input", 50000)                    # no label -> fall back to temp2
            w(d, "temp3_input", "garbage")                # unparsable -> ignored
            self.assertEqual(app.temps([d]), {"Tctl": 41.5, "temp2": 50.0})
        self.assertEqual(app.temps(["/nonexistent"]), {})


# ---------------------------------------------------------------- collectors (fake sysfs)
class CollectorTests(FakeHost):
    def test_cpu(self):
        first = app.cpu()
        self.assertIsNone(first["usage"])                 # no previous sample yet, so usage is unknown
        w(self.root, "/proc/stat", STAT2)
        c = app.cpu()
        self.assertEqual(c["usage"], 50.0)                # total +200, idle (idle+iowait) +100
        self.assertEqual(c["threads"], 2)
        self.assertEqual(c["model"], "Fake CPU 9000")
        self.assertEqual(c["freq_mhz"], 2000)
        self.assertEqual(c["temp"], 45.0)                 # k10temp Tctl, not the nvme 99 C reading

    def test_cpu_temp_fallback_to_max(self):
        (Path(self.root) / "sys/class/hwmon/hwmon0/temp1_label").unlink()
        w(self.root, "/sys/class/hwmon/hwmon0/temp2_input", 61000)
        self.assertEqual(app.cpu()["temp"], 61.0)

    def test_mem(self):
        m = app.mem()
        self.assertEqual(m["total"], 16384000 * 1024)
        self.assertEqual(m["used"], (16384000 - 8192000) * 1024)
        self.assertEqual(m["swap_used"], (4096 - 1024) * 1024)

    def test_gpus_picks_only_discrete_amd(self):
        g = app.gpus()
        self.assertEqual(len(g), 1)                       # iGPU, non-AMD and connector dirs all excluded
        g = g[0]
        self.assertEqual(g["name"], "Fake Radeon")
        self.assertEqual((g["usage"], g["vram_used"]), (3, 1073741824))
        self.assertEqual(g["temps"], {"edge": 29.0})
        self.assertEqual((g["power_w"], g["power_cap_w"]), (18.0, 250.0))
        self.assertEqual((g["pcie"]["gen"], g["pcie"]["width"], g["pcie"]["gbps"]), (5, 16, 64))

    def test_gpus_env_overrides(self):
        os.environ["GPU_NAME"] = "My Card"
        os.environ["INCLUDE_IGPU"] = "1"
        names = [g["name"] for g in app.gpus()]
        self.assertEqual(names[0], "My Card")
        self.assertEqual(len(names), 2)                   # with INCLUDE_IGPU the iGPU counts too

    def test_parents(self):
        self.assertEqual(app.parents("nvme0n1p1"), ["nvme0n1"])      # partition -> whole drive
        self.assertEqual(app.parents("sda"), ["sda"])                # whole drive -> itself
        self.assertEqual(app.parents("dm-0"), ["nvme0n1"])           # LVM/dm -> slaves -> partition -> drive
        self.assertEqual(app.parents("does-not-exist"), [])

    def test_disks(self):
        ds = {d["name"]: d for d in app.disks()}
        self.assertEqual(set(ds), {"nvme0n1", "sda", "sdb"})         # loop / dm / md are skipped
        self.assertEqual((ds["nvme0n1"]["kind"], ds["sda"]["kind"], ds["sdb"]["kind"]), ("NVMe", "HDD", "SSD"))
        self.assertEqual(ds["nvme0n1"]["temp"], 30.0)                # prefer Composite
        self.assertEqual(ds["sda"]["temp"], 33.0)                    # without Composite, take the first
        self.assertIsNone(ds["sdb"]["temp"])
        self.assertEqual(ds["nvme0n1"]["size"], 2000000 * 512)
        self.assertEqual(sorted(f["mount"] for f in ds["nvme0n1"]["fs"]), sorted([f"{self.mnt}/boot", f"{self.mnt}/data"]))
        self.assertEqual([f["mount"] for f in ds["sda"]["fs"]], [f"{self.mnt}/hdd"])   # duplicate mount counted once
        self.assertEqual(ds["sdb"]["fs"], [])
        f = ds["sda"]["fs"][0]
        self.assertEqual(f["fstype"], "ext4")
        self.assertGreater(f["total"], 0)
        self.assertGreaterEqual(f["total"], f["used"])


class SampleTests(FakeHost):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(app, "STATE", {})
        p.start()
        self.addCleanup(p.stop)

    def test_sample_matches_contract_and_is_json(self):
        app.sample(0)
        self.assertEqual(contract.check_payload(app.STATE), [])
        json.dumps(app.STATE)                              # must be serializable (e.g. getloadavg tuples)
        self.assertIsNone(app.STATE["error"])

    def test_disks_only_refreshed_every_10_samples(self):
        app.sample(0)
        app.STATE["disks"] = "sentinel"
        app.sample(1)
        self.assertEqual(app.STATE["disks"], "sentinel")
        app.sample(10)
        self.assertIsInstance(app.STATE["disks"], list)

    def test_failed_sample_does_not_advance_ts(self):
        app.sample(0)
        ts = app.STATE["ts"]
        (Path(self.root) / "proc/meminfo").unlink()
        with self.assertRaises(KeyError):
            app.sample(1)
        self.assertEqual(app.STATE["ts"], ts)              # ts must not advance so the UI can detect staleness

    def test_label_from_env(self):
        os.environ["AGENT_LABEL"] = "host-a"
        app.sample(0)
        self.assertEqual(app.STATE["label"], "host-a")


# ---------------------------------------------------------------- data contract
class ContractTests(unittest.TestCase):
    def test_fixture_matches_contract(self):
        self.assertEqual(contract.check_payload(SAMPLE), [])

    def test_check_payload_detects_problems(self):
        bad = json.loads(json.dumps(SAMPLE))
        del bad["cpu"]["freq_mhz"]
        del bad["gpus"][0]["pcie"]
        del bad["disks"][0]["fs"][0]["avail"]
        problems = contract.check_payload(bad)
        self.assertIn("cpu: missing 'freq_mhz'", problems)
        self.assertIn("gpus[0]: missing 'pcie'", problems)
        self.assertIn("disks[0].fs[0]: missing 'avail'", problems)
        self.assertEqual(contract.check_payload({}), [f"root: missing '{k}'" for k in contract.TOP])

    def test_frontend_reads_every_contract_field(self):
        html = (REPO / "index.html").read_text(encoding="utf-8")
        missing = [f for f in contract.FRONTEND_READS if not re.search(r"\." + f + r"\b", html)]
        self.assertEqual(missing, [], "index.html no longer reads these fields (renamed on one side?)")


# ---------------------------------------------------------------- HTTP
class Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeAgent:
    """Fake agent the web mode proxies to: records request headers, returns a configurable status and body."""

    def __init__(self, status=200, payload=None, counter=False):
        outer = self
        self.seen, self.status, self.payload, self.counter, self.n = [], status, payload, counter, 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append(dict(self.headers))
                outer.n += 1
                data = dict(outer.payload or {})
                if outer.counter:
                    data["ts"] = 1000.0 + outer.n          # ts advances per request, simulating live sampling
                body = json.dumps(data).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = Server(Handler)
        self.port = self.server.port

    def stop(self):
        self.server.stop()


class AgentHttpTests(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(app, "MODE", "agent"), mock.patch.object(app, "TOKEN", ""),
                        mock.patch.object(app, "STATE", {"cpu": {"usage": 1}, "ts": 1.0})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.srv = Server(app.H)
        self.addCleanup(self.srv.stop)

    def test_healthz(self):
        st, _, body = get(self.srv.port, "/healthz")
        self.assertEqual((st, body), (200, b"ok"))

    def test_metrics_without_token_configured(self):
        st, h, body = get(self.srv.port, "/api/metrics")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["ts"], 1.0)
        self.assertEqual(h["cache-control"], "no-store")
        self.assertIn("application/json", h["content-type"])

    def test_metrics_token_enforced(self):
        with mock.patch.object(app, "TOKEN", "s3cret"):
            for hdrs in ({}, {"X-Token": "wrong"}, {"X-Token": "é"}, {"X-Token": ""}):
                st, _, body = get(self.srv.port, "/api/metrics", hdrs)
                self.assertEqual(st, 401, hdrs)
                self.assertEqual(json.loads(body), {"error": "unauthorized"})
            st, _, _ = get(self.srv.port, "/api/metrics", {"X-Token": "s3cret"})
            self.assertEqual(st, 200)

    def test_healthz_needs_no_token(self):
        with mock.patch.object(app, "TOKEN", "s3cret"):
            self.assertEqual(get(self.srv.port, "/healthz")[0], 200)

    def test_agent_does_not_serve_pages(self):
        self.assertEqual(get(self.srv.port, "/")[0], 404)
        self.assertEqual(get(self.srv.port, "/nope")[0], 404)


class WebHttpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.index = w(self.dir, "index.html", '<html><div id="cpu-rows"></div></html>')
        w(self.dir, "favicon.svg", "<svg/>")
        w(self.dir, "secret.txt", "do not serve")
        self.agent = FakeAgent(payload=SAMPLE, counter=True)
        self.addCleanup(self.agent.stop)
        for patcher in (mock.patch.object(app, "MODE", "web"), mock.patch.object(app, "TOKEN", "tok"),
                        mock.patch.object(app, "INDEX", str(self.index)),
                        mock.patch.dict(os.environ, {"AGENT_URL": f"http://127.0.0.1:{self.agent.port}/"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.srv = Server(app.H)
        self.addCleanup(self.srv.stop)

    def test_serves_index_and_favicon(self):
        st, h, body = get(self.srv.port, "/")
        self.assertEqual(st, 200)
        self.assertEqual(h["content-type"], "text/html; charset=utf-8")
        self.assertEqual(body, self.index.read_bytes())
        self.assertEqual(get(self.srv.port, "/?x=1")[0], 200)                       # query string ignored
        st, h, _ = get(self.srv.port, "/favicon.svg")
        self.assertEqual((st, h["content-type"]), (200, "image/svg+xml"))

    def test_missing_files_return_404_not_dropped_connection(self):
        self.index.unlink()
        (self.dir / "favicon.svg").unlink()
        self.assertEqual(get(self.srv.port, "/")[0], 404)         # a missing file used to surface as nginx 502
        self.assertEqual(get(self.srv.port, "/favicon.svg")[0], 404)

    def test_only_whitelisted_paths_are_served(self):
        for path in ("/secret.txt", "/app.py", "/index.html", "/../secret.txt", "/%2e%2e/secret.txt", "/nope"):
            self.assertEqual(get(self.srv.port, path)[0], 404, path)

    def test_proxies_metrics_and_forwards_token(self):
        st, _, body = get(self.srv.port, "/api/metrics")
        self.assertEqual(st, 200)
        self.assertEqual(contract.check_payload(json.loads(body)), [])
        self.assertEqual(self.agent.seen[-1].get("X-Token"), "tok")

    def test_agent_error_becomes_502_with_message(self):
        self.agent.status = 401
        st, _, body = get(self.srv.port, "/api/metrics")
        self.assertEqual(st, 502)
        self.assertIn("401", json.loads(body)["error"])

    def test_agent_down_becomes_502(self):
        with mock.patch.dict(os.environ, {"AGENT_URL": f"http://127.0.0.1:{free_port()}"}):
            st, _, body = get(self.srv.port, "/api/metrics")
        self.assertEqual(st, 502)
        self.assertTrue(json.loads(body)["error"].startswith("无法连接 agent"))   # the frontend keys its English substitution off this prefix

    def test_missing_agent_url_becomes_502(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(get(self.srv.port, "/api/metrics")[0], 502)


# ---------------------------------------------------------------- the smoke script itself
class SmokeScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.index = w(self.dir, "index.html", '<html><div id="cpu-rows"></div></html>')
        w(self.dir, "favicon.svg", "<svg/>")
        self.agent = FakeAgent(payload=SAMPLE, counter=True)
        self.addCleanup(self.agent.stop)
        for patcher in (mock.patch.object(app, "MODE", "web"), mock.patch.object(app, "TOKEN", ""),
                        mock.patch.object(app, "INDEX", str(self.index)),
                        mock.patch.dict(os.environ, {"AGENT_URL": f"http://127.0.0.1:{self.agent.port}"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.srv = Server(app.H)
        self.addCleanup(self.srv.stop)
        self.base = f"http://127.0.0.1:{self.srv.port}"
        self.log = []

    def run_smoke(self, **kw):
        kw = {"retries": 2, "delay": 0.05, "fresh_wait": 0.05, "out": self.log.append, **kw}
        return smoke.run(self.base, **kw)

    def test_passes_on_healthy_deploy(self):
        self.assertEqual(self.run_smoke(expect_index=str(self.index)), 0, "\n".join(self.log))
        self.assertTrue(any("matches" in l for l in self.log))

    def test_detects_stale_index(self):
        other = w(self.dir, "new_index.html", "<html>newer</html>")
        self.assertEqual(self.run_smoke(expect_index=str(other)), 1)
        self.assertTrue(any("differs" in l for l in self.log))

    def test_detects_stuck_agent(self):
        self.agent.counter = False                          # ts stops advancing
        self.assertEqual(self.run_smoke(), 1)
        self.assertTrue(any("not advancing" in l for l in self.log))

    def test_detects_contract_break(self):
        broken = json.loads(json.dumps(SAMPLE))
        del broken["mem"]["used"]
        self.agent.payload = broken
        self.assertEqual(self.run_smoke(), 1)
        self.assertTrue(any("mem: missing 'used'" in l for l in self.log))

    def test_detects_unreachable_agent(self):
        with mock.patch.dict(os.environ, {"AGENT_URL": f"http://127.0.0.1:{free_port()}"}):
            self.assertEqual(self.run_smoke(), 1)

    def test_detects_missing_page(self):
        self.index.unlink()
        self.assertEqual(self.run_smoke(), 1)

    def test_missing_favicon_only_warns(self):
        (self.dir / "favicon.svg").unlink()
        self.assertEqual(self.run_smoke(), 0, "\n".join(self.log))
        self.assertTrue(any(l.startswith("WARN") and "favicon" in l for l in self.log))

    def test_server_down(self):
        self.srv.stop()
        self.assertEqual(self.run_smoke(), 1)
        self.assertTrue(any("not ready" in l for l in self.log))
        self.srv = Server(app.H)                            # keeps the addCleanup stop() valid


if __name__ == "__main__":
    unittest.main()
