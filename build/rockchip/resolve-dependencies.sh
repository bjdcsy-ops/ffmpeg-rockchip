#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/config.sh"
source "$SCRIPT_DIR/git-helpers.sh"

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <rk3588|rk3576|rv1126b>\n' "$0" >&2
  exit 2
fi

target=$1
rockchip_load_target "$target"

mpp_sha=$(git_branch_sha "$MPP_REPOSITORY" "$MPP_BRANCH")
rga_sha=$(git_branch_sha "$RGA_REPOSITORY" "$RGA_BRANCH")
cache_key=$(rockchip_dependency_cache_key "$mpp_sha" "$rga_sha")
ccache_period=$(date -u +%G-W%V)

printf 'MPP revision: %s (%s)\n' "$mpp_sha" "$MPP_BRANCH" >&2
printf 'RGA revision: %s (%s)\n' "$rga_sha" "$RGA_BRANCH" >&2
printf 'Rockchip dependency cache: %s\n' "$cache_key" >&2
printf 'Compiler cache period: %s\n' "$ccache_period" >&2

printf 'mpp_sha=%s\n' "$mpp_sha"
printf 'rga_sha=%s\n' "$rga_sha"
printf 'cache_key=%s\n' "$cache_key"
printf 'ccache_period=%s\n' "$ccache_period"
printf 'ccache_cache_version=%s\n' "$ROCKCHIP_CCACHE_CACHE_VERSION"
