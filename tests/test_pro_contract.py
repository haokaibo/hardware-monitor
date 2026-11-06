"""The pro-tier contracts: history rows and alert dicts.

Contract lists are only useful if something fails when the implementation drifts
from them, so these tests compare the *actual* SQLite schema, dataclass and
output dict against ``hwmon.contract``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hwmon import contract
from hwmon.pro import AlertEngine, HistoryStore, Rule
from hwmon.pro.alerts import Alert
from hwmon.pro.history import COLUMNS, SCHEMA, SampleRow

from conftest import FIXTURES

SAMPLE = json.loads((FIXTURES / "sample_metrics.json").read_text())


# ---------------------------------------------------------------- history contract


def test_contract_columns_match_the_sqlite_table(tmp_path):
    """What contract.HISTORY promises is what the database actually has."""
    with HistoryStore(tmp_path / "h.db") as store:
        info = store._conn.execute("PRAGMA table_info(samples)").fetchall()
    actual = [row[1] for row in info]
    assert actual == contract.HISTORY
    assert COLUMNS == contract.HISTORY


def test_history_ddl_is_generated_from_the_contract():
    for name in contract.HISTORY:
        assert name in SCHEMA
    assert "CREATE TABLE IF NOT EXISTS samples" in SCHEMA
    # ts must stay the primary key: dedupe and range queries depend on it
    assert "ts REAL PRIMARY KEY" in SCHEMA


def test_sample_row_keys_match_the_contract():
    row = SampleRow.from_payload(SAMPLE)
    assert sorted(row.as_dict()) == sorted(contract.HISTORY)
    assert contract.check_history_row(row.as_dict()) == []


def test_recorded_row_reads_back_with_every_contract_field(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        store.record(SAMPLE)
        latest = store.latest().as_dict()
    assert sorted(latest) == sorted(contract.HISTORY)
    assert contract.check_history_row(latest) == []


def test_ddl_has_no_columns_the_contract_does_not_know(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        info = store._conn.execute("PRAGMA table_info(samples)").fetchall()
    extra = {row[1] for row in info} - set(contract.HISTORY)
    assert extra == set(), f"columns absent from contract.HISTORY: {extra}"


def test_recording_rejects_a_row_that_violates_the_contract(tmp_path, monkeypatch):
    """The guard has to actually fire, or it is decoration.

    A row is rejected when the *stored row* violates the contract. Making the
    dataclass report one field as absent simulates a future schema edit that was
    only half applied.
    """
    monkeypatch.setattr(SampleRow, "as_dict", lambda self: {})
    with HistoryStore(tmp_path / "h.db") as store:
        with pytest.raises(ValueError, match="violates the contract"):
            store.record(SAMPLE)


# ---------------------------------------------------------------- alert contract


def test_alert_dict_keys_match_the_contract():
    alert = Alert(rule="cpu_temp", level="warning", value=80.0, ts=1.0, message="hot")
    assert sorted(alert.as_dict()) == sorted(contract.ALERT)
    assert contract.check_alert(alert.as_dict()) == []


def test_emitted_alerts_conform_to_the_contract():
    engine = AlertEngine(rules=[Rule("cpu_temp", "cpu.temp", warning=70, critical=90)])
    engine.evaluate({"ts": 1, "cpu": {"temp": 80}})
    active = engine.active_alerts()
    assert len(active) == 1
    assert contract.check_alert(active[0]) == []
    assert active[0]["level"] in contract.ALERT_LEVELS


def test_levels_are_exactly_the_contract_levels():
    from hwmon.pro.alerts import LEVELS

    assert list(LEVELS) == contract.ALERT_LEVELS


@pytest.mark.parametrize("level", contract.ALERT_LEVELS)
def test_rule_engine_only_produces_contract_levels(level):
    """Every level the engine can emit is declared."""
    rule = Rule("t", "cpu.temp", warning=70, critical=90)
    assert rule.level({"ok": 10, "warning": 80, "critical": 95}[level]) == level


def test_check_alert_reports_each_kind_of_problem():
    assert contract.check_alert({}) != []
    assert contract.check_alert({"rule": "x", "level": "fatal", "message": "m", "ts": 1}) != []
    assert contract.check_alert({"rule": "x", "level": "ok", "message": "m", "ts": "soon"}) != []
    assert contract.check_alert({"rule": "x", "level": "ok", "message": 5, "ts": 1}) != []


def test_check_history_row_reports_a_bad_ts():
    row = SampleRow.from_payload(SAMPLE).as_dict()
    row["ts"] = "not-a-number"
    assert contract.check_history_row(row) != []


def test_recording_a_missing_ts_is_rejected(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        with pytest.raises(KeyError):
            store.record({"cpu": {"usage": 1}})  # no ts: cannot be a time series row


# ---------------------------------------------------------------- round trip


def test_history_survives_a_reopen_with_the_same_contract(tmp_path):
    path = tmp_path / "h.db"
    with HistoryStore(path) as store:
        store.record(SAMPLE)
    with HistoryStore(path) as store:
        row = store.latest()
        assert row is not None
        assert contract.check_history_row(row.as_dict()) == []
    # and the file really is SQLite, not a pickle or JSON blob
    header = path.read_bytes()[:16]
    assert header.startswith(b"SQLite format 3")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    finally:
        conn.close()
