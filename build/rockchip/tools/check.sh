#!/usr/bin/env bash

# Runs static checks for the Linux ARM64 build scripts and workflow.

set -euo pipefail

TOOLS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROCKCHIP_BUILD_DIR=$(cd -- "$TOOLS_DIR/.." && pwd)
REPOSITORY_ROOT=$(cd -- "$ROCKCHIP_BUILD_DIR/../.." && pwd)

scripts=(
  "$REPOSITORY_ROOT/build-rockchip.sh"
  "$ROCKCHIP_BUILD_DIR"/*.sh
  "$ROCKCHIP_BUILD_DIR/lib"/*.sh
  "$ROCKCHIP_BUILD_DIR/tests/roi"/*.sh
  "$TOOLS_DIR"/*.sh
)

bash -n "${scripts[@]}"
shellcheck \
  --external-sources \
  --exclude=SC1090,SC1091 \
  "${scripts[@]}"
command -v mtree >/dev/null
actionlint "$REPOSITORY_ROOT/.github/workflows/rockchip-build.yml"
hadolint --ignore DL3008 "$ROCKCHIP_BUILD_DIR/Dockerfile"

for test_name in test_watermark_control.py test_watermark_remote.py; do
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
    -s "$ROCKCHIP_BUILD_DIR/tests" -p "$test_name"
done

if grep -Fq "\$ORIGIN/../lib" \
  "$ROCKCHIP_BUILD_DIR/lib/package-runtime.sh"; then
  printf 'Runtime packaging must use only %s.\n' "\$ORIGIN/lib" >&2
  exit 1
fi

while read -r component_type component_name; do
  config_name=$(
    printf 'CONFIG_%s_%s\n' "$component_name" "$component_type" |
      tr '[:lower:]-' '[:upper:]_'
  )
  grep -Fqx "    $config_name" "$ROCKCHIP_BUILD_DIR/build-ffmpeg.sh"
done < <(
  sed -nE \
    's/^[[:space:]]*--enable-(protocol|demuxer|muxer|parser|bsf|decoder|encoder|filter)=([^[:space:]]+)$/\1 \2/p' \
    "$ROCKCHIP_BUILD_DIR/build-ffmpeg.sh"
)

"$TOOLS_DIR/test-build-state.sh"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/fake-yolo.py" --self-test
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/fake-yolo.py" \
    --validate-schedule \
    "$ROCKCHIP_BUILD_DIR/tests/roi/effect-schedule.json"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/analyze.py" --self-test
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/fake-yolo.py" \
    --validate-schedule \
    "$ROCKCHIP_BUILD_DIR/tests/roi/production-q15-schedule.json"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/fake-yolo.py" \
    --validate-schedule \
    "$ROCKCHIP_BUILD_DIR/tests/roi/production-q25-schedule.json"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/analyze-production.py" --self-test
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/analyze-production-repeats.py" --self-test
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/analyze-production.py" \
    --validate-schedules \
    "$ROCKCHIP_BUILD_DIR/tests/roi/production-q15-schedule.json" \
    "$ROCKCHIP_BUILD_DIR/tests/roi/production-q25-schedule.json"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/roi-watchdog-bridge.py" --self-test
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/fake-yolo-watchdog.py" --self-test
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/fake-yolo-watchdog.py" \
    --validate-schedule \
    "$ROCKCHIP_BUILD_DIR/tests/roi/watchdog-schedule.json"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROCKCHIP_BUILD_DIR/tests/roi/analyze-watchdog.py" --self-test
python3 -m json.tool \
  "$ROCKCHIP_BUILD_DIR/tests/roi/effect-schedule.json" >/dev/null
python3 -m json.tool \
  "$ROCKCHIP_BUILD_DIR/tests/roi/production-q15-schedule.json" >/dev/null
python3 -m json.tool \
  "$ROCKCHIP_BUILD_DIR/tests/roi/production-q25-schedule.json" >/dev/null
python3 -m json.tool \
  "$ROCKCHIP_BUILD_DIR/tests/roi/watchdog-schedule.json" >/dev/null

printf 'Rockchip build checks passed.\n'
