"""Build Cython extensions for the pro tier.

Why compile at all
------------------
The open part of hwmon (``collectors/``, ``server.py``, ``cli.py``) ships as
readable source. The pro tier (``pro/``) and the licence verifier are compiled
to native extensions so the boundary between "visible standard edition" and
"protected pro edition" is a real artifact boundary rather than a promise.

What this does
--------------
Converts each listed module to C, compiles it against the *running* interpreter,
and writes the resulting ``.so`` next to a ``.py`` shim that imports it.

The ABI trap
------------
The output is named ``<module>.cpython-3XX-<platform>.so``. That tag is the
interpreter's ABI, and Python only looks for suffixes in
``importlib.machinery.EXTENSION_SUFFIXES``. Consequences, all of which this
project now tests:

* A ``3.12`` build is invisible to a ``3.13`` interpreter: the import fails with
  ``ModuleNotFoundError``, not a symbol error. The message is identical to "the
  file is not installed", which is why the troubleshooting guide starts there.
* An ``abi3`` build (``--limited-api``) would be portable across 3.x, but the
  pro modules use ``sqlite3`` and dataclasses; abi3 is not a drop-in and is not
  attempted here. The Python version is therefore pinned for the packaged pro
  build (see ``.python-version``), and the RPM/Docker images install exactly
  that interpreter.

Usage
-----
    python packaging/build_ext.py                 # build into build/cython
    python packaging/build_ext.py --out dist/ext  # custom output

The modules are built in place under ``build/cython`` and the tree is left
untouched: ``src/hwmon/pro/*.py`` stays importable, so the pure-Python path is
what developers and the test suite use.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# Modules compiled for the packaged pro build. Paths are relative to src/.
# `licensing/verify.py` is not installed as a package, so it is compiled
# separately (see compile_licence_verifier) and shipped next to pro/.
PRO_MODULES = (
    "hwmon/pro/history.py",
    "hwmon/pro/alerts.py",
)

# PyPI packages needed at build time only; uv supplies them as the `cython`
# optional dependency group.
SETUP_PY = '''\
from setuptools import Extension, setup
from Cython.Build import cythonize

import sys

# A plain Cython module (no Py_LIMITED_API) is the right choice here: the pro
# modules use sqlite3 and dataclasses, and a limited-API build would not work
# without rewriting them. The consequence is an interpreter-specific ABI tag,
# which is documented and tested rather than hidden.
setup(
    ext_modules=cythonize(
        [{modules}],
        language_level="3",
        compiler_directives={{"binding": True, "embedsignature": False}},
        quiet=False,
    ),
    script_args=sys.argv[1:],
)
'''


def _ensure_cython() -> None:
    try:
        import Cython  # noqa: F401
    except ModuleNotFoundError:  # pragma: no cover - depends on the env
        raise SystemExit(
            "Cython is not installed. Run: uv sync --extra cython "
            "(it is declared in the `cython` optional dependency group)"
        )


def build(out_dir: Path, modules: tuple[str, ...] = PRO_MODULES) -> list[Path]:
    """Compile the given modules and return the produced .so paths."""
    _ensure_cython()

    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="hwmon-cython-"))

    # Compile from a copy so the source tree never gains .c/.so artifacts; a
    # stray in-place .so next to the .py would shadow it for every later import.
    staging = work / "src"
    shutil.copytree(SRC, staging)
    specs = []
    for rel in modules:
        target = staging / rel
        if not target.is_file():
            raise SystemExit(f"missing module to compile: {rel}")
        # --inplace writes next to the source, so the package directory has to
        # exist even when the module lives directly in it.
        target.parent.mkdir(parents=True, exist_ok=True)
        specs.append(f'Extension("{rel[:-3].replace("/", ".")}", ["{target}"])')

    (work / "setup.py").write_text(SETUP_PY.format(modules=", ".join(specs)))

    # Explicit --build-lib instead of --inplace: inplace resolution depends on
    # cwd and silently looks for hwmon/pro under the build root.
    build_lib = work / "lib"
    cmd = [
        sys.executable, "setup.py", "build_ext",
        f"--build-lib={build_lib}",
        f"--build-temp={work / 'tmp'}",
    ]
    print("+", " ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:])
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit("Cython build failed")

    produced = sorted(build_lib.rglob("*.so")) + sorted(build_lib.rglob("*.pyd"))
    if not produced:
        raise SystemExit("no extension produced; check the build log")

    copied: list[Path] = []
    for so in produced:
        rel = so.relative_to(build_lib)
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(so, dest)
        copied.append(dest)
        print(f"built {dest.relative_to(REPO) if dest.is_relative_to(REPO) else dest}", file=sys.stderr)

    return copied


def abi_tag(so: Path) -> str:
    """Extract 'cpython-312' from 'history.cpython-312-x86_64-linux-gnu.so'.

    Only the interpreter part, not the platform suffix: the platform half stays
    the same across the two distributions the packaging docs compare, which is
    exactly why a glibc mismatch cannot be spotted from the filename.
    """
    for part in so.name.split("."):
        if part.startswith("cpython-"):
            # take 'cpython-312' and drop '-x86_64-linux-gnu' / '-darwin'
            return "-".join(part.split("-")[:2])
    return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="build/cython", help="output directory (default: build/cython)")
    ap.add_argument("--modules", nargs="*", default=list(PRO_MODULES),
                    help="paths relative to src/ to compile")
    args = ap.parse_args(argv)

    out = (REPO / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    built = build(out, tuple(args.modules))

    print()
    print(f"interpreter : {sys.version.split()[0]}")
    print(f"ABI tag     : {abi_tag(built[0])}")
    print(f"output      : {out}")
    print()
    print("These .so files load ONLY on the interpreter above. Python looks for")
    print(f"  {abi_tag(built[0])}")
    print("in importlib.machinery.EXTENSION_SUFFIXES; a different minor version")
    print("cannot see them at all and reports ModuleNotFoundError.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
