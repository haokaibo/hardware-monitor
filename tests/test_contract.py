"""Contract tests: the fixture conforms, and the contract actually catches breaks."""

from __future__ import annotations

import json

import pytest

from hwmon import contract

from conftest import FIXTURES

SAMPLE = json.loads((FIXTURES / "sample_metrics.json").read_text())


def test_fixture_matches_contract():
    assert contract.check_payload(SAMPLE) == []


def test_frontend_reads_are_covered_by_the_contract():
    """Every field the dashboard reads must be declared somewhere in the contract."""
    declared = set(
        contract.TOP + contract.CPU + contract.MEM + contract.GPU + contract.PCIE + contract.DISK + contract.FS
    )
    assert set(contract.FRONTEND_READS) <= declared | {"gbps"}


@pytest.mark.parametrize("key", contract.TOP)
def test_missing_top_level_field_is_reported(key):
    broken = json.loads(json.dumps(SAMPLE))
    del broken[key]
    assert any(key in p for p in contract.check_payload(broken))


@pytest.mark.parametrize("key", contract.CPU)
def test_missing_cpu_field_is_reported(key):
    broken = json.loads(json.dumps(SAMPLE))
    del broken["cpu"][key]
    assert any(f"cpu: missing '{key}'" in p for p in contract.check_payload(broken))


def test_missing_disk_field_is_reported():
    broken = json.loads(json.dumps(SAMPLE))
    del broken["disks"][0]["model"]
    assert any("disks[0]: missing 'model'" in p for p in contract.check_payload(broken))


def test_missing_fs_field_is_reported():
    broken = json.loads(json.dumps(SAMPLE))
    del broken["disks"][0]["fs"][0]["mount"]
    assert any("disks[0].fs[0]: missing 'mount'" in p for p in contract.check_payload(broken))


def test_empty_gpu_list_only_fails_when_required():
    payload = json.loads(json.dumps(SAMPLE))
    payload["gpus"] = []
    assert contract.check_payload(payload, require_gpu=True) == ["gpus: empty"]
    assert contract.check_payload(payload, require_gpu=False) == []


def test_wrong_type_is_reported():
    broken = json.loads(json.dumps(SAMPLE))
    broken["cpu"] = "not an object"
    assert any("cpu: expected object" in p for p in contract.check_payload(broken))


def test_none_values_are_allowed():
    payload = json.loads(json.dumps(SAMPLE))
    payload["cpu"]["usage"] = None  # the first sample has no previous tick to diff
    payload["cpu"]["temp"] = None
    assert contract.check_payload(payload) == []


def test_dashboard_and_contract_agree():
    """Every contract field must appear in index.html, or the page cannot show it."""
    html = (FIXTURES.parent.parent / "src/hwmon/static/index.html").read_text()
    missing = [f for f in contract.TOP if f not in html]
    assert missing == [], f"dashboard never mentions: {missing}"
