"""Run every collector once and print the result.

    python -m hwmon.collectors                 # real hardware
    python -m hwmon.collectors --root /fake    # a HWMON_ROOT-style tree
    python -m hwmon.collectors --only cpu,mem  # a subset
    python -m hwmon.collectors --compare old.json

Exit codes: 0 all collectors ran, 1 one or more failed, 2 the comparison found a
difference. This is the acceptance tool for the collector split: it must print
the same JSON the old single-file app.py would have produced.
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import contract, sysfs
from . import COLLECTORS, PAYLOAD_KEYS, load


def _payload(root: str | None) -> dict:
    """Collect everything into an /api/metrics-shaped payload (no metadata)."""
    payload: dict = {}
    for name in COLLECTORS:
        payload[PAYLOAD_KEYS.get(name, name)] = load(name)(root)
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hwmon.collectors", description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=None, help="read from this tree instead of / (default: HWMON_ROOT or /)")
    ap.add_argument("--only", help="comma-separated collector names")
    ap.add_argument("--compare", metavar="FILE", help="diff against a saved JSON payload from the old app.py")
    ap.add_argument("--indent", type=int, default=2)
    args = ap.parse_args(argv)

    root = sysfs.resolve(args.root)
    if args.only:
        wanted = [n.strip() for n in args.only.split(",") if n.strip()]
        unknown = [n for n in wanted if n not in COLLECTORS]
        if unknown:
            print(f"unknown collector(s): {', '.join(unknown)}; known: {', '.join(COLLECTORS)}", file=sys.stderr)
            return 2
        selected = tuple(wanted)
    else:
        selected = COLLECTORS

    result: dict = {}
    failures: dict[str, str] = {}
    for name in selected:
        try:
            result[name] = load(name)(root)
        except Exception as e:  # noqa: BLE001 - a CLI probe reports, it does not raise
            failures[name] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=args.indent, default=str))

    if failures:
        print(f"\n{len(failures)} collector(s) failed:", file=sys.stderr)
        for name, err in failures.items():
            print(f"  {name}: {err}", file=sys.stderr)

    if args.compare:
        return _compare(args.compare, result)

    return 1 if failures else 0


def _compare(path: str, current: dict) -> int:
    """Diff a saved payload against the current run.

    Only shared fields are compared: ``usage`` legitimately differs (it is a
    delta since the previous sample) and timestamps are not part of a collector
    result. Everything else must match exactly.
    """
    try:
        with open(path, encoding="utf-8") as f:
            expected = json.load(f)
    except (OSError, ValueError) as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 2

    diffs = _diff(expected, current, "")

    # Exclude the values that legitimately change between two runs on the same
    # machine, so the comparison reports only real behavioural differences:
    #   cpu.usage / freq_mhz  - deltas and live clocks
    #   fs.{used,avail}       - statvfs reflects actual disk activity
    #   load                  - load average moves every second
    # Matched on the leaf path, so both "cpu.load" and "cpu.load[0]" are covered
    # (the diff reports the whole list when its type or length changes).
    volatile_prefixes = (
        "cpu.usage",
        "cpu.freq_mhz",
        "cpu.load",
    )
    volatile_contains = (".fs[", ".fs.length")
    diffs = [
        d
        for d in diffs
        if not d[0].startswith(volatile_prefixes) and not any(v in d[0] for v in volatile_contains)
    ]

    if not diffs:
        print(
            "\ncomparable fields match "
            "(cpu.usage, cpu.freq_mhz, cpu.load and fs used/avail are excluded as volatile)",
            file=sys.stderr,
        )
        return 0

    print(f"\n{len(diffs)} difference(s) from {path}:", file=sys.stderr)
    for path_name, old, new in diffs:
        print(f"  {path_name}: {old!r} -> {new!r}", file=sys.stderr)
    return 2


def _diff(old, new, prefix: str) -> list[tuple[str, object, object]]:
    """Recursive structural diff of two JSON values."""
    out: list[tuple[str, object, object]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            where = f"{prefix}.{key}" if prefix else key
            if key not in old:
                out.append((where, "<absent>", new[key]))
            elif key not in new:
                out.append((where, old[key], "<absent>"))
            else:
                out.extend(_diff(old[key], new[key], where))
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            out.append((f"{prefix}.length", len(old), len(new)))
        for i, (a, b) in enumerate(zip(old, new)):
            out.extend(_diff(a, b, f"{prefix}[{i}]"))
    elif old != new:
        out.append((prefix, old, new))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
