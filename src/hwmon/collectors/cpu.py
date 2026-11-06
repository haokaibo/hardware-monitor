"""CPU collector: usage from /proc/stat deltas, model from /proc/cpuinfo."""

from __future__ import annotations

import os

from ..sysfs import expand, num, rd, read_text, temps

# Previous /proc/stat snapshot, keyed by cpu label ("cpu", "cpu0", ...).
# Module state on purpose: usage is a delta, so the caller only has to call this
# once per interval and gets None on the first call.
_prev: dict[str, tuple[int, int]] = {}

# hwmon drivers that expose CPU package temperatures.
CPU_HWMON = ("k10temp", "coretemp", "zenpower")

# Label preference for the headline CPU temperature, best first.
TEMP_PREFERENCE = ("Tctl", "Package id 0", "Tdie")


def reset() -> None:
    """Drop the cached snapshot so the next call reports usage as None."""
    _prev.clear()


def _snapshot() -> dict[str, tuple[int, int]]:
    """(total jiffies, idle jiffies) per cpu line."""
    snap: dict[str, tuple[int, int]] = {}
    for line in (read_text(sysfs_root() + "/proc/stat") or "").splitlines():
        if line.startswith("cpu"):
            parts = line.split()
            v = list(map(int, parts[1:9]))
            # total across all states; idle = idle + iowait (columns 4 and 5)
            snap[parts[0]] = (sum(v), v[3] + v[4])
    return snap


def sysfs_root() -> str:
    """Read ``sysfs.ROOT`` at call time so tests can repoint it."""
    from .. import sysfs

    return sysfs.ROOT


def _model() -> str:
    return next(
        (
            line.split(":", 1)[1].strip()
            for line in (read_text(sysfs_root() + "/proc/cpuinfo") or "").splitlines()
            if line.startswith("model name")
        ),
        "CPU",
    )


def _average_freq_mhz() -> int | None:
    freqs = [f for f in (num(p) for p in expand("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")) if f]
    return round(sum(freqs) / len(freqs) / 1000) if freqs else None


def _temperature() -> float | None:
    dirs = [d for d in expand("/sys/class/hwmon/hwmon*") if rd(d + "/name") in CPU_HWMON]
    readings = temps(dirs)
    for label in TEMP_PREFERENCE:
        if readings.get(label):
            return readings[label]
    return max(readings.values()) if readings else None


def cpu() -> dict:
    """CPU usage, threads, model, frequency, load average and package temperature."""
    global _prev

    cur = _snapshot()
    pct = {
        name: round(100 * (1 - (idle - _prev[name][1]) / (total - _prev[name][0])), 1)
        for name, (total, idle) in cur.items()
        # `total > previous` skips the first sample and any counter reset
        if name in _prev and total > _prev[name][0]
    }
    _prev = cur

    return {
        "model": _model(),
        "threads": len(cur) - 1,
        "usage": pct.get("cpu"),
        "freq_mhz": _average_freq_mhz(),
        "load": os.getloadavg(),
        "temp": _temperature(),
    }
