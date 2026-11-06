"""Data contract between app.py and index.html: which fields /api/metrics must carry
and which ones the frontend reads. Shared by the unit tests and the smoke test."""

TOP = ["label", "ts", "version", "cpu", "mem", "gpus", "disks", "error"]
CPU = ["model", "threads", "usage", "freq_mhz", "load", "temp"]
MEM = ["total", "used", "swap_total", "swap_used"]
GPU = ["name", "usage", "vram_total", "vram_used", "temps", "power_w", "power_cap_w", "pcie"]
PCIE = ["gen", "width"]                                   # gbps exists only when both gen and width were read
DISK = ["name", "kind", "model", "size", "temp", "fs"]
FS = ["mount", "fstype", "total", "used", "avail"]

# every field index.html reads as `.field` (catches a rename on one side only)
FRONTEND_READS = sorted(set(TOP + CPU + MEM + GPU + PCIE + DISK + FS + ["gbps"]))


def check_payload(m, require_gpu=True):
    """Return a list of problems; empty means the payload conforms. Values may be None
    (e.g. cpu.usage on the very first sample)."""
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
