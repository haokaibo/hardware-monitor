"""The 1 Hz sampling loop and the shared metrics state.

Invariant that the frontend depends on: **``ts`` only advances when a sample
succeeds**. If a collector raises, nothing is written except ``error``, so the
page can tell "agent broken" apart from "values happen to be unchanged".
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from . import collectors

# Disks are read every Nth tick to avoid waking spinning drives every second.
DISK_EVERY = 10

# Submodule names, not attributes of the package: `importlib` binds
# `hwmon.collectors.cpu` to the *module*, so the callables must be resolved
# through load() at construction time rather than referenced as package names.
DEFAULT_COLLECTORS = {
    "cpu": "cpu",
    "mem": "mem",
    "gpus": "gpu",
    "disks": "disk",
}


class Sampler:
    """Owns the metrics state and the background thread that refreshes it."""

    def __init__(self, label: str, version: str, interval: float = 1.0,
                 cpu=None, mem=None, gpus=None, disks=None) -> None:
        # None means "use the loader", which keeps the dynamic import as the one
        # code path and lets tests inject fakes.
        cpu = cpu or collectors.load(DEFAULT_COLLECTORS["cpu"])
        mem = mem or collectors.load(DEFAULT_COLLECTORS["mem"])
        gpus = gpus or collectors.load(DEFAULT_COLLECTORS["gpus"])
        disks = disks or collectors.load(DEFAULT_COLLECTORS["disks"])
        self.state: dict = {}
        self._label = label
        self._version = version
        self._interval = interval
        self._cpu, self._mem, self._gpus, self._disks = cpu, mem, gpus, disks
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- one tick ---------------------------------------------------------

    def sample(self, n: int) -> None:
        """Collect once. On any exception only ``error`` is recorded."""
        try:
            cpu, mem, gpus = self._cpu(), self._mem(), self._gpus()
        except Exception as e:  # noqa: BLE001 - a broken sample must not kill the loop
            self.state["error"] = repr(e)
            return
        self.state.update(
            label=self._label,
            ts=time.time(),
            version=self._version,
            cpu=cpu,
            mem=mem,
            gpus=gpus,
            error=None,
        )
        if n % DISK_EVERY == 0:
            try:
                self.state["disks"] = self._disks()
            except Exception as e:  # noqa: BLE001 - disks are best-effort
                # Keep the last known list: an unreadable /sys/block is transient
                # (udev restart, renamed node) and clearing it would make every
                # drive vanish from the page until the next successful read.
                self.state.setdefault("disks", [])
                self.state["error"] = repr(e)

    # -- background loop --------------------------------------------------

    def run(self, clock: Callable[[float], None] = time.sleep) -> None:
        n = 0
        while not self._stop.is_set():
            self.sample(n)
            n += 1
            clock(self._interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="hwmon-sampler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self._interval + 1)
