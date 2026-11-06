"""Hardware collectors, one module per subsystem, loaded dynamically.

Each module exposes ``collect(root=None) -> dict | list[dict]`` whose shape
matches :mod:`hwmon.contract`. ``root`` defaults to ``sysfs.ROOT`` (the
``HWMON_ROOT`` override), so tests and CLI runs can point at a fixture tree
without threading a parameter through every call.

Dynamic loading is deliberate
----------------------------
:data:`COLLECTORS` is a list of names, not a set of static imports. That makes
adding a collector a one-line change, but it also means a frozen build cannot
discover the modules by static analysis: PyInstaller and Nuitka both need
explicit instructions (``--include-package`` / ``--include-package-data``) or
the binary dies at runtime with ModuleNotFoundError. ``packaging/`` carries the
specs that supply them; ``tests/test_packaging.py`` asserts that the list of
names and the packaged modules agree.

Adding a collector
------------------
1. Create ``<name>.py`` with a ``collect(root=None)`` function.
2. Append the name to :data:`COLLECTORS`.
3. Add the field to :mod:`hwmon.contract` and the fixture, then read it in the
   dashboard.
"""

from __future__ import annotations

import importlib
from typing import Callable, Any

# Order matters for the CLI output and for the aggregate payload shape.
COLLECTORS: tuple[str, ...] = ("cpu", "mem", "gpu", "disk")

# Name in COLLECTORS -> key in the /api/metrics payload. The disk collector is
# singular on disk and plural in the payload; keeping the mapping explicit
# avoids guessing in three places.
PAYLOAD_KEYS = {"cpu": "cpu", "mem": "mem", "gpu": "gpus", "disk": "disks"}


class CollectorNotFound(Exception):
    """A collector name in :data:`COLLECTORS` does not resolve to a module."""


def module_name(name: str) -> str:
    """Fully qualified module path for a collector name."""
    return f"{__name__}.{name}"


def load(name: str) -> Callable[..., Any]:
    """Import a collector module by name and return its ``collect`` function.

    Raises :class:`CollectorNotFound` when the name is unknown or the module
    lacks ``collect``, so a typo in :data:`COLLECTORS` fails loudly at startup
    rather than silently dropping a metric.
    """
    if name not in COLLECTORS:
        raise CollectorNotFound(f"{name!r} is not a known collector; add it to COLLECTORS")
    try:
        mod = importlib.import_module(module_name(name))
    except ImportError as e:  # frozen builds without --include-package land here
        raise CollectorNotFound(
            f"cannot import {module_name(name)}: {e}. "
            "A frozen build needs --include-package hwmon.collectors (see packaging/)."
        ) from e
    func = getattr(mod, "collect", None)
    if func is None:
        raise CollectorNotFound(f"{module_name(name)} has no collect() function")
    return func


def collect_all(root: str | None = None, payload_keys: bool = True) -> dict:
    """Run every collector and return the aggregate mapping.

    ``payload_keys=True`` renames ``gpu``/``disk`` to ``gpus``/``disks`` so the
    result matches the ``/api/metrics`` shape; pass ``False`` for the module
    names.
    """
    out: dict = {}
    for name in COLLECTORS:
        key = PAYLOAD_KEYS.get(name, name) if payload_keys else name
        out[key] = load(name)(root)
    return out


def collect_one(name: str, root: str | None = None):
    """Run a single collector, keeping its native return type."""
    return load(name)(root)


# NOTE on naming: `importlib.import_module("hwmon.collectors.cpu")` sets the
# attribute `hwmon.collectors.cpu` to the *module*. Any function defined here
# with that same name would be silently replaced after the first load(), which
# is why the collectors are reached through load()/collect_one() and not through
# module-level aliases. The helpers below avoid the colliding names on purpose.


def parent_drives(name: str, depth: int = 0, root: str | None = None) -> list[str]:
    """Drive-name resolution for a block device (implemented in the disk module).

    Exposed as ``parent_drives`` rather than ``parents`` because the latter is a
    name the disk submodule could occupy; see the note above.
    """
    from .disk import parents

    return parents(name, depth, root)


def pcie(device_dir: str) -> dict:
    """PCIe link state for a device directory; lives in the gpu module."""
    from .gpu import pcie as _pcie

    return _pcie(device_dir)
