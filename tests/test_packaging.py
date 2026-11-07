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
    """Each image that *runs* the package must not rely on repo file modes.

    Images named Dockerfile.extbuild-* only compile extensions and never run the
    service, so the rule does not apply to them.
    """
    runtime_images = [
        REPO / "Dockerfile",
        *(p for p in (REPO / "packaging" / "docker").glob("Dockerfile.*") if ".extbuild-" not in p.name),
    ]
    assert len(runtime_images) >= 3, [p.name for p in runtime_images]
    for path in runtime_images:
        text = path.read_text()
        assert "chmod -R a+rX" in text, f"{path.name} does not normalise permissions"


# ---------------------------------------------------------------- supply chain


def test_sbom_script_produces_a_manifest_and_signatures():
    script = (REPO / "packaging" / "sbom.sh").read_text()
    assert "sha256sum" in script
    assert "spdx-json" in script and "cyclonedx-json" in script
    # signing must cover the manifest, not only the artefacts
    assert "SHA256SUMS" in script.split("==> signing", 1)[1]
    # and it must not claim success when no key was supplied
    assert "signing skipped" in script


def _code_lines(text: str) -> list[str]:
    """Non-comment lines: comments legitimately mention the flags they forbid."""
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def test_sbom_uses_portable_commands():
    """find -printf is GNU-only; on macOS it silently yields an empty manifest."""
    script = (REPO / "packaging" / "sbom.sh").read_text()
    assert not any("-printf" in ln for ln in _code_lines(script))
    assert "sha256sum -c" in (REPO / "docs" / "offline-install.md").read_text()


def test_offline_bundle_script_and_docs_exist():
    bundle = (REPO / "packaging" / "offline-bundle.sh").read_text()
    assert "uv export" in bundle          # derived from the lock, not hand-written
    assert "SHA256SUMS" in bundle
    # the interpreter must be copied as real files, not as a symlink into a
    # temp dir: that bug shipped an empty bundle once already
    assert "pwd -P" in bundle
    assert "is not a real directory" in bundle

    doc = (REPO / "docs" / "offline-install.md").read_text()
    assert "hwmon-setup --offline" in doc
    assert "--network none" in doc        # records how it was verified
    assert "bundle checksum mismatch" in doc


def test_setup_supports_offline_mode():
    spec = (REPO / "packaging" / "hwmonpro.spec").read_text()
    setup = spec.split("hwmon-setup <<'SETUP'", 1)[1].split("\nSETUP", 1)[0]
    assert "--offline" in setup
    assert "HWMON_OFFLINE" in setup
    # offline must not silently fall back to the network
    assert "fail " in setup
    # and the checksum verification must be portable (no GNU --quiet)
    assert not any("--quiet" in ln for ln in _code_lines(setup)), [
        ln for ln in _code_lines(setup) if "--quiet" in ln
    ]
    assert "sha256sum -c" in setup


def test_rpm_declares_uv_and_reports_provisioning_failure():
    spec = (REPO / "packaging" / "hwmonpro.spec").read_text()
    assert "Requires:       uv" in spec
    post = spec.split("%post", 1)[1].split("%preun", 1)[0]
    assert "FAILED" in post               # a silent echo was the old behaviour
    assert "offline-bundle.sh" in post    # the operator is told how to recover


def test_docker_image_names_state_their_purpose():
    """Two kinds of image live side by side; the names must not be ambiguous.

    Dockerfile.web-* deploys the service, Dockerfile.extbuild-* produces
    artefacts. An earlier naming scheme (Dockerfile.rocky8 next to
    Dockerfile.extbuild.rocky8) was one character apart, and the runtime images
    were even described as "build image" in their own headers.
    """
    from pathlib import Path

    images = {p.name: p for p in (REPO / "packaging" / "docker").glob("Dockerfile.*")}
    assert images, "no images found"

    for name, path in images.items():
        assert name.startswith(("Dockerfile.web-", "Dockerfile.extbuild-")), (
            f"{name} does not declare web- or extbuild- in its name"
        )
        header = path.read_text().splitlines()[0]
        if name.startswith("Dockerfile.web-"):
            assert "RUNTIME" in header, f"{name} should be labelled a runtime image: {header}"
        else:
            assert "BUILD" in header, f"{name} should be labelled a build image: {header}"

    # both flavours exist for each Rocky release, so the pairs stay obvious
    for release in ("rocky8", "rocky9"):
        assert f"Dockerfile.web-{release}" in images
        assert f"Dockerfile.extbuild-{release}" in images


def test_rocky_images_resolve_the_curl_package_conflict():
    """Rocky 9 pre-installs curl-minimal, which blocks the full curl package.

    Three valid resolutions, and each image must use one of them deliberately:
      - Rocky 8: install plain `curl` (curl-minimal does not exist there)
      - Rocky 9 web: install `curl-minimal` (already present, no conflict)
      - Rocky 9 extbuild: install `curl` with --allowerasing
    All four images were broken in one direction or the other before this was
    pinned; only actually building them exposed it.
    """
    import re

    for name in (
        "Dockerfile.web-rocky8",
        "Dockerfile.web-rocky9",
        "Dockerfile.extbuild-rocky8",
        "Dockerfile.extbuild-rocky9",
    ):
        path = REPO / "packaging" / "docker" / name
        if not path.exists():
            continue
        text = path.read_text()
        line = re.search(r"^RUN dnf[^\n]*install[^\n]*", text, re.M)
        assert line, f"{name}: no dnf install line"
        cmd = line.group(0)

        if "rocky8" in name:
            # Rocky 8 has no curl-minimal at all
            assert "curl-minimal" not in cmd, f"{name}: Rocky 8 has no curl-minimal"
            assert re.search(r"\bcurl\b", cmd), f"{name}: needs curl"
        else:
            # Rocky 9: either the minimal package, or plain curl with --allowerasing
            ok = "curl-minimal" in cmd or "--allowerasing" in cmd
            assert ok, f"{name}: Rocky 9 needs curl-minimal or --allowerasing, got {cmd!r}"


def test_test_image_copies_every_root_document_the_tests_read():
    """dev/Dockerfile.test uses an explicit COPY list, so it drifts.

    When BUILD-COMPARISON.md was added, the image was not updated and two tests
    failed inside the container with FileNotFoundError - which reads like a code
    bug, not a missing COPY. Any file a test opens from the repo root must be in
    the image.
    """
    image = REPO / "dev" / "Dockerfile.test"
    if not image.exists():
        # The image deliberately does not copy dev/ (it is the definition of the
        # image itself). Inside the container this check cannot run; the host and
        # CI run it, which is where a stale COPY list is introduced.
        pytest.skip("dev/Dockerfile.test is not present in this environment")

    root_docs = sorted(p.name for p in REPO.glob("*.md")) + ["VERSION", "LICENSE", "Dockerfile"]
    dockerfile = image.read_text()

    missing = [name for name in root_docs if name not in dockerfile]
    assert missing == [], (
        "dev/Dockerfile.test does not copy: "
        + ", ".join(missing)
        + " - tests that read these will fail in the container"
    )


def test_benchmark_script_is_executable_and_syntactically_valid():
    import subprocess

    script = REPO / "packaging" / "benchmark.sh"
    assert script.is_file()
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_readme_project_layout_matches_the_tree():
    """The layout block is the first thing a visitor reads, so it cannot drift.

    It described a single-file `app.py` long after the code became a uv-managed
    package, which made the repository look far smaller than it is. This test
    fails when the block names a path that does not exist, or when a top-level
    directory/package is missing from it.
    """
    import re

    readme = (REPO / "README.md").read_text()
    block = readme.split("## Project layout", 1)[1].split("\n## ", 1)[0]

    # dev/ is not copied into the test image (it defines the image), so paths
    # under it cannot be validated from inside the container. The structural
    # checks below still run; only the existence check skips those names.
    in_image = (REPO / "dev").is_dir()

    # 1. every path the block names must exist somewhere sensible
    bases = (
        "", "src/hwmon", "src/hwmon/collectors", "src/hwmon/pro", "src/hwmon/static",
        "tests", "packaging", "packaging/docker", "docs",
        ".github/workflows", ".gitea/workflows", "licensing",
    )
    if in_image:
        bases = bases + ("dev",)
    named = set(re.findall(r"[A-Za-z0-9_./-]+\.(?:py|md|toml|lock|sh|svg|html|spec|yaml|yml|txt)", block))
    if not in_image:
        named -= {"Dockerfile.test", "run-tests.sh"}
    missing = sorted(n for n in named if not any((REPO / b / n).exists() for b in bases))
    assert missing == [], f"README layout names paths that do not exist: {missing}"

    # 2. the structure that makes this project what it is must be mentioned
    for required in (
        "src/hwmon/",
        "collectors/",
        "pro/",
        "static/",
        "licensing/",
        "packaging/",
        "dev/",
        "tests/",
        "hwmonpro.spec",
        "build_ext.py",
        "build_exe.py",
        "offline-bundle.sh",
        "sbom.sh",
        "Dockerfile.web-rocky",
        "Dockerfile.extbuild-rocky",
    ):
        assert required in block, f"README layout omits {required}"

    # 3. and it must not describe the architecture this repo no longer has
    for stale in ("app.py", "one file, stdlib only", "test_app.py"):
        assert stale not in block, f"README layout still mentions {stale!r}"


def test_chinese_readme_layout_is_not_stale():
    readme = (REPO / "README.zh-CN.md").read_text()
    block = readme.split("## 目录结构", 1)[1].split("\n## ", 1)[0]
    for stale in ("app.py", "单文件，纯标准库", "test_app.py"):
        assert stale not in block, f"中文目录结构仍提到 {stale!r}"
    for required in ("src/hwmon/", "collectors/", "pro/", "licensing/", "packaging/", "dev/"):
        assert required in block, f"中文目录结构缺少 {required}"


def test_readmes_have_no_broken_relative_links():
    """A dead link in the first screenful is worse than a missing section.

    `[app.py](app.py)` survived the refactor and 404'd for anyone who clicked it.
    """
    import re

    for name in ("README.md", "README.zh-CN.md", "AGENT.md", "SECURITY.md"):
        path = REPO / name
        if not path.exists():
            continue
        text = path.read_text()
        broken = []
        for m in re.finditer(r"\[([^\]]+)\]\(([^)#]+)\)", text):
            target = m.group(2).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not target.endswith((".md", ".py", ".svg", ".html", ".toml", ".spec", ".sh", ".txt", "/")):
                continue
            if not (REPO / target.rstrip("/")).exists():
                broken.append(f"[{m.group(1)}]({target})")
        assert broken == [], f"{name} has broken relative links: {broken}"


def test_readme_intro_does_not_promise_the_old_single_file_tool():
    """The first paragraph is what a visitor judges the project by.

    It claimed "zero dependencies / no install — just python3 app.py" long after
    the project became a uv-managed package with one runtime dependency.
    """
    for name in ("README.md", "README.zh-CN.md"):
        heading = (REPO / name).read_text().split("\n---\n", 1)[0]
        for stale in (
            "python3 app.py",
            "no install",
            "无需安装",
            "Zero dependencies",
            "Zero depend",
            "零依赖",
            "(app.py)",
        ):
            assert stale not in heading, f"{name} intro still says {stale!r}"
        # and it must describe what the project is now
        assert "uv" in heading.lower() or "uv-" in heading, f"{name} intro should mention uv"


def test_readme_does_not_hardcode_a_test_count():
    """Every new test made the README's number wrong; the suite reports its own."""
    import re

    for name in ("README.md", "README.zh-CN.md"):
        text = (REPO / name).read_text()
        stale = re.findall(r"#\s*\d+\s*(?:tests|个用例)", text)
        assert stale == [], f"{name} hardcodes a test count: {stale}"
