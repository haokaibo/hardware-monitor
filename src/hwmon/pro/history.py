"""SQLite-backed history for trends that outlive a browser session.

The open dashboard keeps one hour of samples in a ring buffer in JavaScript.
This store is for the pro tier: durable samples, so a chart can span days and
survive a page reload or a different browser.

Design notes
------------
* One table, one row per sample, with the headline scalars only. Full payloads
  are not stored: they change shape and would bloat the file.
* Writes go through a single connection guarded by a lock, because the sampler
  thread is the only writer but readers may come from HTTP handlers.
* ``prune`` drops old rows; call it periodically rather than on every insert.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .. import contract

# The stored columns come from the shared contract, so adding a metric in one
# place updates the DDL, the row dataclass and the readers together.
COLUMNS = list(contract.HISTORY)

_DDL_TYPES = {name: ("REAL PRIMARY KEY" if name == "ts" else "REAL") for name in COLUMNS}
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS samples (\n"
    + ",\n".join(f"    {name} {_DDL_TYPES[name]}" for name in COLUMNS)
    + "\n);\n"
    + "CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);\n"
)

# Fails at import if the contract and this module disagree about the shape.
assert COLUMNS[0] == "ts", "ts must be the first history column"
assert len(COLUMNS) == len(set(COLUMNS)), "duplicate column in contract.HISTORY"


@dataclass(frozen=True)
class SampleRow:
    ts: float
    cpu_pct: float | None
    cpu_temp: float | None
    mem_pct: float | None
    gpu_pct: float | None
    gpu_temp: float | None
    gpu_vram: float | None

    def as_dict(self) -> dict:
        """Plain mapping whose keys match ``contract.HISTORY``."""
        return {name: getattr(self, name) for name in COLUMNS}

    def as_tuple(self) -> tuple:
        return tuple(getattr(self, name) for name in COLUMNS)

    @classmethod
    def from_payload(cls, payload: dict) -> "SampleRow":
        """Flatten a /api/metrics payload into the stored scalars."""
        cpu = payload.get("cpu") or {}
        mem = payload.get("mem") or {}
        gpu = (payload.get("gpus") or [{}])[0] or {}

        def pct(part, whole):
            if part is None or not whole:
                return None
            return round(100 * part / whole, 2)

        temps = gpu.get("temps") or {}
        gpu_temp = temps.get("edge")
        if gpu_temp is None and temps:
            gpu_temp = max(v for v in temps.values() if v is not None)

        return cls(
            ts=float(payload["ts"]),
            cpu_pct=cpu.get("usage"),
            cpu_temp=cpu.get("temp"),
            mem_pct=pct(mem.get("used"), mem.get("total")),
            gpu_pct=gpu.get("usage"),
            gpu_temp=gpu_temp,
            gpu_vram=pct(gpu.get("vram_used"), gpu.get("vram_total")),
        )


class HistoryStore:
    """Append-only history of headline metrics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writes ----------------------------------------------------------

    def record(self, payload: dict) -> None:
        """Store one sample. Duplicate timestamps are replaced, not an error."""
        row = SampleRow.from_payload(payload)
        problems = contract.check_history_row(row.as_dict())
        if problems:  # a shape mismatch is a programming error, not a data error
            raise ValueError(f"history row violates the contract: {problems}")
        placeholders = ",".join("?" * len(COLUMNS))
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO samples ({','.join(COLUMNS)}) VALUES ({placeholders})",
                row.as_tuple(),
            )
            self._conn.commit()

    def prune(self, keep_seconds: float, now: float | None = None) -> int:
        """Delete rows older than ``keep_seconds``; returns the number removed."""
        newest = now if now is not None else self._newest_ts()
        if newest is None:
            return 0
        cutoff = newest - keep_seconds
        with self._lock:
            cur = self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # -- reads -----------------------------------------------------------

    def _newest_ts(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(ts) FROM samples").fetchone()
        return row[0] if row else None

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def range(self, start: float, end: float) -> list[SampleRow]:
        with self._lock:
            rows: Iterable[tuple] = self._conn.execute(
                f"SELECT {','.join(COLUMNS)} FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                (start, end),
            ).fetchall()
        return [SampleRow(*r) for r in rows]

    def latest(self) -> SampleRow | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {','.join(COLUMNS)} FROM samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return SampleRow(*row) if row else None

    def aggregate(self, start: float, end: float, buckets: int) -> list[dict]:
        """Downsample a range into ``buckets`` equal-width mean/min/max groups."""
        rows = self.range(start, end)
        if not rows or buckets <= 0:
            return []
        width = max((end - start) / buckets, 1e-9)
        out: list[dict] = []
        for i in range(buckets):
            lo, hi = start + i * width, start + (i + 1) * width
            group = [r for r in rows if lo <= r.ts < hi]
            if not group:
                continue
            out.append(
                {
                    "ts": lo + width / 2,
                    "n": len(group),
                    "cpu_avg": _mean([r.cpu_pct for r in group]),
                    "cpu_max": _max([r.cpu_pct for r in group]),
                    "mem_avg": _mean([r.mem_pct for r in group]),
                    "gpu_avg": _mean([r.gpu_pct for r in group]),
                }
            )
        return out


def _mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 2) if clean else None


def _max(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(max(clean), 2) if clean else None
