"""Packaging and project metadata must stay consistent with the code.

These are cheap assertions that catch the mistakes which only show up at deploy
time: a lock file out of sync, a missing packaged static asset, a console script
pointing at a removed function, or a static asset that was renamed but not
copied into the wheel.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"

# tomllib only exists on 3.11+, but requires-python is 3.10, so this module must
# import there too. Rather than vendor a TOML parser (or add tomli just for
# tests), the few fields asserted below are read from the file text and the
# full structure is checked by the tomllib-based test that is skipped on 3.10.
if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover - exercised only on 3.10
    _toml = None


def _section(text: str, header: str) -> str:
    """Body of a [header] table, up to the next table header."""
    marker = f"[{header}]"
    start = text.index(marker) + len(marker)
    rest = text[start:]
    end = rest.find("\n[")
    return rest if end == -1 else rest[:end]


def _value(text: str, header: str, key: str) -> str:
    """Raw value of `key` inside [header]; fails loudly when absent."""
    for line in _section(text, header).splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith(f"{key} ="):
            return line.split("=", 1)[1].strip()
    raise KeyError(f"{key} not found in [{header}]")


@pytest.fixture(scope="module")
def project_text() -> str:
    return PYPROJECT.read_text()


@pytest.fixture(scope="module")
def project() -> dict:
    if _toml is None:
        pytest.skip("full pyproject inspection needs tomllib (3.11+)")
    return _toml.loads(PYPROJECT.read_text())


def test_console_script_target_imports(project):
    target = project["project"]["scripts"]["hwmon"]
    module, func = target.split(":")
    mod = __import__(module, fromlist=[func])
    assert callable(getattr(mod, func))


def test_version_floor_matches_the_documented_one(project_text):
    """Parsed from text so the 3.10 CI leg still checks the floor."""
    assert _value(project_text, "project", "requires-python") == '">=3.10"'
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


# ---------------------------------------------------------------- freezing


def test_freeze_script_passes_the_required_flags():
    """The collectors are imported dynamically, so a frozen build must be told.

    A binary built without these flags builds cleanly and then dies at runtime
    with CollectorNotFound on the first collector. The script encodes the fix;
    this test stops someone from "simplifying" it away.
    """
    script = (REPO / "packaging" / "build_exe.py").read_text()
    assert "--collect-submodules" in script
    assert "hwmon.collectors" in script
    assert "--collect-data" in script
    # and the docstring must keep explaining why
    assert "CollectorNotFound" in script


def test_nuitka_flags_file_matches_the_pyinstaller_flags():
    flags = (REPO / "packaging" / "nuitka_flags.txt").read_text()
    assert "--include-package=hwmon.collectors" in flags
    assert "--include-package-data=hwmon" in flags


def test_collectors_are_loaded_dynamically_not_statically():
    """Guard the design decision rather than the implementation detail.

    If this ever becomes a static import, the packaging story changes and the
    note in AGENT.md goes stale.
    """
    init = (REPO / "src" / "hwmon" / "collectors" / "__init__.py").read_text()
    assert "importlib.import_module" in init
    assert "COLLECTORS" in init and "load(" in init
    # The payload builders must go through load(), not through direct imports.
    # (Two pure helpers do import their module lazily inside the function for
    # path resolution; what matters is that collection itself is dynamic.)
    assert "load(name)(root)" in init


def test_every_declared_collector_module_exists():
    import re

    init = (REPO / "src" / "hwmon" / "collectors" / "__init__.py").read_text()
    match = re.search(r"COLLECTORS[^=]*= \(([^)]+)\)", init)
    assert match, "COLLECTORS literal not found"
    names = re.findall(r'"([a-z]+)"', match.group(1))
    assert names, "COLLECTORS is empty"
    for name in names:
        assert (REPO / "src" / "hwmon" / "collectors" / f"{name}.py").is_file(), f"missing {name}.py"


def _find_uv() -> str | None:
    import shutil

    return shutil.which("uv")


def test_dual_mode_parsing_agrees_when_tomllib_is_available(project, project_text):
    """The text-based reads must agree with the parsed structure on 3.11+."""
    if _toml is None:
        pytest.skip("tomllib unavailable (3.10 leg); text reads are checked directly")
    assert _value(project_text, "project", "requires-python") == '"' + project["project"]["requires-python"] + '"'
    assert _value(project_text, "tool.hatch.version", "path") == '"VERSION"'
    assert 'hwmon = "hwmon.cli:main"' in _section(project_text, "project.scripts")


def test_static_assets_are_world_readable():
    """The container runs as an unprivileged user.

    A file created with a restrictive umask (0600) ships that way into the image,
    and then index.html is unreadable at runtime: "/" returns 404 while /healthz
    and /api/info stay green, so nothing at build time catches it. Found by
    actually running the built image.
    """
    import stat

    static = REPO / "src" / "hwmon" / "static"
    problems = []
    for path in static.iterdir():
        if not path.is_file():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if not mode & stat.S_IROTH:
            problems.append(f"{path.name} is {mode:04o}, not world-readable")
    assert problems == [], problems


def test_dockerfiles_make_the_package_readable():
    """Each image that ships the package must not rely on repo file modes."""
    for path in [REPO / "Dockerfile", *(REPO / "packaging" / "docker").glob("Dockerfile.*")]:
        text = path.read_text()
        assert "chmod -R a+rX" in text, f"{path.name} does not normalise permissions"
