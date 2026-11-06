"""Hardware collectors, one module per subsystem.

Every collector returns a plain dict whose keys match ``hwmon.contract``. They
read through :mod:`hwmon.sysfs`, so ``HWMON_ROOT`` redirects them into a fake
tree and a missing node yields ``None`` instead of raising.

Importing this package re-exports the collectors for convenience::

    from hwmon.collectors import cpu, mem, gpus, disks
"""

from __future__ import annotations

from .cpu import cpu
from .disk import disks, parents
from .gpu import gpus, pcie, pcie_generation
from .mem import mem

__all__ = ["cpu", "mem", "gpus", "disks", "parents", "pcie", "pcie_generation"]
