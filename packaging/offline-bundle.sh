#!/usr/bin/env bash
# Build an air-gap bundle for hwmon.
#
# Enterprise hosts are frequently offline. `hwmon-setup` can install from a
# bundle that contains everything the runtime needs, so the connected machine
# does the downloading and the target only copies files.
#
# Bundle layout
# -------------
#   hwmon-offline/
#     cpython-<version>-<platform>/   uv-managed interpreter, ready to copy
#     wheels/                         every wheel the runtime imports
#     bundle.txt                      interpreter, platform, package versions
#     SHA256SUMS                      verified by hwmon-setup before install
#
# What goes in wheels/ is derived from `uv export` (the locked dependency set),
# not from a hand-written list: the bundle must match uv.lock or an offline
# install will silently differ from a connected one.
#
# Usage
# -----
#   packaging/offline-bundle.sh --out /media/usb/hwmon-offline
#   packaging/offline-bundle.sh --python 3.12 --out /tmp/b
#
# The target must run the same CPU architecture. A bundle built on x86_64
# cannot serve aarch64: the interpreter and every wheel are architecture
# specific. The script records the platform and hwmon-setup does not check it,
# because the interpreter itself fails loudly enough.
set -euo pipefail

cd "$(dirname "$0")/.."

OUT=""
PYVER=$(cat .python-version)
UV=${UV:-uv}

while [ $# -gt 0 ]; do
  case "$1" in
    --out)    OUT="$2"; shift 2 ;;
    --python) PYVER="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$OUT" ] || { echo "usage: $0 --out DIR [--python 3.12]" >&2; exit 2; }
command -v "$UV" >/dev/null 2>&1 || { echo "uv not found on PATH" >&2; exit 1; }

mkdir -p "$OUT"/wheels
ABS_OUT=$(cd "$OUT" && pwd)

echo "==> interpreter ($PYVER)"
# uv installs a relocatable standalone CPython under its install dir; copying the
# whole directory is what makes it usable on a host with no network.
INSTALL_DIR=$(mktemp -d)
trap 'rm -rf "$INSTALL_DIR"' EXIT
UV_PYTHON_INSTALL_DIR="$INSTALL_DIR" "$UV" python install "$PYVER" >/dev/null
# uv lays the install dir out with a versioned name plus a `cpython-X.Y-<plat>`
# symlink pointing at it. `cp -a` would copy that symlink as an ABSOLUTE path
# into the temp dir, so the bundle would reference a directory that no longer
# exists and `sha256sum -c` (via `find -type f`) would silently checksum nothing.
# Resolve the link and copy the real tree under a stable name.
PYLINK=$(ls -d "$INSTALL_DIR"/cpython-"$PYVER"* 2>/dev/null | head -1)
[ -n "$PYLINK" ] || { echo "uv did not produce an interpreter for $PYVER" >&2; exit 1; }
PYROOT=$(cd "$PYLINK" && pwd -P)
PYNAME=cpython-"$PYVER"-$(basename "$PYROOT" | sed -E "s/^cpython-[0-9.]+-//")
[ -d "$PYROOT" ] || { echo "interpreter directory not found at $PYROOT" >&2; exit 1; }
cp -a "$PYROOT"/. "$ABS_OUT/$PYNAME"/
# Assert the copy is a real directory with content, not a dangling link.
[ -d "$ABS_OUT/$PYNAME" ] && [ ! -L "$ABS_OUT/$PYNAME" ] || {
  echo "interpreter copy at $ABS_OUT/$PYNAME is not a real directory" >&2; exit 1; }
[ "$(ls -A "$ABS_OUT/$PYNAME")" ] || { echo "interpreter copy is empty" >&2; exit 1; }
echo "    $PYNAME ($(find "$ABS_OUT/$PYNAME" -type f | wc -l | tr -d ' ') files)"

echo "==> wheels"
# uv export gives the exact locked set, including transitive deps. --no-dev so
# test and build tooling never reaches a production host.
TMPREQ=$(mktemp)
trap 'rm -rf "$INSTALL_DIR" "$TMPREQ"' EXIT
"$UV" export --frozen --no-dev --no-emit-project --format requirements-txt -o "$TMPREQ"
# Download without `uv pip download`, which does not exist in every uv release
# (0.7.x lacks it). Create a throwaway venv and ask it to resolve+download into
# the bundle directory; `--only-binary` keeps it to wheels so no compiler is
# needed on the target.
DL_VENV=$(mktemp -d)
"$UV" venv --python "$PYVER" "$DL_VENV/venv" --seed >/dev/null
"$DL_VENV"/venv/bin/python -m pip download \
    --only-binary=:all: \
    --dest "$ABS_OUT/wheels" \
    -r "$TMPREQ" >/dev/null
rm -rf "$DL_VENV"

# The project wheel has to be built here; hwmon-setup installs it by version.
"$UV" build --wheel --out-dir "$ABS_OUT/wheels" "$PWD" >/dev/null
echo "    $(ls "$ABS_OUT"/wheels | wc -l | tr -d ' ') wheels"

echo "==> manifest"
{
  echo "interpreter: $("$PYROOT"/bin/python3 -VV 2>&1 | head -1)"
  echo "platform: $(uname -srm)"
  echo "python_request: $PYVER"
  echo "hwmon: $(cat VERSION)"
  echo "uv_lock_sha256: $(sha256sum uv.lock | cut -d' ' -f1)"
  echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$ABS_OUT/bundle.txt"

# Checksums cover the interpreter directory and every wheel: a half-copied USB
# stick is the usual failure and it surfaces much later otherwise.
(
  cd "$ABS_OUT"
  : > SHA256SUMS
  for d in cpython-*; do
    [ -d "$d" ] || continue
    find "$d" -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum >> SHA256SUMS
  done
  for w in wheels/*; do
    [ -f "$w" ] || continue
    sha256sum "$w" >> SHA256SUMS
  done
)
echo "    $(wc -l < "$ABS_OUT/SHA256SUMS" | tr -d ' ') files checksummed"

du -sh "$ABS_OUT" | awk '{print "==> bundle:", $0}'
cat <<EOF

Copy this directory to the offline host as /opt/hwmon-offline, then:

    hwmon-setup --offline

Or set it permanently in /etc/hwmon/hwmon-setup.conf:

    HWMON_OFFLINE=1
EOF
