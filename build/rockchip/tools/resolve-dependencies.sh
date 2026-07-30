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

: "${MPP_SHA:?MPP_SHA must be set by config.sh}"
: "${RGA_SHA:?RGA_SHA must be set by config.sh}"
mpp_sha=$MPP_SHA
rga_sha=$RGA_SHA
builder_fingerprint=$(rockchip_builder_fingerprint)
dependency_input_hash=$(rockchip_dependency_input_hash "$builder_fingerprint")
dependency_cache_key=$(
  rockchip_dependency_cache_key \
    "$mpp_sha" "$rga_sha" "$dependency_input_hash"
)
ccache_period=$(date -u +%G-W%V)

printf 'MPP locked revision: %s\n' "$mpp_sha" >&2
printf 'RGA locked revision: %s\n' "$rga_sha" >&2
printf 'Builder fingerprint: %s\n' "$builder_fingerprint" >&2
printf 'Dependency input hash: %s\n' "$dependency_input_hash" >&2
printf 'Rockchip dependency cache: %s\n' "$dependency_cache_key" >&2
printf 'Compiler cache period: %s\n' "$ccache_period" >&2

printf 'builder_fingerprint=%s\n' "$builder_fingerprint"
printf 'dependency_cache_key=%s\n' "$dependency_cache_key"
printf 'dependency_cache_path=.rockchip-cache/deps/%s/%s\n' \
  "$target" "$dependency_cache_key"
printf 'ccache_period=%s\n' "$ccache_period"
printf 'ccache_cache_version=%s\n' "$ROCKCHIP_CCACHE_CACHE_VERSION"
printf 'ccache_path=.rockchip-cache/ccache/%s/%s\n' \
  "$target" "$ROCKCHIP_CCACHE_CACHE_VERSION"
