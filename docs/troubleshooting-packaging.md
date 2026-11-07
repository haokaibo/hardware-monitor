# Troubleshooting compiled deployments

For failures that only appear in a packaged build: missing extensions, ABI
mismatches, and licence verification. For runtime/sensor problems see
[troubleshooting.md](troubleshooting.md); for the security model see
[SECURITY.md](../SECURITY.md).

Start every investigation by finding out what is actually installed. Most of
these failures look identical from Python's side and are only distinguishable by
looking at the files.

```bash
# where the package lives and what is inside it
python -c "import hwmon, pathlib; p=pathlib.Path(hwmon.__file__).parent; print(p)"
ls -l "$(python -c 'import hwmon, pathlib; print(pathlib.Path(hwmon.__file__).parent)')/pro/"
```

A healthy pro install contains, next to `__init__.py`:

```
history.cpython-312-x86_64-linux-gnu.so
alerts.cpython-312-x86_64-linux-gnu.so
```

No `.py` files for those two modules — that is the point of compiling them.

---

## 1. `ModuleNotFoundError: No module named 'hwmon.pro.history'`

The most common failure and the most misleading one: it means the same thing
whether the file is absent, named for a different interpreter, or unreadable.
Check in this order.

### 1a. Is the file there at all?

```bash
ls -l .../hwmon/pro/*.so
```

Nothing there → the package was built without the Cython step. The RPM `%install`
refuses to produce such a package, so this usually means a manual `cp -a` or a
Dockerfile that copied `src/` but not the compiled output. Rebuild with
`python packaging/build_ext.py` and reinstall.

### 1b. Does the suffix match this interpreter?

```bash
python -c "import importlib.machinery; print(importlib.machinery.EXTENSION_SUFFIXES)"
ls .../hwmon/pro/*.so
```

The `.so` name must contain one of the suffixes printed. Typical pair:

```
looking for: .cpython-312-x86_64-linux-gnu.so
on disk    : history.cpython-311-x86_64-linux-gnu.so     ← invisible to 3.12
```

Python does not report "wrong ABI"; it reports "no module", because the file
never enters the import search. **Compare the minor version**, not just the
architecture.

Fix: rebuild the extensions for the interpreter the service actually runs, or
point the service at the interpreter they were built for.

### 1c. Is the file readable by the service user?

```bash
sudo -u hwmon test -r .../hwmon/pro/history.cpython-*.so && echo readable || echo DENIED
```

A `.so` copied as root with a restrictive umask is invisible to a service
running as `hwmon`, and the error is again `ModuleNotFoundError`. The image
builds run `chmod -R a+rX` for exactly this reason.

### 1d. Is `__init__.py` missing from the package directory?

```bash
ls .../hwmon/pro/__init__.py .../hwmon/contract.py
```

The compiled modules use relative imports (`from .. import contract`), so they
need the package structure around them. Copying only the `.so` files into a bare
directory produces `ImportError: cannot import name 'contract'` rather than a
missing-module error. Both files ship in the RPM for this reason.

---

## 2. `ImportError: version 'GLIBC_2.XX' not found`

The extension was built on a newer distribution than it is running on. The
filename gives no hint — two artifacts built on different glibc versions have
**identical names**. Measure both sides:

```bash
objdump -T .../hwmon/pro/history.cpython-*.so | grep -oE 'GLIBC_[0-9.]+' | sort -Vu | tail -1
ldd --version | head -1
```

If the first number is higher than the second, the artifact is unusable here.

Fix: rebuild on the oldest distribution you must support (Rocky 8 → glibc 2.28,
Rocky 9 → 2.34), which is why `packaging/docker/Dockerfile.extbuild-rocky8`
exists. See [packaging-comparison.md](packaging-comparison.md) for the measured
one-way compatibility and the caveat that a simple module can hide the problem
by never binding a newer symbol.

Do not "fix" this by copying an older `.so` around: the same artifact then
breaks somewhere else. Build for the floor.

---

## 3. `ImportError` / `ModuleNotFoundError` naming `cryptography` or `cffi`

`cryptography` is the only third-party runtime dependency, and it ships compiled
wheels per Python version and platform. Two situations:

* **Wheel-less platform** (uncommon Linux/arch combination): uv/pip tries a
  source build and needs Rust plus a C toolchain. Either install the toolchain
  or use a platform the project publishes wheels for.
* **Copied a venv between machines**: the `.so` files inside `cryptography` are
  interpreter- and ABI-specific too. Never move a venv between hosts or Python
  versions; rerun `uv sync --frozen` on the target.

```bash
uv sync --frozen --no-dev          # reproduce the locked environment
python -c "import cryptography; print(cryptography.__version__)"
```

---

## 4. Licence verification failures

`hwmon verify` and the licence check both fail closed. The message names the
specific reason; match it here.

| Message | Meaning | Fix |
|---|---|---|
| `missing signature /path/x.sig` | the detached signature is not next to the artefact | ship the `.sig`, or pass `-s` |
| `cannot load public key ...` | the key file is absent or not PEM | check the path; the public key is PEM, not DER |
| `... is not an Ed25519 public key` | an RSA/ECDSA key was supplied | use the Ed25519 key from `hwmon keygen` |
| `signature does not match <file>` | the artefact changed after signing (truncated download, re-packed tarball) | re-download and re-verify; do not re-package a signed artefact |
| `licence expired at YYYY-MM-DD` | past `expires_at` | issue a new licence |
| `licence is bound to a different machine` | the `machine` field does not match this host | issue with the correct machine id |
| `signature does not match the payload` | the licence JSON was edited (including `expires_at`) | re-issue; never hand-edit a licence |
| `signature is not valid base64` | the signature field was mangled (editor, newline stripping) | use the file as issued |

Two rules that prevent most of these:

1. **Sign the artefact you ship.** Signing a tarball and then re-tarring it (or
   signing a `.whl` and shipping the `.tar.gz`) changes the bytes; the signature
   covers the exact file.
2. **Verify from a fresh download**, not from the file you built — that is what
   the recipient will experience.

Exit status: `hwmon verify` returns `1` on any failure, so it can gate an
installer:

```bash
hwmon verify dist/hwmon-1.0.0.tar.gz -k release-public.pem || exit 1
```

---

## 5. The package installed but pro features are unavailable

Check whether the pro module came from source or from the extension — a mixed
install is possible when a previous source-based install left `.py` files behind:

```bash
python -c "
import hwmon.pro.history as h
print(h.__file__)"
```

* `.../history.cpython-*.so` → compiled, as intended.
* `.../history.py` → a source install is shadowing the packaged one. Remove the
  old install (`pip uninstall hwmon`, or delete the stale directory) and install
  the package again.

A stray `.so` left in the source tree causes the opposite problem during
development: it shadows the `.py` and then silently goes stale after an edit.
`build_ext.py` compiles from a copy specifically so this cannot happen.

---

## 6. Collecting evidence for a bug report

```bash
{
  echo "## interpreter";  python -VV
  echo "## platform";     python -c "import platform;print(platform.platform(), platform.machine())"
  echo "## glibc";        ldd --version | head -1
  echo "## package";      python -c "import hwmon;print(hwmon.__file__, hwmon.__version__)"
  echo "## pro files";    ls -l "$(python -c 'import hwmon,pathlib;print(pathlib.Path(hwmon.__file__).parent)')/pro/"
  echo "## suffixes";     python -c "import importlib.machinery;print(importlib.machinery.EXTENSION_SUFFIXES)"
  echo "## glibc need";   objdump -T "$(python -c 'import hwmon,pathlib;print(pathlib.Path(hwmon.__file__).parent)')"/pro/*.so 2>/dev/null | grep -oE 'GLIBC_[0-9.]+' | sort -Vu | tail -1
} > hwmon-diag.txt
```

This answers the first three questions in every section above without a round
trip.
