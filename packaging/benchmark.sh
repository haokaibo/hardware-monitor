#!/usr/bin/env bash
# Benchmark the three packaging approaches and print a machine-readable table.
#
# Written to run inside a Linux container so the numbers describe the deployment
# target (glibc, x86_64), not the developer's laptop.
#
#   packaging/benchmark.sh                     # in whatever container you ran
#   docker run --rm --platform linux/amd64 -v "$PWD:/work" -w /work \
#     python:3.12-slim bash packaging/benchmark.sh
#
# Caveat when running linux/amd64 on an Apple Silicon host: the CPU is emulated
# (~1.8x slower than native, measured). The script prints the emulation factor
# so a reader can separate "this machine is slow" from "this approach is slow".
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
OUT=${OUT:-/tmp/bench}
mkdir -p "$OUT"

num() { printf '%s' "$1" | tr -d '\n'; }

echo "==> environment"
ARCH=$(uname -m)
echo "    arch:        $ARCH"
echo "    kernel:      $(uname -sr)"
echo "    python:      $(python -V 2>&1)"
echo "    glibc:       $(ldd --version 2>/dev/null | head -1 | awk '{print $NF}')"
echo "    cores:       $(nproc 2>/dev/null || echo '?')"

# ---------------------------------------------------------------- tooling
# PyInstaller needs objdump on Linux (binutils) and a C compiler for Cython and
# Nuitka. Neither is required on macOS, so a laptop run hides this.
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq --no-install-recommends binutils gcc libc6-dev >/dev/null 2>&1 || true
fi
pip install -q --upgrade pip >/dev/null 2>&1 || true
pip install -q pyinstaller nuitka ordered-set zstandard patchelf cython setuptools wheel \
  >/dev/null 2>&1
pip install -q -e . >/dev/null 2>&1

# ---------------------------------------------------------------- cpython baseline
echo "==> baseline: source (python -m hwmon.collectors)"
# Verifies the command actually runs before timing it. A missing binary or a
# crash is *faster* than a real run, so a naive timing loop reports the failure
# as the best result - that happened once and produced a 15ms "Nuitka" figure
# for a binary that was not there.
timeit() { # timeit <label> <cmd...>
  local label=$1; shift
  if ! "$@" >/dev/null 2>&1; then
    printf '%s\tERROR\n' "$label"
    return 1
  fi
  local best=999999
  for i in 1 2 3 4 5; do
    local s e
    s=$(date +%s%N)
    "$@" >/dev/null 2>&1
    e=$(date +%s%N)
    local ms=$(( (e - s) / 1000000 ))
    [ "$ms" -lt "$best" ] && best=$ms
  done
  printf '%s\t%dms\n' "$label" "$best"
}

BASE=$(timeit "source" python -m hwmon.collectors --only mem)
echo "    $BASE"

# ---------------------------------------------------------------- pyinstaller
echo "==> PyInstaller"
START=$(date +%s)
python packaging/build_exe.py >/dev/null 2>&1
PYI_BUILD=$(( $(date +%s) - START ))
PYI_BIN=dist/hwmon-cli
[ -x "$PYI_BIN" ] || { echo "PyInstaller produced no binary at $PYI_BIN" >&2; exit 1; }
PYI_SIZE=$(stat -c%s "$PYI_BIN" 2>/dev/null || stat -f%z "$PYI_BIN")
# cold: first run after dropping the extracted cache
rm -rf /tmp/_MEI* 2>/dev/null || true
"$PYI_BIN" --only mem >/dev/null 2>&1 || { echo "pyinstaller binary does not run" >&2; exit 1; }
S=$(date +%s%N); "$PYI_BIN" --only mem >/dev/null 2>&1; E=$(date +%s%N)
PYI_COLD=$(( (E - S) / 1000000 ))
PYI_WARM=$(timeit "pyinstaller-warm" "$PYI_BIN" --only mem | cut -f2)
echo "    build ${PYI_BUILD}s  size ${PYI_SIZE}B  cold ${PYI_COLD}ms  warm ${PYI_WARM}"

# ---------------------------------------------------------------- nuitka
echo "==> Nuitka onefile"
mkdir -p "$OUT/nuitka" && cd "$OUT/nuitka"
cp -a "$REPO/src" ./src
cat > entry.py <<'PY'
from hwmon.collectors.__main__ import main
import sys
sys.exit(main())
PY
START=$(date +%s)
python -m nuitka --standalone --onefile \
  --output-dir="$OUT/nuitka/dist" --output-filename=hwmon-nuitka \
  --include-package=hwmon.collectors --include-package-data=hwmon \
  --assume-yes-for-downloads entry.py >/dev/null 2>&1
NUI_BUILD=$(( $(date +%s) - START ))
NUI_BIN=$OUT/nuitka/dist/hwmon-nuitka
[ -x "$NUI_BIN" ] || { echo "Nuitka produced no binary at $NUI_BIN" >&2; exit 1; }
NUI_SIZE=$(stat -c%s "$NUI_BIN" 2>/dev/null || stat -f%z "$NUI_BIN")
rm -rf /tmp/onefile_* 2>/dev/null || true
"$NUI_BIN" --only mem >/dev/null 2>&1 || { echo "nuitka binary does not run" >&2; exit 1; }
S=$(date +%s%N); "$NUI_BIN" --only mem >/dev/null 2>&1; E=$(date +%s%N)
NUI_COLD=$(( (E - S) / 1000000 ))
NUI_WARM=$(timeit "nuitka-warm" "$NUI_BIN" --only mem | cut -f2)
echo "    build ${NUI_BUILD}s  size ${NUI_SIZE}B  cold ${NUI_COLD}ms  warm ${NUI_WARM}"
cd "$REPO"

# ---------------------------------------------------------------- cython
echo "==> Cython hybrid"
START=$(date +%s)
python packaging/build_ext.py --out "$OUT/cython" >/dev/null 2>&1
CYT_BUILD=$(( $(date +%s) - START ))
CYT_SIZE=$(du -sb "$OUT/cython" | cut -f1)
# assemble a package where pro/ is compiled and the rest is source
rm -rf "$OUT/hybrid" && mkdir -p "$OUT/hybrid"
cp -a src/hwmon "$OUT/hybrid/"
cp "$OUT"/cython/hwmon/pro/*.so "$OUT/hybrid/hwmon/pro/" 2>/dev/null || true
rm -f "$OUT/hybrid/hwmon/pro/history.py" "$OUT/hybrid/hwmon/pro/alerts.py"
PYTHONPATH=$OUT/hybrid python -c "from hwmon.pro import HistoryStore" >/dev/null 2>&1 \
  || { echo "hybrid package does not import" >&2; exit 1; }
CYT_IMPORT=$(timeit "hybrid-import" env PYTHONPATH=$OUT/hybrid python -c "from hwmon.pro import HistoryStore" | cut -f2)
SRC_IMPORT=$(timeit "source-import" env PYTHONPATH=$REPO/src python -c "from hwmon.pro import HistoryStore" | cut -f2)
echo "    build ${CYT_BUILD}s  size ${CYT_SIZE}B  import ${CYT_IMPORT} (source: ${SRC_IMPORT})"

# ---------------------------------------------------------------- emulation factor
echo "==> emulation factor (native vs this CPU)"
cat > "$OUT/cpu.py" <<'PY'
import time
t0 = time.perf_counter()
s = 0
for i in range(30_000_000):
    s += i * i
print(f"{time.perf_counter()-t0:.4f}")
PY
CPU=$(python "$OUT/cpu.py")
echo "    cpu_loop=${CPU}s"

# ---------------------------------------------------------------- table
cat <<EOF

=== RESULT ($ARCH) ===
metric               pyinstaller   nuitka   cython-hybrid
build_seconds        $PYI_BUILD            $NUI_BUILD       $CYT_BUILD
artifact_bytes       $PYI_SIZE      $NUI_SIZE      $CYT_SIZE
cold_start_ms        $PYI_COLD           $NUI_COLD        -
warm_start_ms        ${PYI_WARM%ms}             ${NUI_WARM%ms}          -
pro_import_ms        -             -        ${CYT_IMPORT%ms}
source_import_ms     ${SRC_IMPORT%ms}
baseline_source      $BASE
cpu_loop_seconds     $CPU
EOF
