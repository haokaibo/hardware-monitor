"""Memory collector: /proc/meminfo totals, usage and swap."""

from __future__ import annotations

from .. import sysfs
from ..sysfs import read_text

# /proc/meminfo reports kB; everything downstream works in bytes.
SCALE = 1024


def mem() -> dict:
    """Memory and swap in bytes.

    Raises ``KeyError`` when a required field is missing; the sampler treats that
    as a failed tick so ``ts`` does not advance.
    """
    fields = {
        line.split(":")[0]: int(line.split()[1]) * SCALE
        for line in (read_text(sysfs.ROOT + "/proc/meminfo") or "").splitlines()
    }
    return {
        "total": fields["MemTotal"],
        "avail": fields["MemAvailable"],
        "used": fields["MemTotal"] - fields["MemAvailable"],
        "swap_total": fields["SwapTotal"],
        "swap_used": fields["SwapTotal"] - fields["SwapFree"],
    }
