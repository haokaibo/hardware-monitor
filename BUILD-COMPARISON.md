# Build comparison: PyInstaller vs Nuitka vs Cython (Linux x86_64)

All figures below were produced by building and running the artefacts on
**linux/amd64** — the platform this project actually deploys to. Nothing is
copied from vendor benchmarks.

> Detailed narrative, methodology and the macOS numbers that had to be discarded
> live in [docs/build-comparison.md](docs/build-comparison.md). This file is the
> data table on its own.

## Environment

| | |
|---|---|
| Platform | `linux/amd64` (x86_64) |
| glibc | 2.41 |
| Python | CPython 3.12.16 |
| Cores | 10 |
| Host | Apple Silicon; amd64 container under QEMU emulation |
| Emulation factor | **1.85×** — 30 M-iteration integer loop: 2.91 s emulated vs 1.58 s native |

**All times are emulated ~1.9× slower than a native x86_64 machine.** Ratios
between the three options are unaffected (all measured identically); sizes and
structural findings are exact.

## Summary

| Metric | PyInstaller | Nuitka onefile | Cython hybrid |
|---|---|---|---|
| **Build time** | 8 s | **57 s** | 9 s |
| **Artefact size** | 8,130,752 B (8.13 MB) | 7,109,384 B (7.11 MB) | 2,140,056 B (2.14 MB) |
| **Cold start** | 218 ms | **179 ms** | n/a |
| **Warm start** (best of 5) | 213 ms | **171 ms** | n/a |
| `pro` import | — | — | 136 ms |
| Source baseline (`python -m`) | 131 ms | 131 ms | 131 ms |
| Source `pro` import | — | — | 132 ms |
| Needs client Python | no | no | **yes, exact minor + glibc floor** |
| Bytecode recoverable | **yes** | no | no |
| Extra flags required | `--collect-submodules` | `--include-package` | `--include-package` (PyInstaller/Nuitka) |
| Linux build deps | `binutils`, `gcc` | `gcc`, `ccache` (opt) | `gcc` |

## Estimated native x86_64 times

Dividing by the 1.85× emulation factor — an estimate, not a measurement:

| | PyInstaller | Nuitka | source baseline |
|---|---|---|---|
| Warm start | ~115 ms | ~92 ms | ~71 ms |

Re-run `packaging/benchmark.sh` on a native Intel/AMD host to replace these.

## Reproduce

```bash
docker run --rm --platform linux/amd64 -v "$PWD:/work" -w /work \
  python:3.12-slim bash packaging/benchmark.sh
```

The script installs `binutils`, `gcc` and `libc6-dev`; PyInstaller refuses to
run on Linux without `objdump`, and Cython/Nuitka need a compiler. Neither
requirement is visible on macOS.

## What each number means

**Nuitka is the fastest and smallest of the two self-contained binaries.**
171 ms vs 213 ms warm (20 % faster), 7.11 MB vs 8.13 MB (13 % smaller). It costs
7× the build time — 57 s vs 8 s — which is paid once per build and directly
slows CI.

**PyInstaller's warm start barely beats cold** (213 vs 218 ms): it extracts once
to a temp dir and reuses it, so the freezer overhead on Linux is small.

**Cython's import cost is indistinguishable from source** (136 vs 132 ms, inside
noise). Compiling `pro/` buys protection, not performance — that should not be
claimed otherwise.

**The three are not interchangeable.** PyInstaller and Nuitka ship a runtime;
Cython ships an ABI-locked object file and moves the interpreter requirement onto
the customer. For a binary handed to an unknown machine that is disqualifying;
for an RPM that already declares `Requires: python3.12` it costs nothing.

## Reverse-engineering resistance

| | PyInstaller | Nuitka | Cython |
|---|---|---|---|
| `.pyc` in artefact | **yes, 118 files** | no | no |
| Function/variable names readable | **yes** (`dis`) | no | no |
| Exported symbols in `.so` | n/a | n/a | 2 |
| Source-line references | n/a | n/a | 0 |
| Docstrings in artefact | **yes** | no | 0 (after fix) |

Measured:

```
$ pyinstxtractor-ng dist/hwmon-cli
[+] Found 118 files in PYZ archive
[+] Successfully extracted pyinstaller archive

$ python -c "dis on hwmon/collectors/cpu.pyc"
functions: ['reset', '_snapshot', '_model', '_average_freq_mhz', '_temperature', 'collect']
```

One leak found and fixed while measuring: **Cython keeps docstrings by default**,
so the first build still contained `"SQLite-backed history storage..."` plus
every literal — a prose map of the algorithm readable with `strings`.
`docstrings` is a global Cython option, not a compiler directive, set via
`Cython.Compiler.Options.docstrings = False`.

**Ranking: `PyInstaller << Nuitka < Cython`.** None stop a determined attacker.
Nuitka and Cython raise the cost from "run a tool" to "use a disassembler". That
is the whole benefit, and it is not secrecy.

## Which one this project uses

| Tier | Tool | Why |
|---|---|---|
| `pro/` in the RPM | **Cython** | target already pins CPython 3.12, so the ABI lock is free; strongest protection where the algorithms are |
| diagnostic CLI | **PyInstaller** | copied to an arbitrary machine and run once; contains only open tiers, so extraction exposes nothing proprietary |
| — | **Nuitka** | evaluated, not adopted: 20 % faster and 13 % smaller than PyInstaller, but 7× the build time, and no case here needs that trade |

## Two measurement faults worth remembering

The first Linux run reported **Nuitka at 15 ms** — faster than the source
interpreter. The binary had not been built (an arm64 path referenced from an
amd64 container), so the loop timed `command not found`, and `|| true` turned it
into the winning number. A missing binary is faster than any real run.

`packaging/benchmark.sh` now validates that each artefact exists and runs before
timing it, and ignores a failed invocation rather than timing it.

Separately, the earlier macOS table is kept only in
[docs/build-comparison.md](docs/build-comparison.md), labelled superseded: on
macOS Nuitka started *slower* than PyInstaller (0.43 s vs 0.33 s), which is the
opposite of Linux. A packaging benchmark from a developer laptop describes the
laptop.
