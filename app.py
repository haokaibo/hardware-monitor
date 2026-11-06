#!/usr/bin/env python3
"""hwmon — usage: python3 app.py agent   (monitored host, samples hardware)
                    python3 app.py web     (web client container, dashboard + proxy)
Standard library only."""
import glob, hmac, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

MODE = sys.argv[1] if len(sys.argv) > 1 else "web"
TOKEN = os.environ.get("AGENT_TOKEN", "")
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
STATE = {}
ROOT = os.environ.get("HWMON_ROOT", "")  # test-only: redirect /proc and /sys reads to a fake tree; empty in production
VERSION = os.environ.get("HWMON_VERSION") or "dev"   # version shown on the page (major.minor.build); CI fills this in


def rd(p):
    txt = read_text(p)
    return txt.strip() if txt is not None else None


def read_text(p):
    """Read a whole file; return None on failure (missing / no permission)."""
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return None


def num(p):
    return _num(rd(p))


def _num(txt):
    try:
        return int(txt)
    except (TypeError, ValueError):
        return None


def temps(dirs):
    out = {}
    for d in dirs:
        for f in glob.glob(d + "/temp*_input"):
            i = re.search(r"temp(\d+)_", f).group(1)
            v = num(f)
            if v is not None:
                out[rd(f"{d}/temp{i}_label") or f"temp{i}"] = v / 1000
    return out


_prev = {}


def cpu():
    global _prev
    cur = {}
    for l in (read_text(ROOT + "/proc/stat") or "").splitlines():
        if l.startswith("cpu"):
            p = l.split()
            v = list(map(int, p[1:9]))
            cur[p[0]] = (sum(v), v[3] + v[4])
    pct = {k: round(100 * (1 - (i - _prev[k][1]) / (t - _prev[k][0])), 1)
           for k, (t, i) in cur.items() if k in _prev and t > _prev[k][0]}
    _prev = cur
    model = next((l.split(":", 1)[1].strip() for l in (read_text(ROOT + "/proc/cpuinfo") or "").splitlines()
                  if l.startswith("model name")), "CPU")
    fr = [x for x in (num(f) for f in glob.glob(
        ROOT + "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")) if x]
    hm = [d for d in glob.glob(ROOT + "/sys/class/hwmon/hwmon*")
          if rd(d + "/name") in ("k10temp", "coretemp", "zenpower")]
    t = temps(hm)
    temp = t.get("Tctl") or t.get("Package id 0") or t.get("Tdie") or (max(t.values()) if t else None)
    return {"model": model, "threads": len(cur) - 1, "usage": pct.get("cpu"),
            "freq_mhz": round(sum(fr) / len(fr) / 1000) if fr else None,
            "load": os.getloadavg(), "temp": temp}


def mem():
    m = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in (read_text(ROOT + "/proc/meminfo") or "").splitlines()}
    return {"total": m["MemTotal"], "avail": m["MemAvailable"],
            "used": m["MemTotal"] - m["MemAvailable"],
            "swap_total": m["SwapTotal"], "swap_used": m["SwapTotal"] - m["SwapFree"]}


_GEN = {2.5: 1, 5: 2, 8: 3, 16: 4, 32: 5, 64: 6}


def _gen(txt):  # "16.0 GT/s PCIe" -> 4
    m = re.search(r"([\d.]+)\s*GT/s", txt or "")
    return _GEN.get(float(m.group(1))) if m else None


def pcie(d):
    p = {"gen": _gen(rd(d + "/current_link_speed")), "width": _num(rd(d + "/current_link_width")),
         "max_gen": _gen(rd(d + "/max_link_speed")), "max_width": _num(rd(d + "/max_link_width"))}
    if p["gen"] and p["width"]:  # GT/s = Gb/s per lane; /8 -> GB/s; x width -> one-way bandwidth (GB/s)
        p["gbps"] = round([2.5, 5, 8, 16, 32, 64][p["gen"] - 1] / 8 * p["width"])
    return p


def gpus():
    out = []
    for c in sorted(glob.glob(ROOT + "/sys/class/drm/card[0-9]*")):
        if not re.fullmatch(r"card\d+", os.path.basename(c)):
            continue
        d = c + "/device"
        tot = num(d + "/mem_info_vram_total")
        if rd(d + "/vendor") != "0x1002" or not tot:
            continue
        if tot < 2 ** 31 and not os.environ.get("INCLUDE_IGPU"):  # skip the iGPU
            continue
        pw = next((num(f) for f in glob.glob(d + "/hwmon/hwmon*/power1_average")
                   + glob.glob(d + "/hwmon/hwmon*/power1_input") if num(f)), None)
        cap = next((num(f) for f in glob.glob(d + "/hwmon/hwmon*/power1_cap") if num(f)), None)
        out.append({"name": os.environ.get("GPU_NAME") or rd(d + "/product_name")
                    or f"AMD GPU ({os.path.basename(c)})",
                    "usage": num(d + "/gpu_busy_percent"),
                    "vram_total": tot, "vram_used": num(d + "/mem_info_vram_used"),
                    "temps": temps(glob.glob(d + "/hwmon/hwmon*")),
                    "power_w": pw / 1e6 if pw else None,
                    "power_cap_w": cap / 1e6 if cap else None,
                    "pcie": pcie(d)})
    return out


def parents(n, depth=0):  # dm-/lvm/partition -> physical drive
    b = ROOT + "/sys/class/block/" + n
    if depth > 5 or not os.path.isdir(b):
        return []
    sl = os.listdir(b + "/slaves") if os.path.isdir(b + "/slaves") else []
    if sl:
        return [x for s in sl for x in parents(s, depth + 1)]
    if os.path.exists(b + "/partition"):
        return [os.path.basename(os.path.dirname(os.path.realpath(b)))]
    return [n]


def disks():
    ds = {}
    for n in sorted(os.listdir(ROOT + "/sys/block")):
        if re.match(r"(loop|ram|zd|dm-|sr|nbd|zram|md)", n):
            continue
        b = ROOT + "/sys/block/" + n
        t = temps(glob.glob(b + "/device/hwmon*") + glob.glob(b + "/device/hwmon/hwmon*")
                  + glob.glob(b + "/device/device/hwmon*"))
        kind = "NVMe" if n.startswith("nvme") else ("HDD" if rd(b + "/queue/rotational") == "1" else "SSD")
        ds[n] = {"name": n, "kind": kind, "model": rd(b + "/device/model") or "",
                 "size": (num(b + "/size") or 0) * 512,
                 "temp": t.get("Composite", next(iter(t.values()), None)), "fs": []}
    seen = set()
    for l in (read_text(ROOT + "/proc/mounts") or "").splitlines():
        dev, mnt, fst = l.split()[:3]
        if not dev.startswith("/dev/") or fst in ("squashfs", "iso9660"):
            continue
        real = os.path.realpath(dev)
        try:
            s = os.statvfs(mnt)
        except OSError:
            continue
        if real in seen or not s.f_blocks:
            continue
        seen.add(real)
        e = {"mount": mnt, "fstype": fst, "total": s.f_blocks * s.f_frsize,
             "used": (s.f_blocks - s.f_bfree) * s.f_frsize, "avail": s.f_bavail * s.f_frsize}
        for p in set(parents(os.path.basename(real))):
            if p in ds:
                ds[p]["fs"].append(e)
    return list(ds.values())


def sample(n):  # take one sample; if anything raises, nothing is written and ts stays put
    # (the frontend uses that to detect stalled data)
    STATE.update(label=os.environ.get("AGENT_LABEL", os.uname().nodename),
                 ts=time.time(), version=VERSION, cpu=cpu(), mem=mem(), gpus=gpus(), error=None)
    if n % 10 == 0:  # disks are read every 10 s to avoid waking HDDs
        STATE["disks"] = disks()


def loop():
    n = 0
    while True:
        try:
            sample(n)
        except Exception as e:
            STATE["error"] = repr(e)
        n += 1
        time.sleep(1)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self.send(200, b"ok", "text/plain")
        if path == "/api/info":   # version for the page header; no token required
            return self.send(200, json.dumps({"version": VERSION}).encode())
        if path == "/api/metrics":
            if MODE == "agent":
                if TOKEN and not hmac.compare_digest(self.headers.get("X-Token", "").encode(), TOKEN.encode()):
                    return self.send(401, b'{"error":"unauthorized"}')
                return self.send(200, json.dumps(STATE).encode())
            try:
                with urlopen(Request(os.environ["AGENT_URL"].rstrip("/") + path,
                                     headers={"X-Token": TOKEN}), timeout=3) as r:
                    return self.send(200, r.read())
            except Exception as e:
                return self.send(502, json.dumps({"error": f"无法连接 agent: {e}"}).encode())
        if path in ("/", "/favicon.svg") and MODE == "web":
            name = os.path.basename(INDEX) if path == "/" else path.lstrip("/")
            ctype = "text/html; charset=utf-8" if path == "/" else "image/svg+xml"
            try:
                with open(os.path.join(os.path.dirname(INDEX), name), "rb") as f:
                    return self.send(200, f.read(), ctype)
            except OSError:  # missing file -> 404; never let the exception drop the connection
                # (nginx would surface that as a 502)
                return self.send(404, b"{}")
        self.send(404, b"{}")


if __name__ == "__main__":
    if MODE == "agent":
        threading.Thread(target=loop, daemon=True).start()
    port = int(os.environ.get("PORT", 9101 if MODE == "agent" else 8080))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
