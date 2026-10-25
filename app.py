#!/usr/bin/env python3
"""hwmon — 用法: python3 app.py agent   (AI PC 宿主机: 硬件采集 + 可选的 llama-server 监控/请求代理)
                 python3 app.py web     (极摩客 G3 容器, 网页 + 转发)
只依赖标准库。"""
import glob, hmac, http.client, json, os, re, sqlite3, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

MODE = sys.argv[1] if len(sys.argv) > 1 else "web"
TOKEN = os.environ.get("AGENT_TOKEN", "")
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
STATE = {}


def rd(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return None


def num(p):
    try:
        return int(rd(p))
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
    for l in open("/proc/stat"):
        if l.startswith("cpu"):
            p = l.split()
            v = list(map(int, p[1:9]))
            cur[p[0]] = (sum(v), v[3] + v[4])
    pct = {k: round(100 * (1 - (i - _prev[k][1]) / (t - _prev[k][0])), 1)
           for k, (t, i) in cur.items() if k in _prev and t > _prev[k][0]}
    _prev = cur
    model = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo")
                  if l.startswith("model name")), "CPU")
    fr = [x for x in (num(f) for f in glob.glob(
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")) if x]
    hm = [d for d in glob.glob("/sys/class/hwmon/hwmon*")
          if rd(d + "/name") in ("k10temp", "coretemp", "zenpower")]
    t = temps(hm)
    temp = t.get("Tctl") or t.get("Package id 0") or t.get("Tdie") or (max(t.values()) if t else None)
    return {"model": model, "threads": len(cur) - 1, "usage": pct.get("cpu"),
            "freq_mhz": round(sum(fr) / len(fr) / 1000) if fr else None,
            "load": os.getloadavg(), "temp": temp}


def mem():
    m = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo")}
    return {"total": m["MemTotal"], "avail": m["MemAvailable"],
            "used": m["MemTotal"] - m["MemAvailable"],
            "swap_total": m["SwapTotal"], "swap_used": m["SwapTotal"] - m["SwapFree"]}


_GEN = {2.5: 1, 5: 2, 8: 3, 16: 4, 32: 5, 64: 6}


def _gen(txt):  # "16.0 GT/s PCIe" -> 4
    m = re.match(r"([\d.]+)\s*GT/s", txt or "")
    return _GEN.get(float(m.group(1))) if m else None


def pcie(d):
    return {"gen": _gen(rd(d + "/current_link_speed")), "width": num(d + "/current_link_width"),
            "max_gen": _gen(rd(d + "/max_link_speed")), "max_width": num(d + "/max_link_width")}


def gpus():
    out = []
    for c in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if not re.fullmatch(r"card\d+", os.path.basename(c)):
            continue
        d = c + "/device"
        tot = num(d + "/mem_info_vram_total")
        if rd(d + "/vendor") != "0x1002" or not tot:
            continue
        if tot < 2 ** 31 and not os.environ.get("INCLUDE_IGPU"):  # 跳过核显
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


def parents(n, depth=0):  # dm-/lvm/分区 -> 物理盘
    b = "/sys/class/block/" + n
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
    for n in sorted(os.listdir("/sys/block")):
        if re.match(r"(loop|ram|zd|dm-|sr|nbd|zram|md)", n):
            continue
        b = "/sys/block/" + n
        t = temps(glob.glob(b + "/device/hwmon*") + glob.glob(b + "/device/hwmon/hwmon*")
                  + glob.glob(b + "/device/device/hwmon*"))
        kind = "NVMe" if n.startswith("nvme") else ("HDD" if rd(b + "/queue/rotational") == "1" else "SSD")
        ds[n] = {"name": n, "kind": kind, "model": rd(b + "/device/model") or "",
                 "size": (num(b + "/size") or 0) * 512,
                 "temp": t.get("Composite", next(iter(t.values()), None)), "fs": []}
    seen = set()
    for l in open("/proc/mounts"):
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


_io_prev = None


def io():  # 所有物理盘合计的读写速率 (B/s), 来自 /proc/diskstats
    global _io_prev
    now, r, w = time.time(), 0, 0
    for l in open("/proc/diskstats"):
        p = l.split()
        if re.match(r"(loop|ram|zd|dm-|sr|nbd|zram|md)", p[2]) or not os.path.isdir("/sys/block/" + p[2]):
            continue  # 分区不在 /sys/block 下, 自然被跳过
        r += int(p[5])
        w += int(p[9])
    out = None
    if _io_prev and now > _io_prev[0]:
        dt = now - _io_prev[0]
        out = {"read_bps": max(0, (r - _io_prev[1]) * 512 / dt), "write_bps": max(0, (w - _io_prev[2]) * 512 / dt)}
    _io_prev = (now, r, w)
    return out


def loop():
    n = 0
    while True:
        try:
            STATE.update(label=os.environ.get("AGENT_LABEL", os.uname().nodename),
                         ts=time.time(), cpu=cpu(), mem=mem(), gpus=gpus(), io=io(), error=None)
            if n % 10 == 0:  # 磁盘每 10 秒读一次, 避免频繁唤醒 HDD
                STATE["disks"] = disks()
        except Exception as e:
            STATE["error"] = repr(e)
        n += 1
        time.sleep(1)

# ===================== LLM 推理监控 (设置 LLAMA_URL 后启用) =====================
# agent 同时做两件事:
#   1) 在 PROXY_PORT 上放一个透明代理, 客户端改连它, 每个请求的 timings 会被记入 SQLite (不保存对话内容)
#   2) 每秒轮询 llama-server 的 /health /slots /metrics /props, 得到状态和上下文占用
LLAMA = os.environ.get("LLAMA_URL", "").rstrip("/")
PROXY_PORT = int(os.environ.get("LLM_PROXY_PORT", 8081))
DB_PATH = os.environ.get("LLM_DB", "/var/lib/hwmon/llm.db")
KEEP_ROWS = 5000
GEN_PATHS = {"/v1/chat/completions", "/chat/completions", "/v1/completions", "/completions",
             "/completion", "/v1/messages"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
COLS = ("ts", "status", "prompt", "reused", "output", "prompt_n", "prompt_ms", "pred_n", "pred_ms", "dur", "path")
ACTIVE, CHUNKS, LAST, _aseq = {}, [0], [None], [0]
_alock, _dbl, _db = threading.Lock(), threading.Lock(), None


def db_init():
    global _db
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
    except Exception as e:
        print("LLM_DB 不可用, 改用内存库 (重启后历史会丢):", e, file=sys.stderr)
        _db = sqlite3.connect(":memory:", check_same_thread=False)
    _db.execute("CREATE TABLE IF NOT EXISTS req(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, status TEXT,"
                " prompt INT, reused INT, output INT, prompt_n INT, prompt_ms REAL, pred_n INT, pred_ms REAL,"
                " dur REAL, path TEXT)")
    _db.commit()


def save(rec):
    with _dbl:
        _db.execute("INSERT INTO req(%s) VALUES(%s)" % (",".join(COLS), ",".join("?" * len(COLS))),
                    [rec.get(k) for k in COLS])
        _db.execute("DELETE FROM req WHERE id <= (SELECT MAX(id) FROM req) - ?", (KEEP_ROWS,))
        _db.commit()


def _rate(n, ms):
    return n * 1000 / ms if n and ms and ms > 0 else None


def recent(limit):
    with _dbl:
        rows = _db.execute("SELECT %s FROM req ORDER BY id DESC LIMIT ?" % ",".join(COLS), (limit,)).fetchall()
    out = []
    for t in rows:
        d = dict(zip(COLS, t))
        d["prefill_tps"] = _rate(d.pop("prompt_n"), d.pop("prompt_ms"))
        d["decode_tps"] = _rate(d.pop("pred_n"), d.pop("pred_ms"))
        out.append(d)
    return out


def totals():
    with _dbl:
        c, since, p, r, o, pn, pms, dn, dms = _db.execute(
            "SELECT COUNT(*),MIN(ts),SUM(prompt),SUM(reused),SUM(output),SUM(prompt_n),SUM(prompt_ms),"
            "SUM(pred_n),SUM(pred_ms) FROM req").fetchone()
    return {"count": c, "since": since, "prompt": p or 0, "reused": r or 0, "output": o or 0,
            "prefill_tps": _rate(pn, pms), "decode_tps": _rate(dn, dms)}


class Tap:
    """旁路解析响应流: 只取 timings / usage / finish_reason 并数 token 块, 不保存内容"""
    LIMIT = 16 * 2 ** 20

    def __init__(self, act, stream):
        self.act, self.stream, self.buf, self.body, self.size = act, stream, b"", [], 0
        self.timings = self.usage = self.finish = None

    def feed(self, b):
        if self.stream:
            self.buf += b
            *lines, self.buf = self.buf.split(b"\n")
            for l in lines:
                self._line(l.strip())
            if len(self.buf) > 4 * 2 ** 20:
                self.buf = b""
        elif self.size < self.LIMIT:
            self.body.append(b)
            self.size += len(b)

    def done(self):
        if self.stream:
            self._line(self.buf.strip())
        else:
            try:
                self._obj(json.loads(b"".join(self.body)))
            except ValueError:
                pass

    def _line(self, l):
        if not l.startswith(b"data:"):
            return
        p = l[5:].strip()
        if p and p != b"[DONE]":
            try:
                self._obj(json.loads(p), True)
            except ValueError:
                pass

    def _obj(self, o, chunk=False):
        if not isinstance(o, dict):
            return
        if isinstance(o.get("timings"), dict):
            self.timings = o["timings"]
        if isinstance(o.get("usage"), dict):
            self.usage = o["usage"]
        chs = o.get("choices")
        ch = chs[0] if isinstance(chs, list) and chs and isinstance(chs[0], dict) else {}
        if ch.get("finish_reason"):
            self.finish = ch["finish_reason"]
        if o.get("stop") is True:  # llama.cpp 原生 /completion
            self.finish = "length" if o.get("stop_type") == "limit" else "stop"
        dl = o.get("delta")
        if isinstance(dl, dict) and dl.get("stop_reason"):  # Anthropic 风格
            self.finish = dl["stop_reason"]
        if not chunk:
            return
        d = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
        if (d.get("content") or d.get("reasoning_content") or d.get("tool_calls")
                or (not chs and o.get("content")) or o.get("type") == "content_block_delta"):
            a = self.act
            a["chunks"] += 1
            CHUNKS[0] += 1
            if a["first"] is None:
                a["first"] = time.time()


def _finish(act, tap, path, http_status, aborted, err):
    now = time.time()
    with _alock:
        ACTIVE.pop(act["id"], None)
    t = (tap.timings if tap else None) or {}
    u = (tap.usage if tap else None) or {}
    fin = tap.finish if tap else None
    cache = t.get("cache_n")
    prompt = u.get("prompt_tokens") or u.get("input_tokens") or ((t.get("prompt_n") or 0) + (cache or 0)) or None
    out = u.get("completion_tokens") or u.get("output_tokens") or t.get("predicted_n") or act["chunks"] or None
    if aborted:
        status = "aborted"
    elif err or (http_status or 200) >= 400:
        status = "error"
    else:
        status = "max_tokens" if fin in ("length", "max_tokens", "limit") else "done"
    rec = {"ts": act["start"], "status": status, "prompt": prompt, "reused": cache, "output": out,
           "prompt_n": t.get("prompt_n"), "prompt_ms": t.get("prompt_ms"),
           "pred_n": t.get("predicted_n"), "pred_ms": t.get("predicted_ms"),
           "dur": round(now - act["start"], 2), "path": path}
    LAST[0] = dict(rec, end=now)
    try:
        save(rec)
    except Exception as e:
        print("保存请求记录失败:", e, file=sys.stderr)


class Proxy(BaseHTTPRequestHandler):
    """透明转发到 LLAMA_URL (含流式), 对生成类接口旁路记录 timings"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = b""
            while True:
                n = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if n == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(n)
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _err(self, msg):
        b = json.dumps({"error": {"message": msg}}).encode()
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b)

    def handle_any(self):
        self.close_connection = True
        body = self._body()
        path = self.path.split("?")[0]
        gen = self.command == "POST" and (path.rstrip("/") or "/") in GEN_PATHS
        act = tap = None
        if gen:
            mx = None
            try:
                j = json.loads(body)
                mx = j.get("max_tokens") or j.get("n_predict") or j.get("max_completion_tokens")
            except ValueError:
                pass
            with _alock:
                _aseq[0] += 1
                act = {"id": _aseq[0], "start": time.time(), "first": None, "chunks": 0,
                       "max": mx if isinstance(mx, int) and mx > 0 else None}
                ACTIVE[act["id"]] = act
        try:
            u = urlparse(LLAMA)
            c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=3600)
            h = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            h["Connection"] = "close"
            c.request(self.command, self.path, body or None, h)
            r = c.getresponse()
        except Exception as e:
            if act:
                _finish(act, None, path, None, False, True)
            try:
                self._err("无法连接 llama-server: %r" % e)
            except OSError:
                pass
            return
        aborted = err = False
        try:
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            if r.getheader("Content-Length"):
                self.send_header("Content-Length", r.getheader("Content-Length"))
            self.send_header("Connection", "close")
            self.end_headers()
            if gen:
                tap = Tap(act, "text/event-stream" in (r.getheader("Content-Type") or ""))
            while True:
                d = r.read1(8192)
                if not d:
                    break
                if tap:
                    tap.feed(d)
                self.wfile.write(d)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            aborted = True  # 客户端中途断开; 关闭上游连接, llama-server 会停止生成
        except Exception:
            err = True
        finally:
            c.close()
            if tap:
                tap.done()
            if act:
                _finish(act, tap, path, r.status, aborted, err)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = handle_any


def _get(path, t=2.0):
    with urlopen(LLAMA + path, timeout=t) as r:
        return r.read().decode("utf-8", "replace")


def _prom(txt):
    out = {}
    for l in txt.splitlines():
        if l and l[0] != "#":
            k, _, v = l.partition(" ")
            try:
                out[k.replace("llamacpp:", "")] = float(v)
            except ValueError:
                pass
    return out


def llm_loop():
    props, slots, met, n = {}, [], {}, 0
    prev_c, prev_t, reach, loading = CHUNKS[0], time.time(), False, False
    while True:
        try:
            try:
                _get("/health")
                reach, loading = True, False
            except HTTPError:  # 503 = 模型加载中
                reach, loading = True, True
            except Exception:
                reach = False
            if reach and not loading:
                if n % 60 == 0 or not props:
                    try:
                        props = json.loads(_get("/props"))
                    except Exception:
                        pass
                if n % 2 == 0:  # /slots /metrics 会排进 llama-server 的任务队列, 降低到 2 秒一次
                    try:
                        slots = json.loads(_get("/slots"))
                    except Exception:
                        slots = []
                    try:
                        met = _prom(_get("/metrics"))
                    except Exception:
                        met = {}
            n += 1
            now = time.time()
            live = (CHUNKS[0] - prev_c) / (now - prev_t) if now > prev_t else 0.0
            prev_c, prev_t = CHUNKS[0], now
            with _alock:
                acts = [dict(a) for a in ACTIVE.values()]
            cur = max(acts, key=lambda a: a["start"]) if acts else None
            queued = int(met.get("requests_deferred") or 0)
            busy = [s for s in slots if isinstance(s, dict) and s.get("is_processing")]
            last = LAST[0]
            progress = elapsed = None
            if not reach or loading:
                state = "error"
            elif queued:
                state = "queued"
            elif cur:
                state = "generating" if any(a["first"] for a in acts) else "reading"
            elif busy:  # 有人绕过代理直连 llama-server
                nt = busy[0].get("next_token")
                nt = nt[0] if isinstance(nt, list) and nt else nt
                state = "generating" if isinstance(nt, dict) and (nt.get("n_decoded") or 0) > 0 else "reading"
            elif last and last["status"] == "error" and now - last["end"] < 20:
                state = "error"
            else:
                state = "idle"
            if cur:
                elapsed = now - cur["start"]
                if cur["first"] and cur["max"]:
                    progress = min(1.0, cur["chunks"] / cur["max"])
            # 上下文占用: 优先 /metrics 的 KV 缓存, 否则退回"上一个请求的 prompt+output"
            n_ctx = next((s.get("n_ctx") for s in slots if isinstance(s, dict) and s.get("n_ctx")), None) \
                or (props.get("default_generation_settings") or {}).get("n_ctx")
            ctx = {"used": None, "total": n_ctx, "src": None}
            kv_size = n_ctx * max(1, len(slots)) if n_ctx else None
            if kv_size and met.get("kv_cache_tokens") is not None:
                ctx.update(used=int(met["kv_cache_tokens"]), total=kv_size, src="kv")
            elif kv_size and met.get("kv_cache_usage_ratio") is not None:
                ctx.update(used=round(met["kv_cache_usage_ratio"] * kv_size), total=kv_size, src="kv")
            elif n_ctx and last and last.get("prompt"):
                ctx.update(used=last["prompt"] + (last.get("output") or 0), src="last")
            reqs = recent(15)
            STATE["llm"] = {
                "enabled": True, "reachable": reach, "loading": loading, "state": state, "queued": queued,
                "model": os.path.basename(props.get("model_path") or "") or props.get("model_alias"),
                "live_tps": round(live, 1) if reach else None, "progress": progress, "elapsed": elapsed,
                "chunks": cur["chunks"] if cur else None, "ctx": ctx, "slots": len(slots),
                "last": reqs[0] if reqs else None, "requests": reqs, "totals": totals(), "proxy_port": PROXY_PORT}
        except Exception as e:
            STATE["llm"] = {"enabled": True, "reachable": False, "state": "error", "error": repr(e),
                            "requests": [], "totals": {"count": 0}, "ctx": {}, "proxy_port": PROXY_PORT}
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
        if path in ("/api/metrics", "/api/llm/requests"):
            lim = 100
            try:
                lim = max(1, min(500, int(parse_qs(urlparse(self.path).query).get("limit", [100])[0])))
            except ValueError:
                pass
            if MODE == "agent":
                if TOKEN and not hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN):
                    return self.send(401, b'{"error":"unauthorized"}')
                if path == "/api/metrics":
                    return self.send(200, json.dumps(STATE).encode())
                if not LLAMA:
                    return self.send(404, b'{"error":"LLAMA_URL \xe6\x9c\xaa\xe9\x85\x8d\xe7\xbd\xae"}')
                return self.send(200, json.dumps({"requests": recent(lim), "total": totals()["count"]}).encode())
            try:
                url = os.environ["AGENT_URL"].rstrip("/") + path + ("?limit=%d" % lim if "llm" in path else "")
                # LLM 请求历史可能较大，且 agent 单线程 handler 忙时会排队，放宽超时避免误报断开
                with urlopen(Request(url, headers={"X-Token": TOKEN}),
                             timeout=15 if "llm" in path else 3) as r:
                    return self.send(200, r.read())
            except Exception as e:
                return self.send(502, json.dumps({"error": f"无法连接 AI PC agent: {e}"}).encode())
        if path == "/" and MODE == "web":
            with open(INDEX, "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        self.send(404, b"{}")


if __name__ == "__main__":
    if MODE == "agent":
        threading.Thread(target=loop, daemon=True).start()
        if LLAMA:
            db_init()
            threading.Thread(target=llm_loop, daemon=True).start()
            threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), Proxy).serve_forever,
                             daemon=True).start()
        else:
            STATE["llm"] = {"enabled": False}
    port = int(os.environ.get("PORT", 9101 if MODE == "agent" else 8080))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
