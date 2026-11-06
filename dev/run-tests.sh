#!/bin/sh
# Build and run the Linux test environment.
#
#   dev/run-tests.sh              # full pytest suite inside Linux
#   dev/run-tests.sh collectors   # python -m hwmon.collectors against the container kernel
#   dev/run-tests.sh real         # privileged run so /sys exposes host sensors
set -e
cd "$(dirname "$0")/.."
IMAGE=${IMAGE:-hwmon-test}

docker build -f dev/Dockerfile.test -t "$IMAGE" .

case "${1:-suite}" in
  suite)      docker run --rm "$IMAGE" ;;
  collectors) docker run --rm "$IMAGE" uv run python -m hwmon.collectors ;;
  real)       docker run --rm --privileged --pid=host \
                -v /sys:/sys:ro -v /proc:/proc:ro "$IMAGE" \
                uv run python -m hwmon.collectors ;;
  shell)      docker run --rm -it "$IMAGE" bash ;;
  *) echo "usage: $0 [suite|collectors|real|shell]" >&2; exit 2 ;;
esac
