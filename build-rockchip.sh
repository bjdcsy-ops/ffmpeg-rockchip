#!/usr/bin/env bash

# Public entry point for local and GitHub Actions Rockchip builds.
# It prepares a Linux ARM64 Docker environment and runs build/rockchip/build.sh.

set -euo pipefail

REPOSITORY_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SCRIPT_DIR="$REPOSITORY_ROOT/build/rockchip"
source "$SCRIPT_DIR/config.sh"

usage() {
  cat >&2 <<EOF
Usage:
  $0 <rk3588|rk3576|rv1126b|all>
  $0 clean <rk3588|rk3576|rv1126b|all>
  $0 image
EOF
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

build_image() {
  docker build \
    --platform linux/arm64 \
    --file "$SCRIPT_DIR/Dockerfile" \
    --tag "$ROCKCHIP_BUILD_IMAGE" \
    "$SCRIPT_DIR"
}

repository_is_dirty() {
  if ! git -C "$REPOSITORY_ROOT" diff --quiet HEAD --; then
    return 0
  fi

  [ -n "$(
    git -C "$REPOSITORY_ROOT" ls-files --others --exclude-standard
  )" ]
}

calculate_source_state_sha() {
  local untracked_path

  {
    printf 'head=%s\n' "$1"
    git -C "$REPOSITORY_ROOT" diff --binary HEAD --
    while IFS= read -r -d '' untracked_path; do
      printf 'untracked=%s\n' "$untracked_path"
      git -C "$REPOSITORY_ROOT" hash-object -- "$untracked_path"
    done < <(
      git -C "$REPOSITORY_ROOT" \
        ls-files -z --others --exclude-standard
    )
  } | git hash-object --stdin
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
    "$REPOSITORY_ROOT/artifact/$ROCKCHIP_ARTIFACT.mtree"
  printf 'Cleaned build state for %s\n' "$target"
}

build_target() {
  local -a docker_args
  local source_dirty
  local source_revision
  local source_sha
  local source_state_sha
  local target=$1
  local variable_name

  source_sha=$(git -C "$REPOSITORY_ROOT" rev-parse HEAD)
  source_dirty=false
  source_state_sha=$source_sha
  if repository_is_dirty; then
    source_dirty=true
    source_state_sha=$(calculate_source_state_sha "$source_sha")
  fi

  if [ "${GITHUB_ACTIONS:-false}" = true ] && [ "$source_dirty" = true ]; then
    printf 'GitHub Actions release builds require a clean source tree.\n' >&2
    exit 1
  fi

  source_revision=${source_sha:0:7}
  if [ "$source_dirty" = true ]; then
    source_revision="$source_revision-dirty.${source_state_sha:0:7}"
  fi

  docker_args=(
    run
    --rm
    --init
    --platform linux/arm64
    --user "$(id -u):$(id -g)"
    --workdir /workspace
    --volume "$REPOSITORY_ROOT:/workspace"
    --env HOME=/tmp/ffmpeg-rockchip-build-home
    --env SOURCE_DIR=/workspace
    --env "SOURCE_SHA=$source_sha"
    --env "SOURCE_STATE_SHA=$source_state_sha"
    --env "SOURCE_DIRTY=$source_dirty"
    --env "SOURCE_REVISION=$source_revision"
  )

  for variable_name in \
    GIT_RETRY_ATTEMPTS \
    GIT_RETRY_INITIAL_DELAY_SECONDS \
    CCACHE_MAXSIZE \
    CCACHE_COMPILERCHECK \
    ROCKCHIP_BUILD_TRACE \
    MPP_REPOSITORY \
    RGA_REPOSITORY \
    ROCKCHIP_CCACHE_CACHE_VERSION; do
    if [ -n "${!variable_name:-}" ]; then
      docker_args+=(--env "$variable_name=${!variable_name}")
    fi
  done

  docker "${docker_args[@]}" "$ROCKCHIP_BUILD_IMAGE" "$target"
}

if [ "${1:-}" = image ]; then
  if [ "$#" -ne 1 ]; then
    usage
    exit 2
  fi
  assert_docker_arm64
  build_image
  exit 0
fi

command_name=build
if [ "${1:-}" = clean ]; then
  command_name=clean
  shift
fi

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

if [ "$1" = all ]; then
  selected_targets=("${ROCKCHIP_TARGETS[@]}")
else
  rockchip_load_target "$1"
  selected_targets=("$1")
fi

if [ "$command_name" = clean ]; then
  for target in "${selected_targets[@]}"; do
    clean_target "$target"
  done
  exit 0
fi

assert_docker_arm64
build_image

for target in "${selected_targets[@]}"; do
  build_target "$target"
done
