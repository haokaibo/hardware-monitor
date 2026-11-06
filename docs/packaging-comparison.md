# Packaging comparison: what the compiled pro tier can and cannot do

Everything below was measured, not assumed. Commands and raw output are included
so the experiment can be repeated when a base image or interpreter changes.

## What gets compiled

| Tier | Ships as | Why |
|---|---|---|
| `collectors/`, `server.py`, `cli.py`, `sysfs.py`, `sampler.py` | source | the "standard edition" is meant to be readable |
| `pro/history.py`, `pro/alerts.py` | **compiled `.so`** | the product boundary: the pro tier ships as native code |
| `licensing/verify.py` | source (compiled separately when packaged alone) | stays import-light; see the note at the end |
| `contract.py` | source | imported by the compiled pro modules, so it must be present |

Build:

```bash
uv run --extra cython python packaging/build_ext.py
# built build/cython/hwmon/pro/alerts.cpython-312-darwin.so
# built build/cython/hwmon/pro/history.cpython-312-darwin.so
```

The **open** tiers are never compiled. `packaging/hwmonpro.spec` enforces this by
copying `src/`, deleting `src/hwmon/pro`, and installing only the `.so` files
back into place — so "pro is protected" is a property of the package contents,
not a promise in a README.

## Axis 1 — Python version (ABI tag)

Cython output is named `<module>.cpython-3XY-<platform>.so`. Python only imports
files whose suffix appears in `importlib.machinery.EXTENSION_SUFFIXES`.

Measured with a 3.12.11 build (`cpython-312-darwin`):

| Interpreter | Result |
|---|---|
| 3.11.17 | `ModuleNotFoundError: No module named 'hwmon.pro.history'` |
| 3.12.15 | **loads and runs** |
| 3.13.13 | `ModuleNotFoundError: No module named 'hwmon.pro.history'` |

Two things worth noting:

* The failure is **`ModuleNotFoundError`, not a symbol or ABI error.** It is
  indistinguishable from "the file was not installed", which is why the
  troubleshooting guide starts by listing what is actually on disk rather than
  by suspecting a compiler mismatch.
* Patch versions within a minor (3.12.11 vs 3.12.15) are interchangeable; the
  ABI is stable across patch releases of one minor version.

All five interpreter versions CI tests (`3.10`–`3.14`) run the pro modules from
**source**. Only the packaged build is compiled, so the matrix proves the source
works everywhere while the artefact is pinned to one version.

Why not `abi3` (stable ABI)? A limited-API build would load on any 3.x, but the
pro modules use `sqlite3` and dataclasses, which are not exposed through the
limited API. Enabling it would mean rewriting them for a packaging convenience
that the pinned interpreter already solves.

## Axis 2 — glibc (the one that surprises people)

Both images produce **identically named** files:

```
hwmon/pro/history.cpython-312-aarch64-linux-gnu.so
```

The filename cannot tell you which glibc it needs. Measured builds:

| Built on | Base image | glibc | gcc |
|---|---|---|---|
| `hwmon-ext:rocky8` | `rockylinux:8` | 2.28 | 8.5.0 |
| `hwmon-ext:rocky9` | `rockylinux:9` | 2.34 | 11.5.0 |

### Result with the real pro modules

| Direction | Outcome |
|---|---|
| rocky8 build → rocky9 runtime | **loads and runs** |
| rocky9 build → rocky8 runtime | **loads and runs** |

Both directions worked, which contradicts the usual expectation. The reason is
measurable:

```
$ objdump -T history*.so | grep -oE 'GLIBC_[0-9.]+' | sort -Vu | tail -1
r8 build: GLIBC_2.17
r9 build: GLIBC_2.17
```

Cython's generated C only calls functions that have existed since glibc 2.17, so
the compiler had no reason to bind a newer versioned symbol. **A simple module
is not a useful test of glibc compatibility.** A build that only touches libc
basics will load anywhere, which can give false confidence.

### Result with a module that uses a newer symbol

A probe calling `gettid()` (glibc 2.30) forces the versioned symbol into the
binary:

```
r9 build: GLIBC_2.17, GLIBC_2.30
```

| Runtime | glibc | Outcome |
|---|---|---|
| rocky8 | 2.28 | `ImportError: /lib64/libc.so.6: version 'GLIBC_2.30' not found (required by probe.cpython-312-aarch64-linux-gnu.so)` |
| rocky9 | 2.34 | loads and runs |

So the one-way rule holds in general — **build on the oldest distribution you
intend to run on** — but it only bites when the extension actually uses a
symbol newer than the target glibc. That is why the check must be mechanical:

```bash
# highest glibc version an artefact requires
objdump -T <file>.so | grep -oE 'GLIBC_[0-9.]+' | sort -Vu | tail -1
# glibc on the target
ldd --version | head -1
```

Add this to the release checklist rather than reasoning about it.

## Consequence for packaging

The RPM `%build` step compiles with a uv-provisioned interpreter at
`%{pyver}` (3.12), writes the result to `cython-out/`, and `%install` refuses to
proceed if:

* no `.so` was produced, or
* the recorded build interpreter differs from `%{pyver}`.

Both checks exist because neither failure is visible at install time otherwise:
a package with the wrong-version `.so` installs cleanly and breaks only when
someone enables the pro features.

## Why `licensing/verify.py` is not compiled

It is deliberately absent from `PRO_MODULES`, and the reasoning is a security
argument rather than a packaging convenience, so it lives in
[SECURITY.md](../SECURITY.md#why-the-licence-verifier-is-not-compiled). In short:

* **Kerckhoffs's principle.** The verifier embeds only the *public* key. The
  secret is the private key, which never reaches a customer, so publishing the
  check removes no security.
* **Auditability is a feature for the buyers involved.** An unauditable licence
  check is a supply-chain objection for semiconductor/EDA teams, not an
  assurance.
* **Compilation protects business logic, not security mechanisms.** `pro/` is
  compiled because its algorithms are the product. That is a different goal
  from "make verification harder to read", and the two should not be conflated.

`build_ext.py --modules licensing/verify.py` will compile it if a future tier
has a threat model that justifies hiding it. The default list is where the
decision is expressed — see also `tests/test_cython_build.py`, which asserts
`verify.py` is not in it.

## Reproducing the experiment

```bash
docker build -f packaging/docker/Dockerfile.extbuild.rocky8 -t hwmon-ext:rocky8 .
docker build -f packaging/docker/Dockerfile.extbuild.rocky9 -t hwmon-ext:rocky9 .

mkdir -p out/r8 out/r9
docker run --rm -v "$PWD/out/r8:/out" hwmon-ext:rocky8
docker run --rm -v "$PWD/out/r9:/out" hwmon-ext:rocky9

# each image records its own toolchain
cat out/r8/toolchain.txt out/r9/toolchain.txt

# cross-load both directions (see the script in this doc's git history, or:)
docker run --rm -v "$PWD/out/r8:/ext:ro" hwmon-ext:rocky9 \
  sh -c 'PYTHONPATH=/ext python -c "import hwmon.pro.history"'
```

Rocky 9 note: its base image ships `curl-minimal`, which conflicts with the full
`curl` package, so the build image installs with `dnf --allowerasing`. Rocky 8
does not need it.
