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
  mkdir -p "$case_root/patches"
  printf '# dependency build logic\n' \
    >"$case_root/lib/build-dependencies.sh"
  printf '# MPP ROI patch\n' \
    >"$case_root/patches/mpp-roi-utils-public.patch"

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

  printf '# changed MPP ROI patch\n' \
    >>"$case_root/patches/mpp-roi-utils-public.patch"
  actual_hash=$(rockchip_dependency_input_hash builder-a)
  assert_not_equal \
    "$baseline_hash" \
    "$actual_hash" \
    "MPP ROI patch did not change the dependency input hash"
  printf '# MPP ROI patch\n' \
    >"$case_root/patches/mpp-roi-utils-public.patch"

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
  local git_log="$TEST_ROOT/rga/git.log"
  local meson_log="$TEST_ROOT/rga/meson-environment.log"
  local ninja_log="$TEST_ROOT/rga/ninja-environment.log"
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

  git() {
    case "$*" in
      *" apply --check "*)
        printf 'check\t%s\n' "$*" >>"$git_log"
        ;;
      *" apply "*)
        printf 'apply\t%s\n' "$*" >>"$git_log"
        ;;
      *)
        fail "dependency build used an unexpected git operation: $*"
        ;;
    esac
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

  ninja() {
    assert_equal "ccache gcc" "${CC-}" "unexpected RGA install CC"
    assert_equal "ccache g++" "${CXX-}" "unexpected RGA install CXX"
    assert_equal "$TARGET_CFLAGS" "${CFLAGS-}" "unexpected RGA install CFLAGS"
    assert_equal \
      "$TARGET_CFLAGS -fpermissive" \
      "${CXXFLAGS-}" \
      "unexpected RGA install CXXFLAGS"
    assert_equal \
      "$TARGET_LDFLAGS" \
      "${LDFLAGS-}" \
      "unexpected RGA install LDFLAGS"
    printf 'ninja %s\n' "$*" >>"$ninja_log"
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
    2 \
    "$(wc -l <"$meson_log" | tr -d '[:space:]')" \
    "unexpected number of mocked Meson calls"
  if grep -Fv 'meson setup ' "$meson_log" >/dev/null; then
    fail "dependency build used an unsupported Meson subcommand"
  fi
  assert_equal \
    4 \
    "$(wc -l <"$ninja_log" | tr -d '[:space:]')" \
    "unexpected number of mocked Ninja calls"
  assert_equal \
    2 \
    "$(awk -v command="ninja -C $BUILD_ROOT/dependencies/rkrga-build" \
      '$0 == command { count++ } END { print count + 0 }' "$ninja_log")" \
    "unexpected RGA build command"
  assert_equal \
    2 \
    "$(awk \
      -v command="ninja -C $BUILD_ROOT/dependencies/rkrga-build install" \
      '$0 == command { count++ } END { print count + 0 }' "$ninja_log")" \
    "unexpected RGA install command"
  assert_equal \
    2 \
    "$(awk -F '\t' '$1 == "check" { count++ } END { print count + 0 }' "$git_log")" \
    "MPP ROI patch preflight was not repeated for each dependency build"
  assert_equal \
    2 \
    "$(awk -F '\t' '$1 == "apply" { count++ } END { print count + 0 }' "$git_log")" \
    "MPP ROI patch application was not repeated for each dependency build"
  if grep -Fv 'mpp-roi-utils-public.patch' "$git_log" >/dev/null; then
    fail "dependency build used an unexpected git operation"
  fi
)

test_ffmpeg_stamp_transitions() (
  local test_target=$1
  local case_root="$TEST_ROOT/ffmpeg-stamp-$test_target"
  local configure_log="$case_root/configure.log"
  local first_log="$case_root/first.log"
  local first_stamp="$case_root/first.stamp"
  local fourth_log="$case_root/fourth.log"
  local make_log="$case_root/make.log"
  local second_log="$case_root/second.log"
  local third_log="$case_root/third.log"
  local third_stamp="$case_root/third.stamp"

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
    "$case_root/frame-decision-sdk/include" \
    "$DEPS_PREFIX" \
    "$CCACHE_DIR"
  printf 'sdk\n' \
    >"$case_root/frame-decision-sdk/include/frame_decision_sdk.h"
  printf 'protocol-v3\n' \
    >"$case_root/frame-decision-sdk/include/frame_decision_protocol_v3.h"
  printf 'async-v3\n' \
    >"$case_root/frame-decision-sdk/include/frame_decision_async.h"
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
    --enable-zlib)
      printf 'CONFIG_ZLIB=yes\n' >>ffbuild/config.mak
      ;;
    --enable-libframe-decision)
      printf 'CONFIG_LIBFRAME_DECISION=yes\n' >>ffbuild/config.mak
      ;;
  esac
done

if [ -n "${ROCKCHIP_TEST_MISSING_CONFIG:-}" ]; then
  sed -i "/^${ROCKCHIP_TEST_MISSING_CONFIG}=yes$/d" ffbuild/config.mak
fi

printf 'configure\n' >>"$ROCKCHIP_TEST_CONFIGURE_LOG"
EOF

  cat >"$case_root/fake-bin/make" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'make %s\n' "$*" >>"$ROCKCHIP_TEST_MAKE_LOG"
if [[ " $* " = *" libavfilter/tests/rkrga_common "* ]]; then
  mkdir -p libavfilter/tests
  printf '#!/usr/bin/env bash\nexit 0\n' \
    >libavfilter/tests/rkrga_common
  chmod 0755 libavfilter/tests/rkrga_common
fi
if [[ " $* " = *" libavfilter/tests/frame_decision_roi "* ]]; then
  mkdir -p libavfilter/tests
  printf '#!/usr/bin/env bash\nexit 0\n' \
    >libavfilter/tests/frame_decision_roi
  chmod 0755 libavfilter/tests/frame_decision_roi
fi
if [[ " $* " = *" libavfilter/tests/frame_decision_roi_control "* ]]; then
  mkdir -p libavfilter/tests
  printf '#!/usr/bin/env bash\nexit 0\n' \
    >libavfilter/tests/frame_decision_roi_control
  chmod 0755 libavfilter/tests/frame_decision_roi_control
fi
EOF

  chmod 0755 "$SOURCE_DIR/configure" "$case_root/fake-bin/make"
  export PATH="$case_root/fake-bin:$PATH"
  export ROCKCHIP_TEST_CONFIGURE_LOG="$configure_log"
  export ROCKCHIP_TEST_MAKE_LOG="$make_log"

  export target=$test_target
  export FRAME_DECISION_SDK_PREFIX="$case_root/frame-decision-sdk"
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
  grep -Eq 'make .* fate-parser( |$)' "$make_log"
  grep -Eq 'make .* fate-parser-timestamp( |$)' "$make_log"
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

  printf 'async-v3-changed\n' \
    >>"$FRAME_DECISION_SDK_PREFIX/include/frame_decision_async.h"
  build_ffmpeg >"$third_log"
  grep -Fq \
    "Resetting FFmpeg build directory: configuration stamp changed" \
    "$third_log"
  if cmp -s "$first_stamp" "$FFMPEG_BUILD_DIR/.rockchip-config-stamp"; then
    fail "frame-decision header content did not change the FFmpeg stamp"
  fi
  grep -Fq \
    "frame_decision_sdk_prefix=$FRAME_DECISION_SDK_PREFIX" \
    "$FFMPEG_BUILD_DIR/.rockchip-config-stamp"
  grep -Eq \
    '^frame_decision_headers_sha256=[0-9a-f]{64}$' \
    "$FFMPEG_BUILD_DIR/.rockchip-config-stamp"
  cp "$FFMPEG_BUILD_DIR/.rockchip-config-stamp" "$third_stamp"

  printf '\n# test-only build script change\n' >>"$SCRIPT_DIR/build-ffmpeg.sh"
  build_ffmpeg >"$fourth_log"
  grep -Fq \
    "Resetting FFmpeg build directory: configuration stamp changed" \
    "$fourth_log"
  if cmp -s "$third_stamp" "$FFMPEG_BUILD_DIR/.rockchip-config-stamp"; then
    fail "FFmpeg build script change did not change the configuration stamp"
  fi

  assert_absent "$FFMPEG_BUILD_DIR/manual-object.o"
  assert_absent "$FFMPEG_BUILD_DIR/stale-state"
  assert_exists "$BUILD_ROOT/sibling.keep"
  assert_exists "$DEPS_PREFIX/cache.keep"
  assert_exists "$CCACHE_DIR/cache.keep"
  assert_equal \
    4 \
    "$(wc -l <"$configure_log" | tr -d '[:space:]')" \
    "unexpected number of mocked configure calls"
  assert_equal \
    8 \
    "$(wc -l <"$make_log" | tr -d '[:space:]')" \
    "unexpected number of mocked make calls"

  if [ "$test_target" = rk3576 ]; then
    local missing_config

    for missing_config in \
      CONFIG_PNG_DECODER \
      CONFIG_IMAGE2_DEMUXER \
      CONFIG_ZLIB; do
      if ROCKCHIP_TEST_MISSING_CONFIG=$missing_config \
        build_ffmpeg >"$case_root/missing-config.log" 2>&1; then
        fail "configuration gate accepted missing $missing_config"
      fi
      grep -Fq "Required FFmpeg configuration is missing: $missing_config" \
        "$case_root/missing-config.log"
    done
  fi
)

test_bundled_zlib() (
  local case_root="$TEST_ROOT/bundled-zlib"
  local fixture_ldd_output="$case_root/ldd.txt"
  local library
  local library_dir="$case_root/package/lib"

  mkdir -p "$library_dir" "$case_root/system"
  for library in librga.so.2 librockchip_mpp.so.1 libz.so.1; do
    : >"$library_dir/$library"
  done
  : >"$case_root/system/libz.so.1"
  env() {
    assert_equal "-u LD_LIBRARY_PATH ldd" "$1 $2 $3" \
      "runtime verification did not clear LD_LIBRARY_PATH before ldd"
    cat "$fixture_ldd_output"
  }
  BUILD_ROOT=$case_root

  source "$ROCKCHIP_BUILD_DIR/lib/package-runtime.sh"

  for library in librga.so.2 librockchip_mpp.so.1; do
    printf '%s => %s/%s (0x0)\n' "$library" "$library_dir" "$library"
  done >"$case_root/ldd-base.txt"
  cp "$case_root/ldd-base.txt" "$fixture_ldd_output"
  printf 'libz.so.1 => %s/libz.so.1 (0x0)\n' "$library_dir" \
    >>"$fixture_ldd_output"
  rockchip_verify_binary_libraries \
    "$case_root/package/ffmpeg" "$library_dir" artifact ffmpeg

  cp "$case_root/ldd-base.txt" "$fixture_ldd_output"
  if rockchip_verify_binary_libraries \
    "$case_root/package/ffmpeg" "$library_dir" artifact ffmpeg \
    >"$case_root/missing.log" 2>&1; then
    fail "runtime verification accepted missing libz.so.1"
  fi

  printf 'libz.so.1 => %s/system/libz.so.1 (0x0)\n' "$case_root" \
    >>"$fixture_ldd_output"
  if rockchip_verify_binary_libraries \
    "$case_root/package/ffmpeg" "$library_dir" artifact ffmpeg \
    >"$case_root/system.log" 2>&1; then
    fail "runtime verification accepted libz.so.1 from the system"
  fi
)

test_internal_artifact_manifest() (
  local case_root="$TEST_ROOT/artifact-manifest"
  local manifest_tmp="$case_root/MANIFEST.tmp"
  local package_dir="$case_root/package"

  mkdir -p "$package_dir/lib"
  printf 'binary\n' >"$package_dir/ffmpeg"
  printf 'library\n' >"$package_dir/lib/libexample.so"
  chmod 0755 "$package_dir/ffmpeg" "$package_dir/lib/libexample.so"

  source "$ROCKCHIP_BUILD_DIR/lib/package-runtime.sh"
  PACKAGE_DIR=$package_dir \
    rockchip_generate_artifact_manifest "$manifest_tmp"
  mv -- "$manifest_tmp" "$package_dir/MANIFEST.mtree"

  "$TOOLS_DIR/compare-artifacts.sh" --verify "$package_dir"
  if grep -Fq 'MANIFEST.mtree' \
    <(grep -v '^#' "$package_dir/MANIFEST.mtree"); then
    fail "internal manifest unexpectedly includes itself"
  fi

  printf 'changed\n' >>"$package_dir/ffmpeg"
  if "$TOOLS_DIR/compare-artifacts.sh" --verify "$package_dir" \
    >"$case_root/invalid.log" 2>&1; then
    fail "artifact verification accepted modified payload"
  fi
)

test_glibc_abi_ceiling() (
  local case_root="$TEST_ROOT/glibc-abi"
  local empty_binary="$case_root/empty"
  local focal_binary="$case_root/focal"
  local legacy_binary="$case_root/legacy"
  local newer_binary="$case_root/newer"

  mkdir -p "$case_root"
  : >"$empty_binary"
  : >"$focal_binary"
  : >"$legacy_binary"
  : >"$newer_binary"

  readelf() {
    case "${*: -1}" in
      "$empty_binary")
        printf 'No version information found in this file.\n'
        ;;
      "$focal_binary")
        printf 'Name: GLIBC_2.17\nName: GLIBC_2.31\n'
        ;;
      "$legacy_binary")
        printf 'Name: GLIBC_2.17\n'
        ;;
      "$newer_binary")
        printf 'Name: GLIBC_2.34\n'
        ;;
      *)
        return 1
        ;;
    esac
  }

  dpkg() {
    [ "$1" = --compare-versions ] || return 2
    case "$2:$3:$4" in
      2.17:le:2.31 | 2.31:le:2.31 | 2.17:gt:2.31 | 2.31:gt:2.17 | \
        2.34:gt:2.31)
        return 0
        ;;
      2.17:gt:2.17 | 2.31:gt:2.31 | 2.34:le:2.31)
        return 1
        ;;
      *)
        fail "unexpected mocked dpkg comparison: $2 $3 $4"
        ;;
    esac
  }

  source "$ROCKCHIP_BUILD_DIR/lib/package-runtime.sh"

  assert_equal \
    2.31 \
    "$(rockchip_required_glibc_version "$focal_binary")" \
    "highest GLIBC requirement was not selected"
  rockchip_verify_glibc_abi 2.31 "$legacy_binary" "$focal_binary"
  assert_equal \
    2.31 \
    "$ROCKCHIP_GLIBC_MAX_REQUIRED" \
    "artifact-wide GLIBC maximum was not recorded"

  if rockchip_verify_glibc_abi 2.31 "$newer_binary" \
    >"$case_root/newer.log" 2>&1; then
    fail "GLIBC ABI gate accepted a requirement above the ceiling"
  fi
  if rockchip_verify_glibc_abi 2.31 "$empty_binary" \
    >"$case_root/empty.log" 2>&1; then
    fail "GLIBC ABI gate accepted an ELF without GLIBC requirements"
  fi
)

test_dependency_input_hash_boundaries
test_rga_environment_isolation
test_ffmpeg_stamp_transitions rk3588
test_ffmpeg_stamp_transitions rk3576
test_ffmpeg_stamp_transitions rv1126b
test_bundled_zlib
test_internal_artifact_manifest
test_glibc_abi_ceiling

printf 'Rockchip build state tests passed.\n'
