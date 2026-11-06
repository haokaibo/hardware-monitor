"""Parser and collector tests against a fake sysfs tree."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hwmon.collectors import parent_drives as parents
from hwmon.collectors.gpu import collect as gpus, pcie, pcie_generation
from hwmon.collectors.cpu import collect as cpu
from hwmon.collectors.disk import collect as disks
from hwmon.collectors.mem import collect as mem
from hwmon.sysfs import temps, to_num

from conftest import STAT1, STAT2, w


# ---------------------------------------------------------------- parsers


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2.5 GT/s PCIe", 1),
        ("16.0 GT/s PCIe", 4),
        ("5.0 GT/s PCIe", 2),
        ("32.0 GT/s PCIe", 5),
        ("2.5 GT/s PCIe (downgraded)", 1),
        ("Unknown", None),
        (None, None),
    ],
)
def test_pcie_generation_mapping(text, expected):
    assert pcie_generation(text) == expected


@pytest.mark.parametrize("text,expected", [("42", 42), (None, None), ("n/a", None)])
def test_to_num(text, expected):
    assert to_num(text) == expected


@pytest.mark.parametrize(
    "speed,width,gen,gbps",
    [
        ("16.0 GT/s PCIe", 16, 4, 32),
        ("32.0 GT/s PCIe", 16, 5, 64),
        ("8.0 GT/s PCIe", 4, 3, 4),
    ],
)
def test_pcie_bandwidth(speed, width, gen, gbps):
    with tempfile.TemporaryDirectory() as d:
        w(d, "current_link_speed", speed)
        w(d, "current_link_width", width)
        p = pcie(d)
        assert (p["gen"], p["width"], p["gbps"]) == (gen, width, gbps)


def test_pcie_unknown_has_no_bandwidth():
    with tempfile.TemporaryDirectory() as d:
        w(d, "current_link_speed", "Unknown")
        w(d, "current_link_width", 16)
        p = pcie(d)
        assert p["gen"] is None
        assert "gbps" not in p
    assert pcie("/nonexistent")["gen"] is None  # a missing directory must not raise


def test_temps():
    with tempfile.TemporaryDirectory() as d:
        w(d, "temp1_input", 41500)
        w(d, "temp1_label", "Tctl")
        w(d, "temp2_input", 50000)  # no label -> fall back to temp2
        w(d, "temp3_input", "garbage")  # unparsable -> ignored
        assert temps([d]) == {"Tctl": 41.5, "temp2": 50.0}
    assert temps(["/nonexistent"]) == {}


# ---------------------------------------------------------------- collectors


def test_cpu(fake_host):
    first = cpu()
    assert first["usage"] is None  # no previous sample yet, so usage is unknown

    fake_host.write("/proc/stat", STAT2)
    c = cpu()
    assert c["usage"] == 50.0  # total +200, idle (idle+iowait) +100
    assert c["threads"] == 2
    assert c["model"] == "Fake CPU 9000"
    assert c["freq_mhz"] == 2000
    assert c["temp"] == 45.0  # k10temp Tctl, not the nvme 99 C reading


def test_cpu_temp_fallback_to_max(fake_host):
    fake_host.path("/sys/class/hwmon/hwmon0/temp1_label").unlink()
    fake_host.write("/sys/class/hwmon/hwmon0/temp2_input", 61000)
    assert cpu()["temp"] == 61.0


def test_mem(fake_host):
    m = mem()
    assert m["total"] == 16384000 * 1024
    assert m["used"] == (16384000 - 8192000) * 1024
    assert m["swap_used"] == (4096 - 1024) * 1024


def test_gpus_picks_only_discrete_amd(fake_host):
    g = gpus()
    assert len(g) == 1  # iGPU, non-AMD and connector dirs all excluded
    g = g[0]
    assert g["name"] == "Fake Radeon"
    assert (g["usage"], g["vram_used"]) == (3, 1073741824)
    assert g["temps"] == {"edge": 29.0}
    assert (g["power_w"], g["power_cap_w"]) == (18.0, 250.0)
    assert (g["pcie"]["gen"], g["pcie"]["width"], g["pcie"]["gbps"]) == (5, 16, 64)


def test_gpus_env_overrides(fake_host, monkeypatch):
    monkeypatch.setenv("GPU_NAME", "My Card")
    monkeypatch.setenv("INCLUDE_IGPU", "1")
    names = [g["name"] for g in gpus()]
    assert names[0] == "My Card"
    assert len(names) == 2  # with INCLUDE_IGPU the iGPU counts too


def test_parents(fake_host):
    assert parents("nvme0n1p1") == ["nvme0n1"]  # partition -> whole drive
    assert parents("sda") == ["sda"]  # whole drive -> itself
    assert parents("dm-0") == ["nvme0n1"]  # LVM/dm -> slaves -> partition -> drive
    assert parents("does-not-exist") == []


def test_disks(fake_host):
    ds = {d["name"]: d for d in disks()}
    assert set(ds) == {"nvme0n1", "sda", "sdb"}  # loop / dm / md are skipped
    assert (ds["nvme0n1"]["kind"], ds["sda"]["kind"], ds["sdb"]["kind"]) == ("NVMe", "HDD", "SSD")
    assert ds["nvme0n1"]["temp"] == 30.0  # prefer Composite
    assert ds["sda"]["temp"] == 33.0  # without Composite, take the first
    assert ds["sdb"]["temp"] is None
    assert ds["nvme0n1"]["size"] == 2000000 * 512
    assert sorted(f["mount"] for f in ds["nvme0n1"]["fs"]) == sorted(
        [f"{fake_host.mnt}/boot", f"{fake_host.mnt}/data"]
    )
    assert [f["mount"] for f in ds["sda"]["fs"]] == [f"{fake_host.mnt}/hdd"]  # duplicate mount counted once
    assert ds["sdb"]["fs"] == []
    f = ds["sda"]["fs"][0]
    assert f["fstype"] == "ext4"
    assert f["total"] > 0
    assert f["total"] >= f["used"]


def test_bind_mounted_file_is_not_reported_as_a_filesystem(fake_host):
    """Docker bind-mounts /etc/hostname, which shows up in /proc/mounts as a
    device mount. statvfs succeeds on it and returns the backing volume's
    numbers, so it must be filtered out or the whole volume's usage gets
    attributed to a single file. Found by running the suite in a container.
    """
    a_file = fake_host.path("/proc/hostname-file")
    a_file.write_text("container-host")
    fake_host.write(
        "/proc/mounts",
        f"/dev/nvme0n1p1 {a_file} ext4 rw 0 0\n"
        f"/dev/nvme0n1p1 {fake_host.mnt}/boot ext4 rw 0 0\n",
    )
    mounts = [f["mount"] for d in disks() for f in d["fs"]]
    assert str(a_file) not in mounts
    assert f"{fake_host.mnt}/boot" in mounts


def test_collectors_skip_when_block_dir_missing(fake_host):
    (Path(fake_host.root) / "sys/block").rename(Path(fake_host.root) / "sys/block-moved")
    assert disks() == []  # a missing /sys/block must degrade, not raise


def test_cpu_handles_backwards_counters(fake_host):
    # usage needs two ticks to diff against
    assert cpu()["usage"] is None
    fake_host.write("/proc/stat", STAT2)
    assert cpu()["usage"] == 50.0
    # Counters reset (reboot, or a fake tree rewritten): the guard `t > prev_t`
    # skips the computation instead of dividing by zero or going negative.
    fake_host.write("/proc/stat", STAT1)
    assert cpu()["usage"] is None
