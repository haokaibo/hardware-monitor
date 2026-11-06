#!/usr/bin/env python3
"""Build a one-file executable of the collector CLI.

Why this script exists
----------------------
The collectors are loaded with ``importlib`` on purpose (see
``hwmon.collectors``). A frozen build cannot see those imports by static
analysis, so a naive ``pyinstaller entry.py`` produces a binary that starts fine
and then fails on the first collector with::

    CollectorNotFound: cannot import hwmon.collectors.cpu: No module named
    'hwmon.collectors.cpu'

``--collect-submodules`` is the PyInstaller equivalent of Nuitka's
``--include-package``; ``--collect-data`` brings the dashboard assets. Both are
required. ``tests/test_packaging.py`` asserts this script still passes them.

Usage (from the repository root)::

    uv run --all-extras python packaging/build_exe.py
    ./dist/hwmon-cli --only cpu

The resulting binary is for diagnostics and air-gapped hosts; the supported
deployment paths stay uv sync + the RPM/Docker images.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# These two are the whole point of the script. Removing either silently breaks
# the frozen binary at runtime, not at build time.
REQUIRED_FLAGS = (
    "--collect-submodules",
    "hwmon.collectors",
    "--collect-data",
    "hwmon",
)

ENTRY = '''\
"""PyInstaller entry point.

A real module rather than the package's __main__, so relative imports inside
hwmon resolve normally in the frozen app.
"""
from hwmon.collectors.__main__ import main
import sys

sys.exit(main())
'''


def build(outdir: Path | None = None) -> Path:
    """Run PyInstaller and return the path to the produced binary."""
    if shutil.which("pyinstaller") is None:
        raise SystemExit(
            "pyinstaller is not installed. Run: uv sync --all-extras "
            "(it is declared in the `build` optional dependency group)"
        )

    outdir = outdir or REPO / "dist"
    work = Path(tempfile.mkdtemp(prefix="hwmon-pyi-"))
    entry = work / "entry.py"
    entry.write_text(ENTRY)

    cmd = [
        "pyinstaller",
        "--onefile",
        "--name", "hwmon-cli",
        "--distpath", str(outdir),
        "--workpath", str(work / "build"),
        "--specpath", str(work),
        "--noconfirm",
        *REQUIRED_FLAGS,
        str(entry),
    ]
    print("+", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, cwd=REPO, check=True)
    return outdir / "hwmon-cli"


if __name__ == "__main__":
    binary = build()
    print(binary)
