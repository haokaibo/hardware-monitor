# Packaging comparison: PyInstaller vs Nuitka vs Cython

Measured on this repository. Every number comes from building and running the
artefact; nothing is copied from vendor benchmarks.

## Measurement environment, and why it matters

The numbers were first taken on macOS arm64 and **contradicted** what the same
workload does on the deployment target. They are reported here for Linux x86_64
only; the macOS figures are kept in a short section at the end because the
difference is itself the finding.

| | |
|---|---|
| Platform | `linux/amd64`, glibc 2.41, 10 cores |
| Interpreter | CPython 3.12.16 |
| Host | Apple Silicon, amd64 container under QEMU emulation |
| Emulation factor | **1.85×** (a 30 M-iteration integer loop: 2.91 s emulated vs 1.58 s native) |

Everything below is emulated. Two consequences:

* **Absolute times are ~1.9× slower than a native x86_64 machine.** Divide by
  1.85 for a native estimate; the *ratios between the three options* are
  unaffected, because all were measured the same way.
* **Sizes and structural behaviour are exact** — they do not depend on CPU speed.

Reproduce with:

```bash
docker run --rm --platform linux/amd64 -v "$PWD:/work" -w /work \
  python:3.12-slim bash packaging/benchmark.sh
```

The script needs `binutils`, `gcc` and `libc6-dev` on Linux; it installs them.
Their absence is invisible on macOS and stops PyInstaller outright.

## What is being compared

| Option | Produces | Customer needs |
|---|---|---|
| **PyInstaller onefile** | single executable, embedded CPython, frozen bytecode archive | nothing |
| **Nuitka onefile** | single executable, own bootstrap, code compiled to C | nothing |
| **Cython hybrid** | `.so` for `pro/`, source for everything else | **same CPython minor version + glibc floor** |

Cython does not ship a runtime. That is why its numbers are not directly
comparable to the other two, and why the table marks them.

## Results

| Metric | PyInstaller | Nuitka onefile | Cython hybrid |
|---|---|---|---|
| Build time | 8 s | **57 s** | 9 s |
| Artefact size | 8.13 MB | 7.11 MB | **2.14 MB** (incl. source) |
| Cold start (first run) | 218 ms | **179 ms** | n/a |
| Warm start (best of 5) | 213 ms | **171 ms** | n/a |
| `pro` import cost | — | — | 136 ms |
| Source baseline (`python -m`) | 131 ms | 131 ms | 131 ms |
| Source `pro` import | — | — | 132 ms |

Reading the table:

* **Nuitka is genuinely faster than PyInstaller here** — 171 ms vs 213 ms warm,
  179 vs 218 cold. On Linux the bootstrap decompresses quickly and the compiled
  code pays off. This is the opposite of what the macOS run showed.
* **Nuitka costs 7× the build time** (57 s vs 8 s). With ccache a rebuild is much
  faster; the first build always pays this.
* **Cython's import cost is indistinguishable from source** (136 ms vs 132 ms,
  inside noise). Compiling `pro/` is a protection measure, not a performance
  measure — that should not be claimed otherwise.
* PyInstaller's warm start barely beats cold (213 vs 218 ms): it extracts once
  and reuses the temp dir, so the freezer overhead is small on Linux.

### Corrected figures for a native x86_64 host

Dividing the emulated times by 1.85 gives an estimate, not a measurement:

| | PyInstaller | Nuitka | source baseline |
|---|---|---|---|
| Warm start (est.) | ~115 ms | ~92 ms | ~71 ms |

Treat these as order-of-magnitude. Anyone publishing them as measurements on a
native machine should re-run the script there.

## The correction that had to be made

The first Linux run reported **Nuitka at 15 ms**, faster than the source
interpreter itself. That was not a result: the Nuitka binary had not been built
(the arm64 `/tmp` path was referenced from an amd64 container), so the timing
loop measured `command not found`. A missing binary is faster than any real run,
and `|| true` turned it into a plausible-looking minimum.

`packaging/benchmark.sh` now:

* fails loudly if an artefact is absent or does not run, before timing it,
* ignores a failed invocation rather than timing it,
* compares `pro` import against the source import, not against CLI startup.

The lesson generalises: a benchmark that swallows failures will always flatter
the option that is broken.

## Dynamic imports

`hwmon.collectors` uses `importlib`. Both freezers need an explicit flag or they
produce a binary that builds cleanly and dies at runtime with
`CollectorNotFound`:

| Tool | Flag |
|---|---|
| PyInstaller | `--collect-submodules hwmon.collectors` |
| Nuitka | `--include-package=hwmon.collectors` |

Verified by building both and grepping their output for `CollectorNotFound`
(zero with the flags).

## Reverse-engineering resistance

### PyInstaller: bytecode is recoverable

```
$ pyinstxtractor-ng dist/hwmon-cli
[+] Found 118 files in PYZ archive
[+] Successfully extracted pyinstaller archive
```

The tree contains `hwmon/collectors/cpu.pyc` and friends. Without any
decompiler, structure is already visible:

```
functions: ['reset', '_snapshot', '_model', '_average_freq_mhz', '_temperature', 'collect']
string constants: 'CPU collector: usage from /proc/stat deltas...', '_prev'
```

`dis` prints the full instruction stream with original names (121 lines for
`collect()`). A Python decompiler recovers near-source.

### Nuitka: no bytecode

`entry.dist/` holds a native binary plus data; there is no `.pyc` and no `.so`
for the Python modules. Recovery needs native disassembly.

### Cython: no bytecode, docstrings stripped

Measured on `history.cpython-312-*.so`:

```
exported symbols:        2
source-line references:  0
docstrings:              0
```

One silent leak was found and fixed: **Cython keeps docstrings by default**, so
the first build still contained *"SQLite-backed history storage..."* and every
literal — a prose map of the algorithm, readable with `strings`, no disassembly
required. `docstrings` is a global Cython option rather than a compiler
directive, so it is set via `Cython.Compiler.Options.docstrings = False` in
`packaging/build_ext.py`. Verified that the stripped module still records rows
and fires alerts.

### Honest ranking

```
PyInstaller  <<  Nuitka  <  Cython
(easy)                      (native reverse engineering)
```

None of these stop a determined attacker. Nuitka and Cython raise the cost from
"run a tool" to "use a disassembler and understand the output". That is the whole
benefit, and it is not secrecy.

## What this project uses

* **Cython for `pro/`** in the RPM. The target already pins CPython 3.12
  (`Requires: python3.12`), so the ABI lock costs nothing, and it gives the
  strongest protection on the tier holding proprietary algorithms. Its import
  cost is nil (136 vs 132 ms), so nothing is traded for that protection.
* **PyInstaller for the diagnostic CLI.** It is copied to an arbitrary machine
  and run once; requiring a matching interpreter would defeat the purpose. The
  binary contains only the open tiers, so its weakness against extraction does
  not expose anything proprietary.
* **Nuitka is evaluated, not adopted.** On Linux it is 20 % faster at startup and
  13 % smaller than PyInstaller, at 7× the build time. That is a reasonable
  trade for a distributed desktop binary; here the CLI is a diagnostic tool run
  occasionally, and the build speed directly slows CI. Flags stay in
  `packaging/nuitka_flags.txt` so the evaluation is repeatable.

## macOS figures (superseded)

Kept only to document the discrepancy that made the re-run necessary:

| | PyInstaller | Nuitka |
|---|---|---|
| Warm start | 0.33 s | **0.43 s** (slower!) |
| Size | 8.8 MB | 7.5 MB |

On macOS Nuitka started *slower* than PyInstaller. On Linux it is 20 % faster.
Nuitka's onefile bootstrap re-decompresses its payload every run on macOS and
does so much faster on Linux; PyInstaller caches its extraction. **A packaging
benchmark from a developer laptop describes the laptop**, which is why the table
above is Linux-only.
