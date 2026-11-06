"""GPU collector: AMD cards exposed through /sys/class/drm.

Only ``vendor == 0x1002`` is supported. The CPU iGPU is hidden unless
``INCLUDE_IGPU=1`` because it usually reports a small, misleading VRAM figure.
"""

from __future__ import annotations

import os
import re

from .. import sysfs
from ..sysfs import expand, glob_abs, num, rd, temps, to_num

AMD_VENDOR = "0x1002"

# Any card with less VRAM than this is treated as an iGPU and skipped by default.
IGPU_VRAM_BYTES = 2**31

# GT/s per lane to PCIe generation.
_GEN = {2.5: 1, 5: 2, 8: 3, 16: 4, 32: 5, 64: 6}

# One-way bandwidth in GB/s per lane, indexed by generation - 1.
_LANE_GBPS = [2.5, 5, 8, 16, 32, 64]

_CARD = re.compile(r"card\d+")


def pcie_generation(link_speed: str | None) -> int | None:
    """Map a link speed string such as '16.0 GT/s PCIe' to a PCIe generation."""
    m = re.search(r"([\d.]+)\s*GT/s", link_speed or "")
    return _GEN.get(float(m.group(1))) if m else None


def pcie(device_dir: str) -> dict:
    """Current link state and one-way bandwidth for a GPU."""
    link = {
        "gen": pcie_generation(rd(device_dir + "/current_link_speed")),
        "width": to_num(rd(device_dir + "/current_link_width")),
        "max_gen": pcie_generation(rd(device_dir + "/max_link_speed")),
        "max_width": to_num(rd(device_dir + "/max_link_width")),
    }
    if link["gen"] and link["width"]:
        # GT/s = Gb/s per lane; /8 -> GB/s; times width -> one-way GB/s
        link["gbps"] = round(_LANE_GBPS[link["gen"] - 1] / 8 * link["width"])
    return link


def _first_number(patterns: list[str]) -> int | None:
    """First truthy integer across the patterns (absolute, ROOT already applied)."""
    for pattern in patterns:
        for path in glob_abs(pattern):
            value = num(path)
            if value:
                return value
    return None


def _hwmon_dirs(card_dir: str) -> list[str]:
    """hwmon directories belonging to a card, given a ROOT-prefixed card path.

    ``card_dir`` comes from :func:`expand`, which has already applied
    ``sysfs.ROOT``, so this must not use ``expand`` again.
    """
    return glob_abs(card_dir + "/device/hwmon/hwmon*")


def gpus() -> list[dict]:
    """Every supported AMD GPU, in card order."""
    found: list[dict] = []
    for card_dir in expand("/sys/class/drm/card[0-9]*"):
        card = os.path.basename(card_dir)
        if not _CARD.fullmatch(card):
            continue  # skip connector dirs such as card0-DP-1

        # expand() gives ROOT-prefixed paths; work in absolute form throughout so
        # nothing gets the root applied twice.
        device = card_dir + "/device"
        vram_total = num(device + "/mem_info_vram_total")
        if rd(device + "/vendor") != AMD_VENDOR or not vram_total:
            continue
        if vram_total < IGPU_VRAM_BYTES and not os.environ.get("INCLUDE_IGPU"):
            continue

        hwmon = device + "/hwmon/hwmon*"
        power = _first_number([hwmon + "/power1_average", hwmon + "/power1_input"])
        cap = _first_number([hwmon + "/power1_cap"])

        found.append(
            {
                "name": os.environ.get("GPU_NAME") or rd(device + "/product_name") or f"AMD GPU ({card})",
                "usage": num(device + "/gpu_busy_percent"),
                "vram_total": vram_total,
                "vram_used": num(device + "/mem_info_vram_used"),
                "temps": temps(_hwmon_dirs(card_dir)),
                "power_w": power / 1e6 if power else None,
                "power_cap_w": cap / 1e6 if cap else None,
                "pcie": pcie(device),
            }
        )
    return found
