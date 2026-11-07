#!/usr/bin/env python3
"""Check that the README layout block describes the tree that exists.

Two directions, because either alone is easy to satisfy while the doc is wrong:

* **Forward** — every path the README names must exist. Catches a refactor that
  moved a file without updating the docs (the `app.py` failure).
* **Reverse** — every source file and directory under the paths the README
  claims to describe must be mentioned. Catches the opposite: code was added
  and the documentation silently stopped being complete.

This runs as a test (`tests/test_packaging.py`) and as a standalone script, so
CI can call it directly on a README-only change without waiting for the full
suite.

Exit codes: 0 in sync, 1 drift found, 2 usage error.

    python packaging/check_readme.py            # human output
    python packaging/check_readme.py --quiet    # only problems
    python packaging/check_readme.py --json     # machine readable
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories whose contents the layout block is expected to enumerate. A new
# module here means the README must say so.
ENUMERATED_DIRS = ("src/hwmon", "src/hwmon/collectors", "src/hwmon/pro", "tests", "packaging", "dev")

# Trees that a reduced checkout may not contain. The test image copies only what
# it needs to run the suite, so .github/, .gitea/ and dev/ are absent there and
# neither direction of the check can say anything about them. Skipping them when
# they are missing keeps the container run meaningful instead of failing on
# paths that exist in the real repository.
ABSENT_IN_IMAGE = (".github", ".gitea", "dev")

# Files that live under those trees and are named bare in the README. Listed
# explicitly because the trees may be absent from the checkout, in which case
# there is nothing to glob; guessing from the filesystem silently produced an
# empty set and the check stopped working inside the test container.
ABSENT_FILES = (
    "Dockerfile.test",   # dev/
    "run-tests.sh",      # dev/
    "ci.yml",            # .github/workflows/
    "deploy.yaml",       # .gitea/workflows/
)

# Files that are noise in a layout listing: they exist but a reader does not
# need them spelled out.
IGNORED = {"__pycache__", ".DS_Store"}

BLOCK_START = "## Project layout"
BLOCK_END = "\n## "


def layout_block(readme: Path = REPO / "README.md") -> str:
    text = readme.read_text(encoding="utf-8")
    if BLOCK_START not in text:
        raise SystemExit(f"{readme} has no '{BLOCK_START}' section")
    rest = text.split(BLOCK_START, 1)[1]
    end = rest.find(BLOCK_END)
    return rest if end == -1 else rest[:end]


def named_paths(block: str) -> set[str]:
    """Paths the block presents as tree entries.

    Only the *first token of a line* counts: the layout is a tree listing, so a
    directory is a line starting with `name/`. Scanning the whole text would
    pick up ordinary prose such as "probe/diff", "/api/metrics" or
    "signing/verification", which are not paths and produced false failures.
    """
    found: set[str] = set()
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        token = stripped.split()[0]
        # A tree entry is a bare name or a path; drop any trailing description.
        if "/" in token or re.search(r"\.[a-z]{2,4}$", token):
            found.add(token)
        elif token.endswith("/"):
            found.add(token)
    return found


# Entries that are legitimate parts of the tree but need no description.
STRUCTURAL = {"__init__.py", "__pycache__"}


def _unverifiable(name: str) -> bool:
    """True when the entry resolves only inside a tree this checkout lacks.

    A bare filename is ambiguous: `Dockerfile.test` in the README refers to
    `dev/Dockerfile.test`, but the name alone does not say so. If any absent
    tree would contain it, treat it as unverifiable here rather than as a
    missing path.
    """
    bare = name.rstrip("/")
    if (REPO / bare).exists():
        return False  # real and present: verifiable
    for absent in ABSENT_IN_IMAGE:
        if not (REPO / absent).exists():
            if bare == absent or bare.startswith(absent + "/"):
                return True
    return bare in ABSENT_FILES


def check_forward(block: str, bases: tuple[str, ...]) -> list[str]:
    """Paths the README presents as tree entries that do not exist."""
    problems = []
    for name in sorted(named_paths(block)):
        bare = name.rstrip("/")
        if any((REPO / b / bare).exists() for b in bases):
            continue
        # multi-segment entries such as .github/workflows/ resolve from the root
        if (REPO / bare).exists():
            continue
        if _unverifiable(name):
            continue
        problems.append(f"README names a path that does not exist: {name}")
    return problems


def check_reverse(block: str) -> list[str]:
    """Source entries that exist but are not described."""
    problems = []
    for rel in ENUMERATED_DIRS:
        directory = REPO / rel
        if not directory.is_dir():
            if rel.split("/")[0] in ABSENT_IN_IMAGE:
                continue  # not part of this checkout; see ABSENT_IN_IMAGE
            problems.append(f"directory promised by this check is missing: {rel}")
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name in IGNORED or entry.name in STRUCTURAL:
                continue
            mentioned = entry.name in block or f"{entry.name}/" in block
            if not mentioned:
                kind = "directory" if entry.is_dir() else "file"
                problems.append(f"{kind} {rel}/{entry.name} is not described in the README layout")
    return problems


def check_required(block: str) -> list[str]:
    """Structures that define the project; the block must not lose them."""
    required = (
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
    )
    return [f"README layout no longer mentions {r}" for r in required if r not in block]


def check_stale(block: str) -> list[str]:
    """Wording that describes the pre-refactor architecture."""
    stale = ("app.py", "one file, stdlib only", "test_app.py", "single file")
    return [f"README layout still describes the old architecture: {s!r}" for s in stale if s in block]


def bases() -> tuple[str, ...]:
    base = (
        "",
        "src/hwmon",
        "src/hwmon/collectors",
        "src/hwmon/pro",
        "src/hwmon/static",
        "tests",
        "packaging",
        "packaging/docker",
        "docs",
        ".github/workflows",
        ".gitea/workflows",
        "licensing",
    )
    # dev/ is not copied into the test image (it defines it), so paths under it
    # cannot be validated from inside the container.
    if (REPO / "dev").is_dir():
        base = base + ("dev",)
    return base


def run() -> list[str]:
    block = layout_block()
    return check_forward(block, bases()) + check_reverse(block) + check_required(block) + check_stale(block)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quiet", action="store_true", help="print nothing when in sync")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    args = ap.parse_args(argv)

    problems = run()

    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
        return 0 if not problems else 1

    if problems:
        print(f"README layout is out of sync ({len(problems)} problem(s)):\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nUpdate the '## Project layout' block in README.md (and README.zh-CN.md).", file=sys.stderr)
        return 1

    if not args.quiet:
        print("README layout matches the tree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
