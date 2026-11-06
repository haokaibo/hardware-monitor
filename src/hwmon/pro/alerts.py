"""Threshold alert engine.

The dashboard already colours values by threshold, but colour is only visible to
someone looking at the page. This engine turns the same idea into state that can
drive notifications, so an alert fires once when a metric crosses a limit and
clears when it recovers, instead of being re-sent on every sample.

Semantics
---------
* A rule has a metric path, a warning and a critical level, and a comparison
  direction (``above`` by default, ``below`` for things like free space).
* An alert is raised on the transition into a worse state, so a value that sits
  above the limit for an hour produces one notification, not 3600.
* A state must hold for ``for_samples`` consecutive evaluations before it is
  reported, which suppresses spikes.
* ``recover`` is required before the same alert can fire again (hysteresis).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

LEVELS = ("ok", "warning", "critical")


@dataclass(frozen=True)
class Rule:
    name: str
    path: str  # dotted path into the payload, e.g. "cpu.temp"
    warning: float
    critical: float
    direction: str = "above"  # "above" or "below"
    for_samples: int = 1
    # When true the derived metric is 0..100 percent; used only for messages.
    is_percent: bool = False

    def level(self, value: float | None) -> str:
        if value is None:
            return "ok"
        if self.direction == "above":
            if value >= self.critical:
                return "critical"
            if value >= self.warning:
                return "warning"
            return "ok"
        if value <= self.critical:
            return "critical"
        if value <= self.warning:
            return "warning"
        return "ok"


@dataclass
class Alert:
    rule: str
    level: str
    value: float | None
    ts: float
    message: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "level": self.level, "value": self.value, "ts": self.ts, "message": self.message}


def _lookup(payload: dict, path: str):
    """Resolve a dotted path; returns None when any hop is missing."""
    node = payload
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


@dataclass
class _RuleState:
    level: str = "ok"
    streak: int = 0
    pending: str = "ok"
    notified: str = "ok"  # last level the sink was told about


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("cpu_temp", "cpu.temp", warning=75, critical=90),
    Rule("gpu_temp", "gpus.0.temps.edge", warning=78, critical=90),
    Rule("gpu_junction", "gpus.0.temps.junction", warning=85, critical=100),
    Rule("mem_used_pct", "mem.used_pct", warning=85, critical=95, is_percent=True),
    Rule("vram_used_pct", "gpus.0.vram_used_pct", warning=85, critical=95, is_percent=True),
    Rule("disk_used_pct", "disks.0.fs.0.used_pct", warning=80, critical=92, is_percent=True),
)


def derive_percentages(payload: dict) -> dict:
    """Add the ``*_pct`` fields the default rules reference.

    The agent payload carries raw byte counts; doing the arithmetic here keeps
    the wire format unchanged and the rules declarative.
    """
    out = dict(payload)

    mem = dict(out.get("mem") or {})
    if mem.get("total"):
        mem["used_pct"] = round(100 * mem["used"] / mem["total"], 2)
    out["mem"] = mem

    gpus = []
    for gpu in out.get("gpus") or []:
        g = dict(gpu)
        if g.get("vram_total"):
            g["vram_used_pct"] = round(100 * (g.get("vram_used") or 0) / g["vram_total"], 2)
        gpus.append(g)
    out["gpus"] = gpus

    disks = []
    for disk in out.get("disks") or []:
        d = dict(disk)
        fs = []
        for entry in d.get("fs") or []:
            f = dict(entry)
            if f.get("total"):
                f["used_pct"] = round(100 * f["used"] / f["total"], 2)
            fs.append(f)
        d["fs"] = fs
        disks.append(d)
    out["disks"] = disks

    return out


class AlertEngine:
    """Evaluates rules against successive payloads and reports transitions."""

    def __init__(
        self,
        rules: Iterable[Rule] | None = None,
        sink: Callable[[Alert], None] | None = None,
    ) -> None:
        self.rules = list(rules if rules is not None else DEFAULT_RULES)
        self.sink = sink or (lambda _alert: None)
        self._state: dict[str, _RuleState] = {r.name: _RuleState() for r in self.rules}
        self.active: dict[str, Alert] = {}

    def evaluate(self, payload: dict, ts: float | None = None) -> list[Alert]:
        """Feed one sample. Returns the alerts to notify about (often none)."""
        enriched = derive_percentages(payload)
        stamp = ts if ts is not None else float(enriched.get("ts") or 0.0)
        fired: list[Alert] = []

        for rule in self.rules:
            state = self._state[rule.name]
            value = _lookup(enriched, rule.path)
            level = rule.level(value)

            # streak-based debounce: a level must persist before it counts
            if level == state.pending:
                state.streak += 1
            else:
                state.pending, state.streak = level, 1
            stable = state.streak >= rule.for_samples
            effective = state.pending if stable else state.level

            if effective == state.level:
                continue

            state.level = effective
            if effective == "ok":
                if state.notified != "ok":
                    cleared = Alert(
                        rule=rule.name, level="ok", value=value, ts=stamp,
                        message=f"{rule.name} recovered",
                    )
                    state.notified = "ok"
                    self.active.pop(rule.name, None)
                    self.sink(cleared)
                continue

            alert = Alert(rule=rule.name, level=effective, value=value, ts=stamp, message=self._describe(rule, value, effective))
            state.notified = effective
            self.active[rule.name] = alert
            fired.append(alert)
            self.sink(alert)

        return fired

    @staticmethod
    def _describe(rule: Rule, value: float | None, level: str) -> str:
        unit = "%" if rule.is_percent else ""
        shown = "unknown" if value is None else f"{value:g}{unit}"
        limit = rule.critical if level == "critical" else rule.warning
        verb = "at or above" if rule.direction == "above" else "at or below"
        return f"{rule.name} is {shown}, {verb} the {level} limit of {limit:g}{unit}"

    def active_alerts(self) -> list[dict]:
        return [a.as_dict() for a in self.active.values()]
