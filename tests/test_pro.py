"""Pro-tier tests: history storage and the alert engine."""

from __future__ import annotations

import json

import pytest

from hwmon.pro import AlertEngine, HistoryStore, Rule
from hwmon.pro.alerts import DEFAULT_RULES, derive_percentages

from conftest import FIXTURES

SAMPLE = json.loads((FIXTURES / "sample_metrics.json").read_text())


# ---------------------------------------------------------------- history


def test_record_and_read_back(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        store.record(SAMPLE)
        assert store.count() == 1
        latest = store.latest()
        assert latest is not None
        assert latest.ts == SAMPLE["ts"]
        assert latest.cpu_pct == SAMPLE["cpu"]["usage"]
        assert latest.gpu_vram is not None


def test_derives_memory_percentage(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        store.record(SAMPLE)
        row = store.latest()
    expected = round(100 * SAMPLE["mem"]["used"] / SAMPLE["mem"]["total"], 2)
    assert row.mem_pct == expected


def test_duplicate_timestamp_is_replaced_not_duplicated(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        store.record(SAMPLE)
        store.record(SAMPLE)
        assert store.count() == 1


def test_range_and_aggregate(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        for i in range(10):
            payload = dict(SAMPLE)
            payload["ts"] = 1000.0 + i
            store.record(payload)
        rows = store.range(1000.0, 1010.0)
        assert len(rows) == 10
        buckets = store.aggregate(1000.0, 1010.0, buckets=5)
        assert len(buckets) == 5
        assert all(b["n"] == 2 for b in buckets)
        assert buckets[0]["cpu_avg"] == SAMPLE["cpu"]["usage"]


def test_prune_drops_old_rows(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        for i in range(10):
            payload = dict(SAMPLE)
            payload["ts"] = 1000.0 + i
            store.record(payload)
        removed = store.prune(keep_seconds=5, now=1009.0)
        assert removed == 4  # 1000..1003 are older than 1004
        assert store.count() == 6


def test_prune_on_empty_store_is_a_noop(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        assert store.prune(keep_seconds=60) == 0


def test_range_returns_empty_outside_the_window(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        store.record(SAMPLE)
        assert store.range(0.0, 1.0) == []


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "h.db"
    with HistoryStore(path) as store:
        store.record(SAMPLE)
    with HistoryStore(path) as store:  # reopening must not wipe data
        assert store.count() == 1


# ---------------------------------------------------------------- alerts


def test_derive_percentages():
    enriched = derive_percentages(SAMPLE)
    assert enriched["mem"]["used_pct"] == pytest.approx(100 * SAMPLE["mem"]["used"] / SAMPLE["mem"]["total"], abs=0.01)
    assert "vram_used_pct" in enriched["gpus"][0]
    assert "used_pct" in enriched["disks"][0]["fs"][0]
    # the original payload must not be mutated
    assert "used_pct" not in SAMPLE["mem"]


@pytest.mark.parametrize(
    "value,expected",
    [(None, "ok"), (10, "ok"), (74.9, "ok"), (75, "warning"), (89.9, "warning"), (90, "critical"), (120, "critical")],
)
def test_rule_levels_above(value, expected):
    rule = Rule("t", "cpu.temp", warning=75, critical=90)
    assert rule.level(value) == expected


@pytest.mark.parametrize("value,expected", [(None, "ok"), (50, "ok"), (10, "warning"), (5, "critical"), (0, "critical")])
def test_rule_levels_below(value, expected):
    rule = Rule("free", "disks.0.fs.0.avail_pct", warning=10, critical=5, direction="below")
    assert rule.level(value) == expected


def test_alert_fires_once_per_transition():
    seen = []
    engine = AlertEngine(rules=[Rule("cpu_temp", "cpu.temp", warning=70, critical=90)], sink=seen.append)
    # Start from the fixture, then override the metric under test *after* the
    # update so the fixture cannot win.
    payload = dict(SAMPLE)
    payload["cpu"] = dict(SAMPLE["cpu"], temp=80)

    engine.evaluate(payload, ts=1.0)
    engine.evaluate(payload, ts=2.0)  # still above: no second notification
    engine.evaluate(payload, ts=3.0)
    assert len(seen) == 1
    assert seen[0].level == "warning"
    assert seen[0].rule == "cpu_temp"


def test_escalation_and_recovery_are_reported():
    seen = []
    engine = AlertEngine(rules=[Rule("cpu_temp", "cpu.temp", warning=70, critical=90)], sink=seen.append)

    engine.evaluate({"ts": 1, "cpu": {"temp": 80}}, ts=1)
    engine.evaluate({"ts": 2, "cpu": {"temp": 95}}, ts=2)
    engine.evaluate({"ts": 3, "cpu": {"temp": 40}}, ts=3)

    assert [a.level for a in seen] == ["warning", "critical", "ok"]
    assert engine.active_alerts() == []


def test_debounce_requires_consecutive_samples():
    seen = []
    engine = AlertEngine(rules=[Rule("cpu_temp", "cpu.temp", warning=70, critical=90, for_samples=3)], sink=seen.append)

    engine.evaluate({"ts": 1, "cpu": {"temp": 80}}, ts=1)
    engine.evaluate({"ts": 2, "cpu": {"temp": 20}}, ts=2)  # spike, resets the streak
    engine.evaluate({"ts": 3, "cpu": {"temp": 80}}, ts=3)
    assert seen == []

    engine.evaluate({"ts": 4, "cpu": {"temp": 80}}, ts=4)
    engine.evaluate({"ts": 5, "cpu": {"temp": 80}}, ts=5)
    assert [a.level for a in seen] == ["warning"]


def test_missing_metric_never_alerts():
    seen = []
    engine = AlertEngine(rules=[Rule("gpu_temp", "gpus.0.temps.edge", warning=70, critical=90)], sink=seen.append)
    engine.evaluate({"ts": 1, "gpus": []}, ts=1)
    engine.evaluate({"ts": 2}, ts=2)
    assert seen == []


def test_default_rules_cover_the_core_metrics():
    names = {r.name for r in DEFAULT_RULES}
    assert {"cpu_temp", "gpu_temp", "mem_used_pct", "vram_used_pct"} <= names


def test_default_rules_fire_only_for_the_busy_vram_in_the_fixture():
    # The fixture is a real capture with 93% VRAM in use, so exactly one default
    # rule should trip; nothing else may. Guards both false negatives and a rule
    # set that starts shouting on healthy data.
    engine = AlertEngine()
    fired = engine.evaluate(SAMPLE, ts=SAMPLE["ts"])
    assert [a.rule for a in fired] == ["vram_used_pct"]
    assert fired[0].level == "warning"


def test_default_rules_are_quiet_on_a_relaxed_payload():
    calm = dict(SAMPLE)
    calm["cpu"] = dict(SAMPLE["cpu"], temp=45)
    calm["mem"] = dict(SAMPLE["mem"], used=SAMPLE["mem"]["total"] // 2)
    calm["gpus"] = [dict(SAMPLE["gpus"][0], vram_used=SAMPLE["gpus"][0]["vram_total"] // 2)]
    calm["disks"] = []
    assert AlertEngine().evaluate(calm, ts=calm["ts"]) == []
