#!/usr/bin/env bash

# Exercises dependency-environment and FFmpeg stamp transitions without
# network access or a real compilation.

set -euo pipefail

TOOLS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROCKCHIP_BUILD_DIR=$(cd -- "$TOOLS_DIR/.." && pwd)
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ffmpeg-rockchip-state-test.XXXXXX")
trap 'rm -rf -- "$TEST_ROOT"' EXIT

fail() {
  printf 'Build state test failed: %s\n' "$1" >&2
  exit 1
}

assert_equal() {
  local expected=$1
  local actual=$2
  local message=$3

  if [ "$actual" != "$expected" ]; then
    printf 'Build state test failed: %s\n' "$message" >&2
    printf '  expected: %s\n' "$expected" >&2
    printf '  actual:   %s\n' "$actual" >&2
    exit 1
  fi
}

assert_not_equal() {
  local unexpected=$1
  local actual=$2
  local message=$3

  if [ "$actual" = "$unexpected" ]; then
    printf 'Build state test failed: %s\n' "$message" >&2
    printf '  unexpected: %s\n' "$unexpected" >&2
    exit 1
  fi
}

assert_exists() {
  local path=$1

  [ -e "$path" ] || fail "expected path to exist: $path"
}

assert_absent() {
  local path=$1

  [ ! -e "$path" ] || fail "expected path to be absent: $path"
}

test_dependency_input_hash_boundaries() (
  local actual_hash
  local baseline_hash
  local case_root="$TEST_ROOT/dependency-input-hash"
  local irrelevant_file

  mkdir -p "$case_root/lib"
  printf '# dependency build logic\n' \
    >"$case_root/lib/build-dependencies.sh"

  for irrelevant_file in \
    Dockerfile \
    config.sh \
    dependencies.lock \
    lib/git-helpers.sh; do
    mkdir -p "$(dirname -- "$case_root/$irrelevant_file")"
    printf 'initial irrelevant content\n' >"$case_root/$irrelevant_file"
  done

  source "$ROCKCHIP_BUILD_DIR/config.sh"
  export ROCKCHIP_CONFIG_DIR=$case_root
  export ROCKCHIP_TARGET_CFLAGS="-O2 -mcpu=test-cpu"
  export ROCKCHIP_TARGET_LDFLAGS="-Wl,--as-needed"

  rockchip_builder_fingerprint() {
    printf 'called\n' >"$case_root/fingerprint-called"
    printf 'unexpected-builder\n'
  }

  baseline_hash=$(rockchip_dependency_input_hash builder-a)
  assert_absent "$case_root/fingerprint-called"

  for irrelevant_file in \
    Dockerfile \
    config.sh \
    dependencies.lock \
    lib/git-helpers.sh; do
    printf 'changed irrelevant content\n' >>"$case_root/$irrelevant_file"
    actual_hash=$(rockchip_dependency_input_hash builder-a)
    assert_equal \
      "$baseline_hash" \
      "$actual_hash" \
      "$irrelevant_file unexpectedly changed the dependency input hash"
  done

  printf '# changed dependency build logic\n' \
    >>"$case_root/lib/build-dependencies.sh"
  actual_hash=$(rockchip_dependency_input_hash builder-a)
  assert_not_equal \
    "$baseline_hash" \
    "$actual_hash" \
    "build-dependencies.sh did not change the dependency input hash"
  printf '# dependency build logic\n' \
    >"$case_root/lib/build-dependencies.sh"

  ROCKCHIP_TARGET_CFLAGS="-O3 -mcpu=test-cpu"
  actual_hash=$(rockchip_dependency_input_hash builder-a)
  assert_not_equal \
    "$baseline_hash" \
    "$actual_hash" \
    "target CFLAGS did not change the dependency input hash"
  ROCKCHIP_TARGET_CFLAGS="-O2 -mcpu=test-cpu"

  ROCKCHIP_TARGET_LDFLAGS="-Wl,-z,now"
  actual_hash=$(rockchip_dependency_input_hash builder-a)
  assert_not_equal \
    "$baseline_hash" \
    "$actual_hash" \
    "target LDFLAGS did not change the dependency input hash"
  ROCKCHIP_TARGET_LDFLAGS="-Wl,--as-needed"

  actual_hash=$(rockchip_dependency_input_hash builder-b)
  assert_not_equal \
    "$baseline_hash" \
    "$actual_hash" \
    "builder fingerprint did not change the dependency input hash"
  assert_absent "$case_root/fingerprint-called"
)

test_rga_environment_isolation() (
  local meson_log="$TEST_ROOT/rga/meson-environment.log"
  local variable_name

  mkdir -p "$(dirname -- "$meson_log")"

  BUILD_ROOT="$TEST_ROOT/rga/build"
  DEPS_PREFIX="$TEST_ROOT/rga/deps"
  TARGET_CFLAGS="-O2 -mcpu=test-cpu"
  TARGET_LDFLAGS="-Wl,--as-needed"
  export MPP_REPOSITORY=https://invalid.example/mpp.git
  export RGA_REPOSITORY=https://invalid.example/rga.git

  source "$ROCKCHIP_BUILD_DIR/lib/build-dependencies.sh"

  git_clone_commit_with_retry() {
    mkdir -p -- "$3"
  }

  git_verify_head() {
    :
  }

  cmake() {
    :
  }

  meson() {
    assert_equal "ccache gcc" "${CC-}" "unexpected RGA CC"
    assert_equal "ccache g++" "${CXX-}" "unexpected RGA CXX"
    assert_equal "$TARGET_CFLAGS" "${CFLAGS-}" "unexpected RGA CFLAGS"
    assert_equal \
      "$TARGET_CFLAGS -fpermissive" \
      "${CXXFLAGS-}" \
      "unexpected RGA CXXFLAGS"
    assert_equal "$TARGET_LDFLAGS" "${LDFLAGS-}" "unexpected RGA LDFLAGS"
    printf 'meson %s\n' "$*" >>"$meson_log"
  }

  export CC=outer-cc
  export CXX=outer-cxx
  export CFLAGS=outer-cflags
  export CXXFLAGS=outer-cxxflags
  export LDFLAGS=outer-ldflags

  build_rockchip_dependencies first-mpp first-rga

  assert_equal outer-cc "$CC" "RGA changed the caller CC"
  assert_equal outer-cxx "$CXX" "RGA changed the caller CXX"
  assert_equal outer-cflags "$CFLAGS" "RGA changed the caller CFLAGS"
  assert_equal \
    outer-cxxflags \
    "$CXXFLAGS" \
    "RGA changed the caller CXXFLAGS"
  assert_equal outer-ldflags "$LDFLAGS" "RGA changed the caller LDFLAGS"

  unset CC CXX CFLAGS CXXFLAGS LDFLAGS
  build_rockchip_dependencies second-mpp second-rga

  for variable_name in CC CXX CFLAGS CXXFLAGS LDFLAGS; do
    if declare -p "$variable_name" >/dev/null 2>&1; then
      fail "RGA left $variable_name set in an initially clean environment"
    fi
  done

  assert_equal \
    6 \
    "$(wc -l <"$meson_log" | tr -d '[:space:]')" \
    "unexpected number of mocked Meson calls"
)

test_ffmpeg_stamp_transitions() (
  local case_root="$TEST_ROOT/ffmpeg-stamp"
  local configure_log="$TEST_ROOT/ffmpeg-stamp/configure.log"
  local first_log="$TEST_ROOT/ffmpeg-stamp/first.log"
  local first_stamp="$TEST_ROOT/ffmpeg-stamp/first.stamp"
  local make_log="$TEST_ROOT/ffmpeg-stamp/make.log"
  local second_log="$TEST_ROOT/ffmpeg-stamp/second.log"
  local third_log="$TEST_ROOT/ffmpeg-stamp/third.log"

  SCRIPT_DIR="$case_root/scripts"
  SOURCE_DIR="$case_root/source"
  BUILD_ROOT="$case_root/build"
  FFMPEG_BUILD_DIR="$BUILD_ROOT/ffmpeg"
  export INSTALL_PREFIX="$case_root/install"
  DEPS_PREFIX="$case_root/dependency-cache"
  CCACHE_DIR="$case_root/ccache"

  mkdir -p \
    "$SCRIPT_DIR/lib" \
    "$SOURCE_DIR" \
    "$BUILD_ROOT" \
    "$case_root/fake-bin" \
    "$DEPS_PREFIX" \
    "$CCACHE_DIR"
  cp "$ROCKCHIP_BUILD_DIR/build-ffmpeg.sh" "$SCRIPT_DIR/build-ffmpeg.sh"
  cp "$ROCKCHIP_BUILD_DIR/lib/compiler.sh" "$SCRIPT_DIR/lib/compiler.sh"

  cat >"$SOURCE_DIR/configure" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

mkdir -p ffbuild
: >ffbuild/config.mak

for argument in "$@"; do
  case "$argument" in
    --enable-protocol=* | \
      --enable-demuxer=* | \
      --enable-muxer=* | \
      --enable-parser=* | \
      --enable-bsf=* | \
      --enable-decoder=* | \
      --enable-encoder=* | \
      --enable-filter=*)
      config_type=${argument#--enable-}
      config_type=${config_type%%=*}
      config_name=${argument#*=}
      config_type=$(
        printf '%s' "$config_type" | tr '[:lower:]-' '[:upper:]_'
      )
      config_name=$(
        printf '%s' "$config_name" | tr '[:lower:]-' '[:upper:]_'
      )
      printf 'CONFIG_%s_%s=yes\n' "$config_name" "$config_type" \
        >>ffbuild/config.mak
      ;;
  esac
done

printf 'configure\n' >>"$ROCKCHIP_TEST_CONFIGURE_LOG"
EOF

  cat >"$case_root/fake-bin/make" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'make %s\n' "$*" >>"$ROCKCHIP_TEST_MAKE_LOG"
EOF

  chmod 0755 "$SOURCE_DIR/configure" "$case_root/fake-bin/make"
  export PATH="$case_root/fake-bin:$PATH"
  export ROCKCHIP_TEST_CONFIGURE_LOG="$configure_log"
  export ROCKCHIP_TEST_MAKE_LOG="$make_log"

  export target=rk3576
  export ROCKCHIP_FFMPEG_ARCH=aarch64
  export ROCKCHIP_FFMPEG_CPU=test-cpu
  TARGET_CFLAGS="-O2 -mcpu=test-cpu"
  TARGET_LDFLAGS=""
  export builder_fingerprint=test-builder
  export deps_cache_key=test-dependency-cache
  export dependency_input_hash=test-dependency-input
  export SOURCE_DATE_EPOCH=1700000000

  source "$SCRIPT_DIR/build-ffmpeg.sh"

  build_ffmpeg >"$first_log"
  grep -Fq \
    "Preparing FFmpeg build directory: no configuration stamp found" \
    "$first_log"

  cp "$FFMPEG_BUILD_DIR/.rockchip-config-stamp" "$first_stamp"
  mkdir -p "$FFMPEG_BUILD_DIR/stale-state"
  printf 'object\n' >"$FFMPEG_BUILD_DIR/manual-object.o"
  printf 'stale\n' >"$FFMPEG_BUILD_DIR/stale-state/keep"
  printf 'keep\n' >"$BUILD_ROOT/sibling.keep"
  printf 'keep\n' >"$DEPS_PREFIX/cache.keep"
  printf 'keep\n' >"$CCACHE_DIR/cache.keep"

  build_ffmpeg >"$second_log"
  grep -Fq \
    "Reusing FFmpeg build directory: configuration stamp unchanged" \
    "$second_log"
  cmp -s "$first_stamp" "$FFMPEG_BUILD_DIR/.rockchip-config-stamp"
  assert_exists "$FFMPEG_BUILD_DIR/manual-object.o"
  assert_exists "$FFMPEG_BUILD_DIR/stale-state/keep"

  printf '\n# test-only build script change\n' >>"$SCRIPT_DIR/build-ffmpeg.sh"
  build_ffmpeg >"$third_log"
  grep -Fq \
    "Resetting FFmpeg build directory: configuration stamp changed" \
    "$third_log"
  if cmp -s "$first_stamp" "$FFMPEG_BUILD_DIR/.rockchip-config-stamp"; then
    fail "FFmpeg configuration stamp did not change"
  fi

  assert_absent "$FFMPEG_BUILD_DIR/manual-object.o"
  assert_absent "$FFMPEG_BUILD_DIR/stale-state"
  assert_exists "$BUILD_ROOT/sibling.keep"
  assert_exists "$DEPS_PREFIX/cache.keep"
  assert_exists "$CCACHE_DIR/cache.keep"
  assert_equal \
    3 \
    "$(wc -l <"$configure_log" | tr -d '[:space:]')" \
    "unexpected number of mocked configure calls"
  assert_equal \
    6 \
    "$(wc -l <"$make_log" | tr -d '[:space:]')" \
    "unexpected number of mocked make calls"
)

test_dependency_input_hash_boundaries
test_rga_environment_isolation
test_ffmpeg_stamp_transitions

printf 'Rockchip build state tests passed.\n'
