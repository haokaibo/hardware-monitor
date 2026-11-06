"""Disk collector: physical drives plus the filesystems mounted from them.

Two things make this less trivial than it looks:

* LVM, dm-crypt and partitions must be folded back onto the physical drive, or
  the same capacity shows up several times.
* ``statvfs`` is called on the mount point, but the drive it belongs to is found
  through ``/sys/class/block/*/slaves`` and ``partition`` markers, so the
  numbers come from the kernel rather than from parsing device names.
"""

from __future__ import annotations

import os
import re

from .. import sysfs
from ..sysfs import num, rd, read_text, temps

# Pseudo devices that never represent real storage.
_SKIP = re.compile(r"(loop|ram|zd|dm-|sr|nbd|zram|md)")

# Filesystem types that are not worth showing as storage.
_SKIP_FS = ("squashfs", "iso9660")

# sysfs nests the hwmon node differently per driver; try all known layouts.
_HWMON_LAYOUTS = (
    "/sys/block/{name}/device/hwmon*",
    "/sys/block/{name}/device/hwmon/hwmon*",
    "/sys/block/{name}/device/device/hwmon*",
)


def parents(name: str, depth: int = 0) -> list[str]:
    """Resolve dm/LVM/partition names down to the physical drive name.

    Returns an empty list when the node does not exist, so callers can ignore
    unknown devices instead of raising.
    """
    base = sysfs.ROOT + "/sys/class/block/" + name
    if depth > 5 or not os.path.isdir(base):
        return []
    slaves = os.listdir(base + "/slaves") if os.path.isdir(base + "/slaves") else []
    if slaves:
        return [x for s in slaves for x in parents(s, depth + 1)]
    if os.path.exists(base + "/partition"):
        return [os.path.basename(os.path.dirname(os.path.realpath(base)))]
    return [name]


def _kind(name: str, rotational: str | None) -> str:
    if name.startswith("nvme"):
        return "NVMe"
    return "HDD" if rotational == "1" else "SSD"


def _drives() -> dict[str, dict]:
    """Physical drives keyed by kernel name, with no filesystem information yet."""
    block_dir = sysfs.ROOT + "/sys/block"
    names = sorted(os.listdir(block_dir)) if os.path.isdir(block_dir) else []

    found: dict[str, dict] = {}
    for name in names:
        if _SKIP.match(name):
            continue
        base = block_dir + "/" + name
        readings: dict[str, float] = {}
        for layout in _HWMON_LAYOUTS:
            readings.update(temps(sysfs.expand(layout.format(name=name))))
        found[name] = {
            "name": name,
            "kind": _kind(name, rd(base + "/queue/rotational")),
            "model": rd(base + "/device/model") or "",
            "size": (num(base + "/size") or 0) * 512,
            # NVMe exposes "Composite"; SATA drives provide a single unlabelled sensor
            "temp": readings.get("Composite", next(iter(readings.values()), None)),
            "fs": [],
        }
    return found


def _filesystems() -> list[tuple[str, dict]]:
    """(real device path, filesystem entry) for every mountable /dev/ entry."""
    out: list[tuple[str, dict]] = []
    for line in (read_text(sysfs.ROOT + "/proc/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fstype = parts[:3]
        if not device.startswith("/dev/") or fstype in _SKIP_FS:
            continue
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        if not st.f_blocks:
            continue
        out.append(
            (
                os.path.realpath(device),
                {
                    "mount": mount,
                    "fstype": fstype,
                    "total": st.f_blocks * st.f_frsize,
                    "used": (st.f_blocks - st.f_bfree) * st.f_frsize,
                    "avail": st.f_bavail * st.f_frsize,
                },
            )
        )
    return out


def disks() -> list[dict]:
    """Physical drives, each carrying the filesystems mounted from it."""
    found = _drives()

    seen: set[str] = set()
    for real, entry in _filesystems():
        if real in seen:
            continue  # the same device mounted twice (bind mounts) counts once
        seen.add(real)
        for drive in set(parents(os.path.basename(real))):
            if drive in found:
                found[drive]["fs"].append(entry)

    return list(found.values())
