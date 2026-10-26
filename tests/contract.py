"""app.py 与 index.html 之间的数据契约: /api/metrics 必须带哪些字段, 前端读取哪些字段。
单元测试与部署后冒烟测试共用。"""

TOP = ["label", "ts", "version", "cpu", "mem", "gpus", "disks", "error"]
CPU = ["model", "threads", "usage", "freq_mhz", "load", "temp"]
MEM = ["total", "used", "swap_total", "swap_used"]
GPU = ["name", "usage", "vram_total", "vram_used", "temps", "power_w", "power_cap_w", "pcie"]
PCIE = ["gen", "width"]                                   # gbps 仅在 gen、width 都读到时才有
DISK = ["name", "kind", "model", "size", "temp", "fs"]
FS = ["mount", "fstype", "total", "used", "avail"]

# index.html 里会以 `.字段名` 的形式读取的全部字段 (用于检查任何一边改名后另一边没跟上)
FRONTEND_READS = sorted(set(TOP + CPU + MEM + GPU + PCIE + DISK + FS + ["gbps"]))


def check_payload(m, require_gpu=True):
    """返回问题列表; 空列表表示符合契约。字段值允许为 None (例如首个采样的 cpu.usage)。"""
    problems = []

    def need(obj, keys, where):
        if not isinstance(obj, dict):
            problems.append(f"{where}: expected object, got {type(obj).__name__}")
            return False
        problems.extend(f"{where}: missing '{k}'" for k in keys if k not in obj)
        return True

    if not need(m, TOP, "root"):
        return problems
    for key, keys in (("cpu", CPU), ("mem", MEM)):
        if key in m:
            need(m[key], keys, key)

    gpus = m.get("gpus", [])
    if not isinstance(gpus, list):
        problems.append("gpus: expected list")
    elif gpus:
        if need(gpus[0], GPU, "gpus[0]") and "pcie" in gpus[0]:
            need(gpus[0]["pcie"], PCIE, "gpus[0].pcie")
    elif require_gpu and "gpus" in m:
        problems.append("gpus: empty")

    disks = m.get("disks", [])
    if not isinstance(disks, list):
        problems.append("disks: expected list")
    else:
        for i, d in enumerate(disks):
            if need(d, DISK, f"disks[{i}]"):
                for j, f in enumerate(d["fs"] or []):
                    need(f, FS, f"disks[{i}].fs[{j}]")
    return problems
