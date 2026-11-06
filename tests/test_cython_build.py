"""The Cython build path: what it produces and what that implies.

These tests do not compile by default (a compiler is not guaranteed on every
machine); they assert the properties that the docs depend on and run the compile
when a toolchain is present.
"""

from __future__ import annotations

import importlib.machinery
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUILD_EXT = REPO / "packaging" / "build_ext.py"


@pytest.fixture(scope="module")
def build_script() -> str:
    return BUILD_EXT.read_text()


def test_build_script_compiles_only_the_pro_tier(build_script):
    """The open tiers must stay source: that is the product boundary."""
    assert '"hwmon/pro/history.py"' in build_script
    assert '"hwmon/pro/alerts.py"' in build_script
    for open_module in ("collectors", "server.py", "cli.py", "sampler.py", "sysfs.py"):
        assert f"hwmon/{open_module}" not in build_script.split("PRO_MODULES")[1].split(")")[0]


def test_build_script_does_not_use_inplace(build_script):
    """--inplace depends on cwd and once produced files in the wrong directory."""
    # the command is built as a list literal; check the actual arguments
    cmd_block = build_script.split("cmd = [", 1)[1].split("]", 1)[0]
    assert '"--inplace"' not in cmd_block
    assert '"--build-lib=' in cmd_block


def test_build_script_compiles_from_a_copy(build_script):
    """Building in the source tree would leave a .so that shadows the .py."""
    assert "copytree" in build_script
    assert "staging" in build_script


def test_abi_tag_parser():
    sys.path.insert(0, str(BUILD_EXT.parent))
    import importlib

    build_ext = importlib.import_module("build_ext")

    assert build_ext.abi_tag(Path("history.cpython-312-x86_64-linux-gnu.so")) == "cpython-312"
    assert build_ext.abi_tag(Path("alerts.cpython-310-darwin.so")) == "cpython-310"
    assert build_ext.abi_tag(Path("plain.so")) == "unknown"


def test_interpreter_suffix_is_version_specific():
    """Documents the central ABI fact the troubleshooting guide relies on."""
    suffixes = importlib.machinery.EXTENSION_SUFFIXES
    minor = sys.version_info.minor
    # a suffix for this interpreter must exist, and it must carry the minor version
    matching = [s for s in suffixes if f"cpython-3{minor}" in s or s.endswith(".so")]
    assert matching, suffixes
    # and no suffix targets a *different* minor version as the preferred one
    preferred = suffixes[0]
    assert preferred.startswith(f".cpython-3{minor}") or preferred == ".so", preferred


def test_spec_installs_compiled_pro_not_source():
    spec = (REPO / "packaging" / "hwmonpro.spec").read_text()
    assert "cython-out/hwmon/pro/*.so" in spec
    # the source pro directory must be removed before the .so files go back
    assert "rm -rf %{buildroot}%{_datadir}/%{name}/src/hwmon/pro" in spec
    # and an empty compiled set must abort the build
    assert "no compiled pro extensions were installed" in spec


def test_spec_builds_in_build_not_install():
    """%install must only copy; compiling there needs a compiler on the target."""
    spec = (REPO / "packaging" / "hwmonpro.spec").read_text()
    # split on the section markers at line start: "%%build" inside a comment and
    # "%build" as a section header look alike to a naive split
    import re

    sections = dict(
        (name, body)
        for name, body in re.findall(r"^%(build|install|files)\s*$(.*?)(?=^%\w+\s*$|\Z)", spec, re.M | re.S)
    )
    assert "build" in sections and "install" in sections, list(sections)
    build_section = sections["build"]
    install_section = sections["install"]
    assert "build_ext.py" in build_section
    assert "build_ext.py" not in install_section
    assert "cython-out" in install_section


def test_spec_has_exactly_one_install_section():
    spec = (REPO / "packaging" / "hwmonpro.spec").read_text()
    assert spec.count("\n%install") == 1


def test_spec_pins_the_interpreter_and_checks_it():
    spec = (REPO / "packaging" / "hwmonpro.spec").read_text()
    assert "%global pyver 3.12" in spec
    # the recorded build interpreter must match the declared one
    assert "interpreter.txt" in spec
    assert "but the package declares" in spec


def test_docs_describe_the_measured_behaviour():
    """The comparison doc must state the numbers the tests and experiments found."""
    doc = (REPO / "docs" / "packaging-comparison.md").read_text()
    assert "ModuleNotFoundError" in doc
    assert "GLIBC_2.30" in doc  # the measured version that failed on Rocky 8
    assert "GLIBC_2.17" in doc  # and the measured floor of the real modules
    assert "rocky8 build → rocky9 runtime" in doc
    assert "rocky9 build → rocky8 runtime" in doc

    guide = (REPO / "docs" / "troubleshooting-packaging.md").read_text()
    for symptom in (
        "ModuleNotFoundError",
        "GLIBC_2.XX",
        "signature does not match",
        "licence expired",
        "different machine",
    ):
        assert symptom in guide, f"missing symptom: {symptom}"


def test_compiled_modules_need_the_source_package_around_them():
    """A .so alone is not importable: pro uses `from .. import contract`."""
    history = (REPO / "src" / "hwmon" / "pro" / "history.py").read_text()
    assert "from .. import contract" in history
    # and the spec ships contract.py plus the package shim
    spec = (REPO / "packaging" / "hwmonpro.spec").read_text()
    assert "src/hwmon/contract.py" in spec
    assert "src/hwmon/pro/__init__.py" in spec


compiler_available = bool(shutil.which("cc") or shutil.which("gcc"))


@pytest.mark.skipif(not compiler_available, reason="no C compiler available")
def test_cython_build_produces_loadable_extensions(tmp_path):
    """End-to-end: compile, then import the result in a clean tree.

    Skipped when Cython is not installed, so a plain `uv sync` keeps working.
    """
    pytest.importorskip("Cython")

    proc = subprocess.run(
        [sys.executable, str(BUILD_EXT), "--out", str(tmp_path / "ext")],
        cwd=REPO, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]

    so_files = list((tmp_path / "ext").rglob("*.so"))
    assert len(so_files) == 2, [p.name for p in so_files]
    for so in so_files:
        assert f"cpython-3{sys.version_info.minor}" in so.name, so.name

    # assemble a minimal importable package: extensions + what they import
    stage = tmp_path / "pkg"
    (stage / "hwmon" / "pro").mkdir(parents=True)
    for so in so_files:
        shutil.copy2(so, stage / "hwmon" / "pro" / so.name)
    shutil.copy2(REPO / "src" / "hwmon" / "pro" / "__init__.py", stage / "hwmon" / "pro" / "__init__.py")
    shutil.copy2(REPO / "src" / "hwmon" / "contract.py", stage / "hwmon" / "contract.py")
    (stage / "hwmon" / "__init__.py").write_text("")

    check = (
        "import tempfile, pathlib\n"
        "from hwmon.pro.history import HistoryStore\n"
        "from hwmon.pro.alerts import AlertEngine, Rule\n"
        "with HistoryStore(pathlib.Path(tempfile.mkdtemp())/'h.db') as s:\n"
        "    s.record({'ts':1.0,'cpu':{'usage':50},'mem':{'total':100,'used':50},'gpus':[]})\n"
        "    assert s.count()==1\n"
        "seen=[]\n"
        "AlertEngine(rules=[Rule('t','cpu.temp',warning=70,critical=90)], sink=seen.append).evaluate({'ts':1,'cpu':{'temp':80}})\n"
        "assert [a.level for a in seen]==['warning']\n"
        "print('compiled pro OK')\n"
    )
    import os

    env = dict(os.environ, PYTHONPATH=str(stage))
    run = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "compiled pro OK" in run.stdout


# ---------------------------------------------------------------- the verifier


def test_licence_verifier_is_not_compiled(build_script):
    """A deliberate decision, not an oversight.

    Rationale is in SECURITY.md: Kerckhoffs's principle (the public key is not a
    secret), customer auditability, and the distinction between protecting
    business logic (pro/) and protecting a security mechanism (the verifier).
    A test keeps the decision from being reversed by accident when someone
    "tidies" PRO_MODULES.
    """
    pro_modules_block = build_script.split("PRO_MODULES", 1)[1].split(")", 1)[0]
    assert "licensing" not in pro_modules_block
    assert "verify" not in pro_modules_block


def test_runtime_does_not_depend_on_the_licensing_tool():
    """If the runtime imported it, "not compiling it" would still ship it as a
    native artifact exercised on every start."""
    src = REPO / "src"
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "licensing" in stripped:
                offenders.append(f"{path.relative_to(REPO)}: {stripped}")
    assert offenders == [], offenders


def test_security_md_states_the_reason():
    """The decision must be argued, not just asserted somewhere."""
    doc = (REPO / "SECURITY.md").read_text()
    assert "Why the licence verifier is not compiled" in doc
    assert "Kerckhoffs" in doc
    # the three legs of the argument
    assert "private key" in doc
    assert "audit" in doc.lower()
    assert "business logic" in doc


def test_comparison_doc_points_at_the_argument():
    doc = (REPO / "docs" / "packaging-comparison.md").read_text()
    assert "SECURITY.md#why-the-licence-verifier-is-not-compiled" in doc


def test_verifier_can_still_be_compiled_on_request(build_script):
    """The capability is documented; only the default excludes it."""
    assert "--modules" in build_script
