#!/usr/bin/env bash

set -euo pipefail

REPOSITORY_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SCRIPT_DIR="$REPOSITORY_ROOT/build/rockchip"
source "$SCRIPT_DIR/config.sh"

usage() {
  cat >&2 <<EOF
Usage:
  $0 <rk3588|rk3576|rv1126b|all>
  $0 clean <rk3588|rk3576|rv1126b|all>
EOF
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
      printf 'Unsupported host platform: %s/%s. Linux ARM64 is required.\n' \
        "$system" "$machine" >&2
      exit 1
      ;;
  esac
}

assert_docker_arm64() {
  local docker_arch
  local docker_os

  if ! command -v docker >/dev/null 2>&1; then
    printf 'Docker is required to build ffmpeg-rockchip.\n' >&2
    exit 1
  fi

  docker_os=$(docker info --format '{{.OSType}}')
  docker_arch=$(docker info --format '{{.Architecture}}')
  case "$docker_os/$docker_arch" in
    linux/aarch64 | linux/arm64)
      ;;
    *)
      printf 'Unsupported Docker platform: %s/%s. Linux ARM64 is required.\n' \
        "$docker_os" "$docker_arch" >&2
      exit 1
      ;;
  esac
}

expand_targets() {
  local requested=$1

  if [ "$requested" = all ]; then
    printf '%s\n' "${ROCKCHIP_TARGETS[@]}"
    return
  fi

  rockchip_load_target "$requested"
  printf '%s\n' "$requested"
}

clean_target() {
  local target=$1

  rockchip_load_target "$target"
  rm -rf -- \
    "$REPOSITORY_ROOT/.build/rockchip/$target" \
    "$REPOSITORY_ROOT/.rockchip-cache/deps/$target" \
    "$REPOSITORY_ROOT/.rockchip-cache/ccache/$target" \
    "$REPOSITORY_ROOT/dist/$target" \
    "$REPOSITORY_ROOT/artifact/$ROCKCHIP_ARTIFACT" \
    "$REPOSITORY_ROOT/artifact/$ROCKCHIP_ARTIFACT.tar.gz"
  printf 'Cleaned build state for %s\n' "$target"
}

build_target() {
  local -a docker_args
  local source_sha
  local target=$1
  local variable_name

  source_sha=$(git -C "$REPOSITORY_ROOT" rev-parse HEAD)
  docker_args=(
    run
    --rm
    --init
    --user "$(id -u):$(id -g)"
    --workdir /workspace
    --volume "$REPOSITORY_ROOT:/workspace"
    --env HOME=/tmp/ffmpeg-rockchip-build-home
    --env SOURCE_DIR=/workspace
    --env "SOURCE_SHA=$source_sha"
  )

  for variable_name in \
    MPP_SHA \
    RGA_SHA \
    GIT_RETRY_ATTEMPTS \
    GIT_RETRY_INITIAL_DELAY_SECONDS \
    CCACHE_MAXSIZE \
    ROCKCHIP_BUILD_TRACE \
    MPP_REPOSITORY \
    MPP_BRANCH \
    RGA_REPOSITORY \
    RGA_BRANCH \
    ROCKCHIP_DEPS_CACHE_VERSION; do
    if [ -n "${!variable_name:-}" ]; then
      docker_args+=(--env "$variable_name=${!variable_name}")
    fi
  done

  docker "${docker_args[@]}" "$ROCKCHIP_BUILD_IMAGE" "$target"
}

assert_linux_arm64

command_name=build
if [ "${1:-}" = clean ]; then
  command_name=clean
  shift
fi

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

mapfile -t selected_targets < <(expand_targets "$1")

if [ "$command_name" = clean ]; then
  for target in "${selected_targets[@]}"; do
    clean_target "$target"
  done
  exit 0
fi

assert_docker_arm64
docker build \
  --file "$SCRIPT_DIR/Dockerfile" \
  --tag "$ROCKCHIP_BUILD_IMAGE" \
  "$SCRIPT_DIR"

for target in "${selected_targets[@]}"; do
  build_target "$target"
done
