#!/usr/bin/env bash

# Container-side build orchestrator invoked by the repository build entry point.
# It prepares caches and Rockchip dependencies, builds FFmpeg, then packages it.
# shellcheck disable=SC2016

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/config.sh"
source "$SCRIPT_DIR/lib/git-helpers.sh"
source "$SCRIPT_DIR/lib/build-dependencies.sh"
source "$SCRIPT_DIR/build-ffmpeg.sh"
source "$SCRIPT_DIR/lib/package-runtime.sh"

usage() {
  printf 'Usage: %s <rk3588|rk3576|rv1126b>\n' "$0" >&2
}

assert_linux_arm64() {
  local machine
  local system

  system=$(uname -s)
  machine=$(uname -m)
  case "$system/$machine" in
    Linux/aarch64 | Linux/arm64)
      ;;
    *)
      printf 'Unsupported build platform: %s/%s. Linux ARM64 is required.\n' \
        "$system" "$machine" >&2
      exit 1
      ;;
  esac
}

show_ccache_stats() {
  local ccache_stats

  if ! command -v ccache >/dev/null 2>&1; then
    return
  fi

  if ccache_stats=$(ccache --print-stats 2>/dev/null); then
    printf '%s\n' "$ccache_stats" |
      LC_ALL=C awk -F '\t' '
        $1 == "cache_miss" { misses = $2 }
        $1 == "cache_size_kibibyte" { size = $2 }
        $1 == "direct_cache_hit" { direct_hits = $2 }
        $1 == "files_in_cache" { files = $2 }
        $1 == "preprocessed_cache_hit" { preprocessed_hits = $2 }
        END {
          printf \
            "ccache: hits=%d direct_hits=%d preprocessed_hits=%d " \
            "misses=%d files=%d size_kibibyte=%d\n",
            direct_hits + preprocessed_hits,
            direct_hits,
            preprocessed_hits,
            misses,
            files,
            size
        }
      '
  else
    printf 'ccache: statistics unavailable\n' >&2
  fi

  if [ -n "${ROCKCHIP_BUILD_TRACE:-}" ]; then
    ccache --show-stats || true
  fi
}

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

assert_linux_arm64

target=$1
rockchip_load_target "$target"

SOURCE_DIR=${SOURCE_DIR:-/workspace}
if [ ! -x "$SOURCE_DIR/configure" ]; then
  printf 'FFmpeg source tree not found at %s\n' "$SOURCE_DIR" >&2
  exit 1
fi

cd "$SOURCE_DIR"

if [ -n "${ROCKCHIP_BUILD_TRACE:-}" ]; then
  set -x
fi

source_sha=${SOURCE_SHA:-${GITHUB_SHA:-}}
if [ -z "$source_sha" ]; then
  source_sha=$(git -C "$SOURCE_DIR" rev-parse HEAD)
fi
# Consumed by the packaging function sourced above.
# shellcheck disable=SC2034
source_state_sha=${SOURCE_STATE_SHA:-$source_sha}
source_dirty=${SOURCE_DIRTY:-false}
source_revision=${SOURCE_REVISION:-${source_sha:0:7}}

case "$source_dirty" in
  true | false)
    ;;
  *)
    printf 'Invalid SOURCE_DIRTY value: %s\n' "$source_dirty" >&2
    exit 2
    ;;
esac

# FFmpeg otherwise derives a checkout-dependent abbreviation length from
# `git describe`, which differs between a full local clone and Actions.
export revision=$source_revision
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(
  git -C "$SOURCE_DIR" show -s --format=%ct "$source_sha"
)}

export GIT_TERMINAL_PROMPT=0
export GIT_RETRY_ATTEMPTS=${GIT_RETRY_ATTEMPTS:-5}
export GIT_RETRY_INITIAL_DELAY_SECONDS=${GIT_RETRY_INITIAL_DELAY_SECONDS:-10}

: "${MPP_SHA:?MPP_SHA must be set by config.sh}"
: "${RGA_SHA:?RGA_SHA must be set by config.sh}"
mpp_sha=$MPP_SHA
rga_sha=$RGA_SHA
builder_fingerprint=$(rockchip_builder_fingerprint)
dependency_input_hash=$(rockchip_dependency_input_hash "$builder_fingerprint")
deps_cache_key=$(
  rockchip_dependency_cache_key \
    "$mpp_sha" "$rga_sha" "$dependency_input_hash"
)
printf 'Dependency cache: builder=%s input_hash=%s key=%s\n' \
  "$builder_fingerprint" "$dependency_input_hash" "$deps_cache_key"
BUILD_ROOT="$SOURCE_DIR/.build/rockchip/$target"
DEPS_PREFIX="$SOURCE_DIR/.rockchip-cache/deps/$target/$deps_cache_key"
INSTALL_PREFIX="$SOURCE_DIR/dist/$target"
PACKAGE_DIR="$SOURCE_DIR/artifact/$ROCKCHIP_ARTIFACT"
PACKAGE_MANIFEST="$PACKAGE_DIR.mtree"
FFMPEG_BUILD_DIR="$BUILD_ROOT/ffmpeg"
CCACHE_DIR="$SOURCE_DIR/.rockchip-cache/ccache/$target/$ROCKCHIP_CCACHE_CACHE_VERSION"

export BUILD_ROOT
export DEPS_PREFIX
export INSTALL_PREFIX
export PACKAGE_DIR
export PACKAGE_MANIFEST
export FFMPEG_BUILD_DIR
export CCACHE_DIR
export CCACHE_MAXSIZE=${CCACHE_MAXSIZE:-750M}
export CCACHE_COMPILERCHECK=${CCACHE_COMPILERCHECK:-content}
export CCACHE_BASEDIR="$SOURCE_DIR"
export TARGET_CFLAGS="$ROCKCHIP_TARGET_CFLAGS"
export TARGET_LDFLAGS="$ROCKCHIP_TARGET_LDFLAGS"

mkdir -p "$BUILD_ROOT" "$CCACHE_DIR"
ccache --set-config=max_size="$CCACHE_MAXSIZE"
ccache --set-config=compiler_check="$CCACHE_COMPILERCHECK"
ccache --zero-stats >/dev/null
if [ -n "${ROCKCHIP_BUILD_TRACE:-}" ]; then
  ccache --show-config
fi
trap show_ccache_stats EXIT

deps_metadata="$DEPS_PREFIX/.rockchip-build-metadata"
deps_cache_hit=false
if [ -f "$deps_metadata" ] &&
  grep -Fqx "mpp_sha=$mpp_sha" "$deps_metadata" &&
  grep -Fqx "rga_sha=$rga_sha" "$deps_metadata" &&
  grep -Fqx "builder_fingerprint=$builder_fingerprint" "$deps_metadata" &&
  grep -Fqx "dependency_input_hash=$dependency_input_hash" "$deps_metadata" &&
  grep -Fqx "target_cflags=$TARGET_CFLAGS" "$deps_metadata" &&
  grep -Fqx "target_ldflags=$TARGET_LDFLAGS" "$deps_metadata"; then
  deps_cache_hit=true
fi

if [ "$deps_cache_hit" = false ]; then
  printf 'Rockchip dependencies: cache miss\n'
  build_rockchip_dependencies "$mpp_sha" "$rga_sha"

  {
    printf 'mpp_sha=%s\n' "$mpp_sha"
    printf 'rga_sha=%s\n' "$rga_sha"
    printf 'builder_fingerprint=%s\n' "$builder_fingerprint"
    printf 'dependency_input_hash=%s\n' "$dependency_input_hash"
    printf 'target_cflags=%s\n' "$TARGET_CFLAGS"
    printf 'target_ldflags=%s\n' "$TARGET_LDFLAGS"
  } >"$deps_metadata"
else
  printf 'Rockchip dependencies: cache hit\n'
fi

export PKG_CONFIG_PATH="$DEPS_PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

mpp_version=$(pkg-config --modversion rockchip_mpp)
rga_version=$(pkg-config --modversion librga)
drm_version=$(pkg-config --modversion libdrm)
printf 'Dependency versions: MPP=%s RGA=%s DRM=%s\n' \
  "$mpp_version" "$rga_version" "$drm_version"

rm -rf -- "$INSTALL_PREFIX" "$PACKAGE_DIR"
rm -f -- "$PACKAGE_MANIFEST"
mkdir -p "$INSTALL_PREFIX"

build_ffmpeg
package_rockchip_runtime
