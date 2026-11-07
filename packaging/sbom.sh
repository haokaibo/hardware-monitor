#!/usr/bin/env bash
# Supply-chain artefacts: SBOM, checksums, and signatures over both.
#
# What this produces
# ------------------
#   dist/sbom-image.spdx.json   inventory of the built container image
#   dist/sbom-image.cdx.json    the same, CycloneDX
#   dist/sbom-python.spdx.json  the runtime Python set, from the image's venv
#   dist/SHA256SUMS             checksum of every artefact in dist/
#   dist/*.sig                  detached Ed25519 signature per artefact
#
# Why the image and not pyproject.toml
# ------------------------------------
# The SBOM must describe what ships. Scanning the source tree or a wheel tells
# you the declared dependencies; scanning the image tells you the CPython
# runtime uv installed, the OS packages in the base image, and the wheels that
# were actually unpacked. A wheel on its own is opaque to syft (it sees one
# file), which is why the image is the primary source and the venv inside it is
# scanned separately for a readable Python-only list.
#
# Signing covers SHA256SUMS as well as each artefact, so a consumer verifies one
# signature and then trusts the manifest. Check side: `hwmon verify`.
#
# Usage
# -----
#   packaging/sbom.sh --image hwmon-web:1.0.0
#   packaging/sbom.sh --image ... --key /secure/hwmon-release-private.pem
#   packaging/sbom.sh --no-image            # wheel + sources only, no docker
#
# Signing requires a key from `hwmon keygen`. Without one this still produces the
# SBOM and checksums and says so: an unsigned build is a legitimate intermediate
# state, an unverified release is not.
set -euo pipefail

cd "$(dirname "$0")/.."
DIST=${DIST:-dist}
IMAGE=${IMAGE:-hwmon-web:release}
SIGN_KEY=${SIGN_KEY:-}
SYFT_IMAGE=${SYFT_IMAGE:-anchore/syft:latest}
DO_IMAGE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --image)    IMAGE="$2"; shift 2 ;;
    --no-image) DO_IMAGE=0; shift ;;
    --key)      SIGN_KEY="$2"; shift 2 ;;
    -h|--help)  sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$DIST"
HAVE_SYFT=0
command -v syft >/dev/null 2>&1 && HAVE_SYFT=1

have_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

# ---------------------------------------------------------------- SBOM

if [ "$DO_IMAGE" -eq 1 ]; then
  [ "$HAVE_SYFT" -eq 1 ] || have_docker || {
    echo "==> image SBOM skipped: needs syft or docker" >&2; DO_IMAGE=0; }

  if [ "$DO_IMAGE" -eq 1 ]; then
    echo "==> image SBOM ($IMAGE)"
    if [ "$HAVE_SYFT" -eq 1 ]; then
      syft "$IMAGE" -o "spdx-json=$DIST/sbom-image.spdx.json" \
                    -o "cyclonedx-json=$DIST/sbom-image.cdx.json" >/dev/null
    else
      # The syft container cannot reach the host's docker socket, so scan a
      # saved archive instead of mounting /var/run/docker.sock.
      TMP=$(mktemp -d)
      trap 'rm -rf "$TMP"' EXIT
      docker save "$IMAGE" -o "$TMP/image.tar"
      docker run --rm -v "$TMP:/in:ro" -v "$PWD/$DIST:/out" "$SYFT_IMAGE" \
        "docker-archive:/in/image.tar" \
        -o "spdx-json=/out/sbom-image.spdx.json" \
        -o "cyclonedx-json=/out/sbom-image.cdx.json" >/dev/null
    fi
    echo "    $DIST/sbom-image.spdx.json"
    echo "    $DIST/sbom-image.cdx.json"

    echo "==> python-only inventory (from the image's venv)"
    # A short, human-readable companion: the full image SBOM includes the Debian
    # packages in the base layer, which drowns the four runtime Python wheels.
    # -d so the glob expands to the directory names, not their contents
    docker run --rm "$IMAGE" sh -c \
      'ls -d /opt/hwmon/venv/lib/python*/site-packages/*.dist-info 2>/dev/null | xargs -r -n1 basename' \
      > "$DIST/python-packages.txt" || true
    if [ -s "$DIST/python-packages.txt" ]; then
      sed 's/^/      /' "$DIST/python-packages.txt"
    else
      echo "      (none found; check the venv path in the image)" >&2
    fi
  fi
fi

# ---------------------------------------------------------------- checksums

echo "==> checksums"
(
  cd "$DIST"
  # `find -printf` is a GNU extension and silently produces an empty manifest on
  # BSD/macOS, which would ship an unsigned, unverifiable release. Use `ls` plus
  # a glob loop instead: portable on both, and `sort` keeps the output stable.
  : > SHA256SUMS
  for f in *; do
    [ -f "$f" ] || continue
    case "$f" in SHA256SUMS|*.sig|.gitignore) continue ;; esac
    printf '%s\n' "$f"
  done | sort | while IFS= read -r f; do
    sha256sum "$f" >> SHA256SUMS
  done
  [ -s SHA256SUMS ] || { echo "ERROR: no artefacts to checksum in $DIST" >&2; exit 1; }
)
echo "    $DIST/SHA256SUMS"
sed 's/^/      /' "$DIST/SHA256SUMS"

# ---------------------------------------------------------------- signatures

if [ -z "$SIGN_KEY" ]; then
  cat <<'NOTE'

==> signing skipped (no --key)
    Sign with the offline key:
        packaging/sbom.sh --image <image> --key /secure/hwmon-release-private.pem
    A published release must be signed: without it `hwmon verify` has nothing to
    check and a consumer cannot distinguish authentic from downloaded.
NOTE
  exit 0
fi

[ -f "$SIGN_KEY" ] || { echo "signing key not found: $SIGN_KEY" >&2; exit 1; }

echo "==> signing"
signed=0
for f in "$DIST"/*.json "$DIST"/*.txt "$DIST"/*.whl "$DIST"/*.tar.gz "$DIST/SHA256SUMS"; do
  [ -f "$f" ] || continue
  case "$f" in *.sig) continue ;; esac
  uv run hwmon sign "$f" -k "$SIGN_KEY" >/dev/null
  echo "    $(basename "$f").sig"
  signed=$((signed + 1))
done
echo "    signed $signed artefact(s); verify with:"
echo "      uv run hwmon verify $DIST/SHA256SUMS -k release-public.pem"
