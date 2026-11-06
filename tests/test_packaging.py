"""Packaging and project metadata must stay consistent with the code.

These are cheap assertions that catch the mistakes which only show up at deploy
time: a lock file out of sync, a missing packaged static asset, a console script
pointing at a removed function, or a static asset that was renamed but not
copied into the wheel.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def test_console_script_target_imports(project):
    target = project["project"]["scripts"]["hwmon"]
    module, func = target.split(":")
    mod = __import__(module, fromlist=[func])
    assert callable(getattr(mod, func))


def test_version_floor_matches_the_documented_one(project):
    assert project["project"]["requires-python"] == ">=3.10"
    assert not sys.version_info < (3, 10)


def test_python_version_pin_is_inside_the_supported_range(project):
    pin = (REPO / ".python-version").read_text().strip()
    major, minor = (int(p) for p in pin.split(".")[:2])
    assert (major, minor) >= (3, 10)
    assert (major, minor) <= (3, 14), "pin the CI-verified versions"


def test_static_assets_live_inside_the_package(project):
    # They must be under src/hwmon so hatchling picks them up automatically;
    # adding them to force-include as well makes hatchling fail with a
    # duplicate-path error.
    static = REPO / "src" / "hwmon" / "static"
    assert (static / "index.html").is_file()
    assert (static / "favicon.svg").is_file()
    wheel_cfg = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "force-include" not in wheel_cfg


def test_static_assets_are_readable_through_the_package():
    from importlib import resources

    for name in ("index.html", "favicon.svg"):
        data = (resources.files("hwmon") / "static" / name).read_bytes()
        assert data, f"{name} is empty"
    assert b'id="cpu-rows"' in (resources.files("hwmon") / "static" / "index.html").read_bytes()


def test_lock_file_matches_pyproject():
    """`uv lock --check` exits non-zero when the lock is stale."""
    uv = _find_uv()
    if uv is None:
        pytest.skip("uv not on PATH")
    proc = subprocess.run([uv, "lock", "--check"], cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_lock_file_is_present_and_locked(project):
    text = (REPO / "uv.lock").read_text()
    assert 'name = "hwmon"' in text
    # cryptography must be a locked runtime dependency, not just in a dev group
    assert 'name = "cryptography"' in text
    assert "cryptography>=43" in "\n".join(project["project"]["dependencies"])


def test_runtime_dependencies_are_importable():
    from hwmon import config, server, sysfs  # noqa: F401
    from hwmon.collectors import cpu  # noqa: F401

    import cryptography  # noqa: F401


def test_pro_package_is_importable_without_optional_extras():
    from hwmon.pro import AlertEngine, HistoryStore  # noqa: F401


def test_packaging_files_reference_existing_paths():
    for path in (REPO / "packaging").rglob("Dockerfile.*"):
        for line in path.read_text().splitlines():
            if line.startswith("COPY "):
                for token in line.split()[1:-1]:
                    if token.startswith("--"):
                        continue
                    local = REPO / token.split("/")[0]
                    assert local.exists(), f"{path.name} copies missing {token}"

    spec = (REPO / "packaging" / "hwmonpro.spec").read_text()
    for token in ("pyproject.toml", "uv.lock", ".python-version", "src", "README.md", "LICENSE"):
        assert token in spec, f"spec never mentions {token}"


def test_docs_mentioned_in_agent_md_exist():
    agent_md = (REPO / "AGENT.md").read_text()
    for name in ("SECURITY.md", "docs/deployment-ci.md", "README.zh-CN.md"):
        assert name in agent_md
        assert (REPO / name).exists(), f"{name} referenced but missing"


def _find_uv() -> str | None:
    import shutil

    return shutil.which("uv")
