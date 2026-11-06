"""Tolerant sysfs/procfs readers.

Every helper here returns ``None`` instead of raising when a node is missing,
unreadable or unparsable. Collectors are built on top of these so a single
absent sensor degrades that one field rather than failing the whole sample.

``HWMON_ROOT`` prefixes every absolute path so the test suite can point the
readers at a fake tree instead of the real ``/proc`` and ``/sys``.
"""

from __future__ import annotations

import glob
import os
import re

# Test-only: redirect /proc and /sys reads into a fake tree. Empty in production.
# Collectors take an explicit ``root`` argument; when it is omitted they fall
# back to this value, so `HWMON_ROOT=/fake/tree hwmon verify` and the test
# fixture keep working without threading a parameter through every call.
ROOT = os.environ.get("HWMON_ROOT", "")


def resolve(root: str | None = None) -> str:
    """Pick the tree to read: the explicit argument, else ``ROOT``.

    An empty string means "real /proc and /sys".
    """
    return ROOT if root is None else root


def read_text(path: str) -> str | None:
    """Read a whole file; return None on failure (missing / no permission)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def rd(path: str) -> str | None:
    """Read a single value with surrounding whitespace stripped."""
    txt = read_text(path)
    return txt.strip() if txt is not None else None


def num(path: str) -> int | None:
    """Read an integer value, or None when it is absent or not a number."""
    return to_num(rd(path))


def to_num(txt: str | None) -> int | None:
    try:
        return int(txt)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def temps(dirs: list[str]) -> dict[str, float]:
    """Collect every ``temp*_input`` under the given hwmon directories.

    Keys come from the sibling ``temp*_label`` file, falling back to
    ``tempN``. Values are degrees Celsius (hwmon exposes millidegrees).
    """
    out: dict[str, float] = {}
    for d in dirs:
        for f in glob.glob(d + "/temp*_input"):
            m = re.search(r"temp(\d+)_", f)
            if not m:
                continue
            i = m.group(1)
            v = num(f)
            if v is not None:
                out[rd(f"{d}/temp{i}_label") or f"temp{i}"] = v / 1000
    return out


def expand(pattern: str, root: str | None = None) -> list[str]:
    """Glob a sysfs-style absolute path with the resolved root applied.

    Returns root-prefixed absolute paths, already sorted.
    """
    return sorted(glob.glob(resolve(root) + pattern))


def glob_abs(pattern: str) -> list[str]:
    """Glob an already-ROOT-prefixed pattern (no second prefix applied)."""
    return sorted(glob.glob(pattern))
