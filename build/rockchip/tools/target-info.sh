#!/usr/bin/env bash

set -euo pipefail

TOOLS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROCKCHIP_BUILD_DIR=$(cd -- "$TOOLS_DIR/.." && pwd)
source "$ROCKCHIP_BUILD_DIR/config.sh"

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <rk3588|rk3576|rv1126b>\n' "$0" >&2
  exit 2
fi

target=$1
rockchip_load_target "$target"

printf 'artifact=%s\n' "$ROCKCHIP_ARTIFACT"
