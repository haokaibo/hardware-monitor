"""Data contracts: agent <-> dashboard, and the pro-tier records.

Three contracts live here because they all have the same failure mode — a field
renamed on one side only, which no unit test would otherwise catch:

* ``/api/metrics`` (TOP/CPU/MEM/GPU/PCIE/DISK/FS) — agent to ``index.html``.
* ``HISTORY`` — the SQLite columns in ``hwmon.pro.history``. The schema and the
  ``SampleRow`` dataclass must agree, or a recorded sample silently loses a
  metric.
* ``ALERT`` — the dict produced by ``hwmon.pro.alerts.Alert.as_dict``, which is
  what any consumer (webhook, UI, log shipper) receives.

Shared by the test suite, the post-deploy smoke test and the pro tests.
"""

from __future__ import annotations

TOP = ["label", "ts", "version", "cpu", "mem", "gpus", "disks", "error"]
CPU = ["model", "threads", "usage", "freq_mhz", "load", "temp"]
MEM = ["total", "used", "swap_total", "swap_used"]
GPU = ["name", "usage", "vram_total", "vram_used", "temps", "power_w", "power_cap_w", "pcie"]
PCIE = ["gen", "width"]  # gbps exists only when both gen and width were read
DISK = ["name", "kind", "model", "size", "temp", "fs"]
FS = ["mount", "fstype", "total", "used", "avail"]

# Pro tier: columns stored per history row (sorted to match the SQL DDL order).
HISTORY = ["ts", "cpu_pct", "cpu_temp", "mem_pct", "gpu_pct", "gpu_temp", "gpu_vram"]

# Pro tier: the alert dict handed to a sink. `value` and `rule` are what a
# consumer keys on, so they are part of the contract, not extras.
ALERT = ["rule", "level", "message", "ts", "value"]

# Levels an alert may carry, worst last. Anything outside this set is a bug.
ALERT_LEVELS = ["ok", "warning", "critical"]

# Every field index.html reads as `.field` (catches a rename on one side only).
FRONTEND_READS = sorted(set(TOP + CPU + MEM + GPU + PCIE + DISK + FS + ["gbps"]))


def check_history_row(row: dict) -> list[str]:
    """Problems with a history row (empty list means it conforms)."""
    missing = [k for k in HISTORY if k not in row]
    problems = [f"history: missing '{k}'" for k in missing]
    ts = row.get("ts")
    if not isinstance(ts, (int, float)):
        problems.append(f"history: ts must be a number, got {type(ts).__name__}")
    return problems


def check_alert(alert: dict) -> list[str]:
    """Problems with an alert dict (empty list means it conforms)."""
    problems = [f"alert: missing '{k}'" for k in ALERT if k not in alert]
    level = alert.get("level")
    if level not in ALERT_LEVELS:
        problems.append(f"alert: level {level!r} not in {ALERT_LEVELS}")
    if "ts" in alert and not isinstance(alert["ts"], (int, float)):
        problems.append(f"alert: ts must be a number, got {type(alert['ts']).__name__}")
    if "message" in alert and not isinstance(alert["message"], str):
        problems.append("alert: message must be a string")
    return problems


def check_payload(m: object, require_gpu: bool = True) -> list[str]:
    """Return a list of problems; empty means the payload conforms.

    Values may be ``None`` (for example ``cpu.usage`` on the very first sample).
    """
    problems: list[str] = []

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

    gpus = m.get("gpus", [])  # type: ignore[union-attr]
    if not isinstance(gpus, list):
        problems.append("gpus: expected list")
    elif gpus:
        if need(gpus[0], GPU, "gpus[0]") and "pcie" in gpus[0]:
            need(gpus[0]["pcie"], PCIE, "gpus[0].pcie")
    elif require_gpu and "gpus" in m:  # type: ignore[operator]
        problems.append("gpus: empty")

    disks = m.get("disks", [])  # type: ignore[union-attr]
    if not isinstance(disks, list):
        problems.append("disks: expected list")
    else:
        for i, d in enumerate(disks):
            if need(d, DISK, f"disks[{i}]"):
                for j, f in enumerate(d["fs"] or []):
                    need(f, FS, f"disks[{i}].fs[{j}]")

    return problems
