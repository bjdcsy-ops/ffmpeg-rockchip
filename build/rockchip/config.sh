#!/usr/bin/env bash

MPP_REPOSITORY=${MPP_REPOSITORY:-https://github.com/nyanmisaka/mpp.git}
MPP_BRANCH=${MPP_BRANCH:-jellyfin-mpp}
RGA_REPOSITORY=${RGA_REPOSITORY:-https://github.com/nyanmisaka/rk-mirrors.git}
RGA_BRANCH=${RGA_BRANCH:-jellyfin-rga}

ROCKCHIP_DEPS_CACHE_VERSION=${ROCKCHIP_DEPS_CACHE_VERSION:-v2}
ROCKCHIP_CCACHE_CACHE_VERSION=${ROCKCHIP_CCACHE_CACHE_VERSION:-v2}
ROCKCHIP_BUILD_IMAGE=${ROCKCHIP_BUILD_IMAGE:-ffmpeg-rockchip-build:ubuntu22-arm64}

ROCKCHIP_TARGETS=(rk3588 rk3576 rv1126b)

rockchip_load_target() {
  local target=$1

  case "$target" in
    rk3588)
      ROCKCHIP_ARTIFACT=ffmpeg-rockchip-rk3588-ubuntu22-arm64
      ROCKCHIP_FFMPEG_ARCH=aarch64
      ROCKCHIP_FFMPEG_CPU=cortex-a76.cortex-a55
      ROCKCHIP_TARGET_CFLAGS="-O3 -pipe -mcpu=cortex-a76.cortex-a55 -mtune=cortex-a76.cortex-a55 -fno-semantic-interposition"
      ROCKCHIP_TARGET_LDFLAGS=""
      ;;
    rk3576)
      ROCKCHIP_ARTIFACT=ffmpeg-rockchip-rk3576-ubuntu22-arm64
      ROCKCHIP_FFMPEG_ARCH=aarch64
      ROCKCHIP_FFMPEG_CPU=cortex-a72.cortex-a53
      ROCKCHIP_TARGET_CFLAGS="-O3 -pipe -mcpu=cortex-a72.cortex-a53 -mtune=cortex-a72.cortex-a53 -fno-semantic-interposition"
      ROCKCHIP_TARGET_LDFLAGS=""
      ;;
    rv1126b)
      ROCKCHIP_ARTIFACT=ffmpeg-rockchip-rv1126b-ubuntu22-arm64
      ROCKCHIP_FFMPEG_ARCH=aarch64
      ROCKCHIP_FFMPEG_CPU=cortex-a53
      ROCKCHIP_TARGET_CFLAGS="-O3 -pipe -mcpu=cortex-a53 -mtune=cortex-a53 -fno-semantic-interposition"
      ROCKCHIP_TARGET_LDFLAGS=""
      ;;
    *)
      printf 'Unsupported Rockchip target: %s\n' "$target" >&2
      printf 'Supported targets: %s\n' "${ROCKCHIP_TARGETS[*]}" >&2
      return 2
      ;;
  esac
}

rockchip_flags_key() {
  {
    printf 'target_cflags=%s\n' "$ROCKCHIP_TARGET_CFLAGS"
    printf 'target_ldflags=%s\n' "$ROCKCHIP_TARGET_LDFLAGS"
  } | sha256sum | cut -c1-12
}

rockchip_dependency_cache_key() {
  local mpp_sha=$1
  local rga_sha=$2
  local flags_key

  flags_key=$(rockchip_flags_key)
  printf '%s-mpp-%s-rga-%s-flags-%s\n' \
    "$ROCKCHIP_DEPS_CACHE_VERSION" \
    "${mpp_sha:0:12}" \
    "${rga_sha:0:12}" \
    "$flags_key"
}
