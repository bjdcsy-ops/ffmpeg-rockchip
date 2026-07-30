#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/config.sh"

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <rk3588|rk3576|rv1126b>\n' "$0" >&2
  exit 2
fi

target=$1
rockchip_load_target "$target"

printf 'artifact=%s\n' "$ROCKCHIP_ARTIFACT"
printf 'package_path=artifact/%s\n' "$ROCKCHIP_ARTIFACT"
