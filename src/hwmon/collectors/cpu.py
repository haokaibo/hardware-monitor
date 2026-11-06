"""CPU collector: usage from /proc/stat deltas, model from /proc/cpuinfo.

Public entry point is :func:`collect`, which takes an optional root so the same
code can read either the real machine or a fixture tree.
"""

from __future__ import annotations

import os

from .. import sysfs
from ..sysfs import num, rd, read_text, temps

# Previous /proc/stat snapshot, keyed by cpu label ("cpu", "cpu0", ...).
# Module state on purpose: usage is a delta, so the caller calls collect() once
# per interval and receives None on the first call.
_prev: dict[str, tuple[int, int]] = {}

# hwmon drivers that expose CPU package temperatures.
CPU_HWMON = ("k10temp", "coretemp", "zenpower")

# Label preference for the headline CPU temperature, best first.
TEMP_PREFERENCE = ("Tctl", "Package id 0", "Tdie")


def reset() -> None:
    """Drop the cached snapshot so the next call reports usage as None."""
    _prev.clear()


def _snapshot(root: str) -> dict[str, tuple[int, int]]:
    """(total jiffies, idle jiffies) per cpu line."""
    snap: dict[str, tuple[int, int]] = {}
    for line in (read_text(root + "/proc/stat") or "").splitlines():
        if line.startswith("cpu"):
            parts = line.split()
            v = list(map(int, parts[1:9]))
            # total across all states; idle = idle + iowait (columns 4 and 5)
            snap[parts[0]] = (sum(v), v[3] + v[4])
    return snap


def _model(root: str) -> str:
    return next(
        (
            line.split(":", 1)[1].strip()
            for line in (read_text(root + "/proc/cpuinfo") or "").splitlines()
            if line.startswith("model name")
        ),
        "CPU",
    )


def _average_freq_mhz(root: str) -> int | None:
    freqs = [
        f
        for f in (num(p) for p in sysfs.expand("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq", root))
        if f
    ]
    return round(sum(freqs) / len(freqs) / 1000) if freqs else None


def _temperature(root: str) -> float | None:
    dirs = [d for d in sysfs.expand("/sys/class/hwmon/hwmon*", root) if rd(d + "/name") in CPU_HWMON]
    readings = temps(dirs)
    for label in TEMP_PREFERENCE:
        if readings.get(label):
            return readings[label]
    return max(readings.values()) if readings else None


def collect(root: str | None = None) -> dict:
    """CPU usage, threads, model, frequency, load average and package temperature.

    ``root`` defaults to ``sysfs.ROOT`` (the ``HWMON_ROOT`` override).
    """
    global _prev

    base = sysfs.resolve(root)
    cur = _snapshot(base)
    pct = {
        name: round(100 * (1 - (idle - _prev[name][1]) / (total - _prev[name][0])), 1)
        for name, (total, idle) in cur.items()
        # `total > previous` skips the first sample and any counter reset
        if name in _prev and total > _prev[name][0]
    }
    _prev = cur

    return {
        "model": _model(base),
        "threads": len(cur) - 1,
        "usage": pct.get("cpu"),
        "freq_mhz": _average_freq_mhz(base),
        "load": os.getloadavg(),
        "temp": _temperature(base),
    }


# Backwards-compatible alias: the old no-argument name.
cpu = collect
