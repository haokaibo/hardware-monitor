"""Sampler tests: the ts invariant, the disk cadence, and error containment."""

from __future__ import annotations

import json

import pytest

from hwmon import contract
from hwmon.collectors.cpu import collect as cpu
from hwmon.collectors.disk import collect as disks
from hwmon.collectors.gpu import collect as gpus
from hwmon.collectors.mem import collect as mem
from hwmon.sampler import DISK_EVERY, Sampler


@pytest.fixture
def sampler(fake_host):
    return Sampler(label="host-a", version="1.2.3", cpu=cpu, mem=mem, gpus=gpus, disks=disks)


def test_sample_matches_contract_and_is_json(sampler):
    sampler.sample(0)
    payload = sampler.state
    assert contract.check_payload(payload) == []
    json.dumps(payload)  # must be serializable (e.g. getloadavg tuples)
    assert payload["error"] is None
    assert payload["label"] == "host-a"
    assert payload["version"] == "1.2.3"


def test_disks_only_refreshed_every_n_samples(sampler):
    sampler.sample(0)
    sampler.state["disks"] = "sentinel"
    sampler.sample(1)
    assert sampler.state["disks"] == "sentinel"
    sampler.sample(DISK_EVERY)
    assert isinstance(sampler.state["disks"], list)


def test_failed_sample_does_not_advance_ts(sampler, fake_host):
    sampler.sample(0)
    ts = sampler.state["ts"]
    fake_host.path("/proc/meminfo").unlink()
    sampler.sample(1)
    assert sampler.state["ts"] == ts  # ts must not advance so the UI can detect staleness
    assert "KeyError" in sampler.state["error"]


def test_recovery_clears_the_error(sampler, fake_host):
    fake_host.path("/proc/meminfo").unlink()
    sampler.sample(0)
    assert sampler.state["error"]
    fake_host.write(
        "/proc/meminfo",
        "MemTotal: 1024 kB\nMemAvailable: 512 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
    )
    sampler.sample(1)
    assert sampler.state["error"] is None


def test_label_falls_back_to_env(fake_host, monkeypatch):
    monkeypatch.setenv("AGENT_LABEL", "from-env")
    from hwmon.config import Config

    assert Config.from_env("agent").label == "from-env"


def test_disk_failure_keeps_last_known_disks(sampler, fake_host):
    sampler.sample(0)
    assert sampler.state["disks"]

    # Make the disk collector itself explode, rather than a single mount entry:
    # best-effort means "keep the previous list", not "clear it".
    def boom():
        raise OSError("udev gone")

    sampler._disks = boom
    sampler.sample(DISK_EVERY)
    # The failure is reported (visible on the page as a degraded state) but the
    # previous disk list is retained so drives do not disappear on a hiccup.
    assert "udev gone" in sampler.state["error"]
    assert sampler.state["disks"], "last known disks must be retained"
    assert [d["name"] for d in sampler.state["disks"]] == ["nvme0n1", "sda", "sdb"]


def test_background_thread_runs_and_stops(fake_host):
    ticks = []

    def clock(_interval):
        ticks.append(1)

    s = Sampler(label="x", version="0", interval=0.01)
    s.start()
    try:
        import time

        for _ in range(200):
            if s.state.get("ts"):
                break
            time.sleep(0.01)
    finally:
        s.stop()
    assert s.state.get("ts")
    assert not s._thread.is_alive()
