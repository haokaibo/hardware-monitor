# Packaging comparison: PyInstaller vs Nuitka vs Cython

Measured on this repository, not copied from vendor benchmarks. Every number
below was produced by building and running the artefact; commands are included.

Platform for all measurements: macOS 15 arm64, CPython 3.12.11.
Absolute times are platform specific; the **ratios and the structural findings**
are what carry over to Linux.

## What is being packaged

The three options are not interchangeable, and pretending otherwise is how this
comparison usually goes wrong:

| Option | What it produces | What the customer needs |
|---|---|---|
| **PyInstaller onefile** | single executable with an embedded CPython and a frozen bytecode archive | nothing |
| **Nuitka onefile** | single executable, own bootstrap, code compiled to C | nothing |
| **Cython hybrid** | `.so` for `pro/`, source for the rest | **the same CPython minor version and glibc floor** |

That last row is the whole decision. Cython does not ship a runtime; it makes the
artefact smaller and faster but moves the interpreter requirement onto the
customer. For a CLI given to an unknown machine, that is a hard constraint. For
an RPM that already declares `Requires: python3.12`, it costs nothing.

## Numbers

| Metric | PyInstaller | Nuitka onefile | Cython hybrid |
|---|---|---|---|
| Artefact size | **8.8 MB** | 7.5 MB | 236 KB (`pro/*.so` only) |
| Cold start | 0.78 s | 0.46 s | 0.04 s |
| Warm start (x5) | 0.33 s | 0.43 s | **0.03 s** |
| Build time | ~3 s | **28.5 s** (no ccache) | ~2 s |
| Needs client Python | no | no | **yes, exact minor** |
| PyInstaller `--collect-submodules` needed | **yes** | n/a | n/a |
| Nuitka `--include-package` needed | n/a | **yes** | n/a |

Two results contradict the usual advice:

* **Nuitka onefile started *slower* than PyInstaller** (0.43 s vs 0.33 s), while
  its cold start was faster (0.46 s vs 0.78 s). PyInstaller pays a large
  one-time cost unpacking to a temp dir on first run, then reuses it; Nuitka's
  bootstrap decompresses its payload on every start. "Nuitka is faster" is a
  statement about *execution* of compiled code, not about CLI startup dominated
  by interpreter boot.
* **Cython's 0.03 s is not comparable** to the others: it measures importing a
  `.so` against an already-running interpreter, not launching a self-contained
  binary. The like-for-like comparison is `python -m hwmon.collectors` (0.04 s,
  source) versus the hybrid (0.03 s) — a ~25 % import improvement on the pro
  modules, invisible in wall-clock CLI use.

## Dynamic imports: the trap each tool handles differently

`hwmon.collectors` loads its modules with `importlib`. Both freezers fail on this
by default, and both fail the same way — the binary builds cleanly and dies at
runtime:

| Tool | Without the flag | With the flag |
|---|---|---|
| PyInstaller | `CollectorNotFound: cannot import hwmon.collectors.cpu` | `--collect-submodules hwmon.collectors` → all four load |
| Nuitka | same class of failure | `--include-package=hwmon.collectors` → all four load |

Verified by building both and grepping the output for `CollectorNotFound`
(0 occurrences with the flags, non-zero without).

## Reverse-engineering resistance

This is where the tools genuinely differ, and where claims should be modest.

### PyInstaller: bytecode is recoverable

```
$ pyinstxtractor-ng dist/hwmon-cli
[+] Found 118 files in PYZ archive
[+] Successfully extracted pyinstaller archive
```

The extracted tree contains `hwmon/collectors/cpu.pyc` and friends. Bytecode is
enough to recover structure without a decompiler:

```
module: <module>
functions: ['reset', '_snapshot', '_model', '_average_freq_mhz', '_temperature', 'collect']
string constants: 'CPU collector: usage from /proc/stat deltas...', 'dict[str, tuple[int, int]]', '_prev'
```

`dis` on the extracted code prints the full instruction stream with original
function and variable names (121 lines for `collect()` alone). A Python
decompiler recovers near-source. **PyInstaller offers no meaningful protection.**

### Nuitka: no bytecode, but not opaque

`dist/entry.dist/` contains a native binary plus a data folder; there is no
`.pyc` and no `.so` for the Python modules. Source structure is not recoverable
by extraction. It is still recoverable by disassembly — the code is C, and
C decompilers exist — but that is a different order of effort from running a
Python-specific tool.

### Cython: no bytecode, symbols stripped

Measured on `history.cpython-312-darwin.so`:

```
exported symbols:        2
source-line references:  0
docstrings:              0   (after Options.docstrings = False)
SQL literals:            present (required at runtime)
```

The `.so` contains no `.py`, no `.pyc`, no bytecode, and no prose describing the
algorithm. Recovering logic requires native reverse engineering.

One finding worth recording, because it was silent: **Cython keeps docstrings by
default.** The first build still contained the module docstring
("SQLite-backed history storage...") and every literal, which hands a reader a
map of the algorithm for free. `docstrings` is a global option, not a compiler
directive, so it is set via `Cython.Compiler.Options.docstrings = False` in
`packaging/build_ext.py`. Verified: the module still works (records rows, fires
alerts) with docstrings stripped.

### Ranking, stated honestly

```
PyInstaller  <<  Nuitka  <  Cython
(easy)                      (native RE required)
```

None of these stop a determined attacker. Nuitka and Cython raise the cost from
"run a tool" to "use a disassembler and understand the output". That is the
entire benefit, and it is a real one — but it is not secrecy.

## Which one this project uses, and why both

* **Cython for `pro/`** in the RPM: the target already pins CPython 3.12
  (`Requires: python3.12`), so the "needs an exact minor version" cost is zero,
  and the extension gives the strongest protection on the tier that has
  proprietary algorithms.
* **PyInstaller for the diagnostic CLI** (`packaging/build_exe.py`): it is meant
  to be copied to an arbitrary machine and run once. Requiring a matching
  interpreter there would defeat the purpose, so a self-contained binary wins
  despite offering no protection — that binary contains only the open tiers.
* **Nuitka is not used.** It was evaluated and measured for this document. It
  sits between the two on every axis and is slower to build by an order of
  magnitude; the project has no case that needs exactly that trade-off. The
  flags remain in `packaging/nuitka_flags.txt` so the evaluation can be repeated.

## Reproducing

```bash
# PyInstaller
uv sync --all-extras
python packaging/build_exe.py                       # -> dist/hwmon-cli

# Nuitka
python -m nuitka --standalone --onefile \
  --output-dir=/tmp/nuitka-build/dist --output-filename=hwmon-nuitka \
  --include-package=hwmon.collectors --include-package-data=hwmon entry.py

# Cython
uv run --extra cython python packaging/build_ext.py --out build/cython

# timing (5 runs, warm)
for i in 1 2 3 4 5; do /usr/bin/time -p ./dist/hwmon-cli --only mem >/dev/null; done

# extraction check
uv run --with pyinstxtractor-ng python -m pyinstxtractor_ng dist/hwmon-cli
```

Note for the Cython leg: run the result with the interpreter it was built for.
A `.cpython-312` artefact is invisible to 3.13 and reports `ModuleNotFoundError`,
which is easy to misread as a packaging failure — see
[packaging-comparison.md](packaging-comparison.md#axis-1--python-version-abi-tag).
