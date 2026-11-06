"""Dynamic collector loading and the `python -m hwmon.collectors` CLI.

The loader is a deliberate indirection, so it gets its own tests: a typo in
COLLECTORS, a module without collect(), or a frozen build missing the package
must all fail loudly rather than silently dropping a metric.
"""

from __future__ import annotations

import json
import types

import pytest

from hwmon import collectors
from hwmon.collectors import COLLECTORS, PAYLOAD_KEYS, CollectorNotFound, load, module_name

from conftest import STAT2


def test_every_declared_collector_loads():
    for name in COLLECTORS:
        func = load(name)
        assert callable(func)
        assert func.__module__ == module_name(name)


def test_every_module_has_a_matching_payload_key():
    assert set(PAYLOAD_KEYS) == set(COLLECTORS)


def test_unknown_collector_name_is_rejected():
    with pytest.raises(CollectorNotFound, match="not a known collector"):
        load("nonsense")


def test_module_without_collect_is_rejected(monkeypatch):
    """A module that exists but exports no collect() must fail loudly."""
    import sys
    import types

    fake = types.ModuleType("hwmon.collectors.stub")
    fake.something_else = lambda: None  # no collect
    monkeypatch.setitem(sys.modules, "hwmon.collectors.stub", fake)
    monkeypatch.setattr(collectors, "COLLECTORS", ("stub",))
    with pytest.raises(CollectorNotFound, match="has no collect"):
        load("stub")


def test_import_error_is_wrapped_with_packaging_hint(monkeypatch):
    """A name whose module is absent (frozen build without the package) gets a
    hint naming the packaging flag instead of a bare ModuleNotFoundError."""
    monkeypatch.setattr(collectors, "COLLECTORS", ("ghost",))
    with pytest.raises(CollectorNotFound, match="frozen build needs --include-package"):
        load("ghost")


def test_load_is_repeatable():
    # importlib caches, so a second call must return the same function object
    assert load("cpu") is load("cpu")


# ---------------------------------------------------------------- aggregate API


def test_collect_all_uses_payload_keys(fake_host):
    payload = collectors.collect_all(fake_host.root)
    # module names are cpu/mem/gpu/disk; payload keys are cpu/mem/gpus/disks
    assert set(payload) == {"cpu", "mem", "gpus", "disks"}
    assert isinstance(payload["gpus"], list)
    assert isinstance(payload["disks"], list)


def test_collect_all_can_keep_module_names(fake_host):
    payload = collectors.collect_all(fake_host.root, payload_keys=False)
    assert set(payload) == set(COLLECTORS)


def test_collect_all_matches_the_contract_shape(fake_host):
    payload = collectors.collect_all(fake_host.root)
    fields = {"cpu", "mem", "gpus", "disks"}
    assert set(payload) == fields


def test_collect_one_returns_native_type(fake_host):
    assert isinstance(collectors.collect_one("cpu", fake_host.root), dict)
    assert isinstance(collectors.collect_one("gpu", fake_host.root), list)


def test_root_argument_overrides_the_module_default(fake_host, monkeypatch, tmp_path):
    """An explicit root wins over sysfs.ROOT."""
    from hwmon import sysfs

    monkeypatch.setattr(sysfs, "ROOT", str(tmp_path / "nonexistent"))
    assert collectors.collect_one("cpu", fake_host.root)["threads"] == 2


def test_root_none_falls_back_to_sysfs_root(fake_host):
    # fake_host points sysfs.ROOT at the tree, so omitting root must still work
    assert collectors.collect_one("cpu")["threads"] == 2


def test_submodule_binding_does_not_shadow_the_api():
    """load() makes hwmon.collectors.<name> the module; the API must not collide.

    This is the trap that made the first version of this loader broken: a
    module-level `def cpu()` was silently replaced by the cpu submodule after
    the first import, so callers got "module is not callable".
    """
    load("cpu")
    assert isinstance(collectors.cpu, types.ModuleType)
    # the public entry points stay callable
    assert callable(collectors.collect_one)
    assert callable(collectors.collect_all)
    assert callable(collectors.load)


# ---------------------------------------------------------------- CLI


def test_cli_prints_every_collector(fake_host, capsys):
    from hwmon.collectors.__main__ import main

    code = main(["--root", fake_host.root])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(out) == set(COLLECTORS)


def test_cli_only_selects_a_subset(fake_host, capsys):
    from hwmon.collectors.__main__ import main

    assert main(["--root", fake_host.root, "--only", "cpu,mem"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"cpu", "mem"}


def test_cli_rejects_an_unknown_only_name(fake_host, capsys):
    from hwmon.collectors.__main__ import main

    assert main(["--root", fake_host.root, "--only", "cpu,bogus"]) == 2
    assert "unknown collector" in capsys.readouterr().err


def test_cli_reports_a_failing_collector(fake_host, capsys):
    """A collector that raises is reported on stderr and the exit code is 1."""
    from hwmon.collectors.__main__ import main

    # remove meminfo so the memory collector raises KeyError
    fake_host.path("/proc/meminfo").unlink()
    assert main(["--root", fake_host.root]) == 1
    err = capsys.readouterr().err
    assert "mem" in err
    assert "KeyError" in err


def test_cli_compare_matches_identical_output(fake_host, tmp_path, capsys):
    from hwmon.collectors.__main__ import main

    baseline = tmp_path / "baseline.json"
    assert main(["--root", fake_host.root]) == 0
    baseline.write_text(capsys.readouterr().out)   # exactly what the CLI prints

    # cpu.usage is a delta between runs, so the comparison ignores it
    fake_host.write("/proc/stat", STAT2)
    assert main(["--root", fake_host.root, "--compare", str(baseline)]) == 0
    err = capsys.readouterr().err
    assert "comparable fields match" in err


def test_cli_compare_detects_a_difference(fake_host, tmp_path, capsys):
    from hwmon.collectors.__main__ import main

    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"cpu": {"model": "Old CPU", "threads": 1, "usage": None,
                                         "freq_mhz": 1, "load": [0, 0, 0], "temp": 1.0}}))
    assert main(["--root", fake_host.root, "--compare", str(stale)]) == 2
    err = capsys.readouterr().err
    assert "difference" in err


def test_cli_compare_missing_file(fake_host, tmp_path, capsys):
    from hwmon.collectors.__main__ import main

    assert main(["--root", fake_host.root, "--compare", str(tmp_path / "nope.json")]) == 2
    assert "cannot read" in capsys.readouterr().err
