#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/config.sh"
source "$SCRIPT_DIR/git-helpers.sh"

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
  if command -v ccache >/dev/null 2>&1; then
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

# FFmpeg otherwise derives a checkout-dependent abbreviation length from
# `git describe`, which differs between a full local clone and Actions.
export revision=${source_sha:0:7}
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(
  git -C "$SOURCE_DIR" show -s --format=%ct HEAD
)}

export GIT_TERMINAL_PROMPT=0
export GIT_RETRY_ATTEMPTS=${GIT_RETRY_ATTEMPTS:-5}
export GIT_RETRY_INITIAL_DELAY_SECONDS=${GIT_RETRY_INITIAL_DELAY_SECONDS:-10}

mpp_sha=${MPP_SHA:-}
rga_sha=${RGA_SHA:-}
if [ -z "$mpp_sha" ]; then
  mpp_sha=$(git_branch_sha "$MPP_REPOSITORY" "$MPP_BRANCH")
fi
if [ -z "$rga_sha" ]; then
  rga_sha=$(git_branch_sha "$RGA_REPOSITORY" "$RGA_BRANCH")
fi

deps_cache_key=$(rockchip_dependency_cache_key "$mpp_sha" "$rga_sha")
BUILD_ROOT="$SOURCE_DIR/.build/rockchip/$target"
DEPS_PREFIX="$SOURCE_DIR/.rockchip-cache/deps/$target/$deps_cache_key"
INSTALL_PREFIX="$SOURCE_DIR/dist/$target"
PACKAGE_DIR="$SOURCE_DIR/artifact/$ROCKCHIP_ARTIFACT"
PACKAGE_ARCHIVE="$SOURCE_DIR/artifact/$ROCKCHIP_ARTIFACT.tar.gz"
FFMPEG_BUILD_DIR="$BUILD_ROOT/ffmpeg"
CCACHE_DIR="$SOURCE_DIR/.rockchip-cache/ccache/$target"

export BUILD_ROOT
export DEPS_PREFIX
export INSTALL_PREFIX
export PACKAGE_DIR
export PACKAGE_ARCHIVE
export CCACHE_DIR
export CCACHE_MAXSIZE=${CCACHE_MAXSIZE:-750M}
export CCACHE_COMPILERCHECK=${CCACHE_COMPILERCHECK:-content}
export CCACHE_BASEDIR="$SOURCE_DIR"
export TARGET_CFLAGS="$ROCKCHIP_TARGET_CFLAGS"
export TARGET_LDFLAGS="$ROCKCHIP_TARGET_LDFLAGS"

mkdir -p "$BUILD_ROOT" "$CCACHE_DIR"
ccache --set-config=max_size="$CCACHE_MAXSIZE"
ccache --set-config=compiler_check="$CCACHE_COMPILERCHECK"
ccache --zero-stats
ccache --show-config
trap show_ccache_stats EXIT

deps_metadata="$DEPS_PREFIX/.rockchip-build-metadata"
deps_cache_hit=false
if [ -f "$deps_metadata" ] &&
  grep -Fqx "mpp_sha=$mpp_sha" "$deps_metadata" &&
  grep -Fqx "rga_sha=$rga_sha" "$deps_metadata" &&
  grep -Fqx "target_cflags=$TARGET_CFLAGS" "$deps_metadata" &&
  grep -Fqx "target_ldflags=$TARGET_LDFLAGS" "$deps_metadata"; then
  deps_cache_hit=true
fi

if [ "$deps_cache_hit" = false ]; then
  deps_build_root="$BUILD_ROOT/dependencies"
  rm -rf -- "$deps_build_root" "$DEPS_PREFIX"
  mkdir -p "$deps_build_root" "$DEPS_PREFIX"

  git_clone_branch_with_retry \
    "$MPP_REPOSITORY" "$MPP_BRANCH" "$deps_build_root/rkmpp"
  git_verify_head "$deps_build_root/rkmpp" "$mpp_sha"

  mpp_cmake_args=(
    -DCMAKE_INSTALL_PREFIX="$DEPS_PREFIX"
    -DCMAKE_INSTALL_LIBDIR=lib
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_C_FLAGS_RELEASE="$TARGET_CFLAGS -DNDEBUG"
    -DCMAKE_CXX_FLAGS_RELEASE="$TARGET_CFLAGS -DNDEBUG"
    -DCMAKE_C_COMPILER_LAUNCHER=ccache
    -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
    -DBUILD_SHARED_LIBS=ON
    -DBUILD_TEST=OFF
  )

  cmake \
    -S "$deps_build_root/rkmpp" \
    -B "$deps_build_root/rkmpp-build" \
    "${mpp_cmake_args[@]}"
  cmake --build "$deps_build_root/rkmpp-build" --parallel "$(nproc)"
  cmake --install "$deps_build_root/rkmpp-build"

  git_clone_branch_with_retry \
    "$RGA_REPOSITORY" "$RGA_BRANCH" "$deps_build_root/rkrga"
  git_verify_head "$deps_build_root/rkrga" "$rga_sha"

  export CC="ccache gcc"
  export CXX="ccache g++"
  export CFLAGS="$TARGET_CFLAGS"
  export CXXFLAGS="$TARGET_CFLAGS -fpermissive"
  export LDFLAGS="$TARGET_LDFLAGS"

  rga_meson_args=(
    --prefix="$DEPS_PREFIX"
    --libdir=lib
    --buildtype=release
    --default-library=shared
    -Dlibdrm=false
    -Dlibrga_demo=false
  )

  meson setup \
    "$deps_build_root/rkrga" \
    "$deps_build_root/rkrga-build" \
    "${rga_meson_args[@]}"
  meson compile -C "$deps_build_root/rkrga-build"
  meson install -C "$deps_build_root/rkrga-build"

  {
    printf 'mpp_sha=%s\n' "$mpp_sha"
    printf 'rga_sha=%s\n' "$rga_sha"
    printf 'target_cflags=%s\n' "$TARGET_CFLAGS"
    printf 'target_ldflags=%s\n' "$TARGET_LDFLAGS"
  } >"$deps_metadata"
else
  printf 'Using cached Rockchip dependencies from %s\n' "$DEPS_PREFIX"
fi

export PKG_CONFIG_PATH="$DEPS_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

pkg-config --modversion rockchip_mpp
pkg-config --modversion librga
pkg-config --modversion libdrm
pkg-config --cflags --libs rockchip_mpp librga libdrm
find "$DEPS_PREFIX" -maxdepth 3 -type f -print | sort

mkdir -p "$FFMPEG_BUILD_DIR"
rm -rf -- "$INSTALL_PREFIX" "$PACKAGE_DIR"
rm -f -- "$PACKAGE_ARCHIVE"
mkdir -p "$INSTALL_PREFIX" "$PACKAGE_DIR"

configure_args=(
  --prefix="$INSTALL_PREFIX"
  --disable-debug
  --disable-doc
  --disable-ffplay
  --disable-everything
  --disable-autodetect
  --enable-gpl
  --enable-version3
  --enable-lto=auto
  "--cc=ccache gcc"
  "--cxx=ccache g++"
  "--host-cc=ccache gcc"
  --enable-libdrm
  --enable-rkmpp
  --enable-rkrga
  --enable-network
  --enable-protocol=file
  --enable-protocol=pipe
  --enable-protocol=tcp
  --enable-protocol=udp
  --enable-protocol=rtp
  --enable-protocol=http
  --enable-demuxer=rtsp
  --enable-demuxer=rtp
  --enable-demuxer=sdp
  --enable-demuxer=mpegts
  --enable-demuxer=h264
  --enable-demuxer=hevc
  --enable-demuxer=mov
  --enable-muxer=rtsp
  --enable-muxer=rtp
  --enable-muxer=rtp_mpegts
  --enable-muxer=mpegts
  --enable-muxer=h264
  --enable-muxer=hevc
  --enable-muxer=mp4
  --enable-muxer=mov
  --enable-muxer=null
  --enable-parser=h264
  --enable-parser=hevc
  --enable-bsf=h264_mp4toannexb
  --enable-bsf=hevc_mp4toannexb
  --enable-decoder=h264_rkmpp
  --enable-decoder=hevc_rkmpp
  --enable-decoder=pcm_mulaw
  --enable-encoder=h264_rkmpp
  --enable-encoder=hevc_rkmpp
  --enable-encoder=pcm_mulaw
  --enable-filter=aresample
  --enable-filter=hwdownload
  --enable-filter=hwmap
  --enable-filter=hwupload
  --enable-filter=scale_rkrga
  --enable-filter=vpp_rkrga
  --enable-filter=overlay_rkrga
  --enable-filter=format
  --enable-filter=null
  "--arch=$ROCKCHIP_FFMPEG_ARCH"
  "--cpu=$ROCKCHIP_FFMPEG_CPU"
  "--extra-cflags=-I$DEPS_PREFIX/include $TARGET_CFLAGS"
  "--extra-ldflags=-L$DEPS_PREFIX/lib $TARGET_LDFLAGS"
)

(
  cd "$FFMPEG_BUILD_DIR"
  "$SOURCE_DIR/configure" "${configure_args[@]}"
  make -j"$(nproc)"
  make install
)

patchelf --set-rpath '$ORIGIN/../lib:$ORIGIN/lib' \
  "$INSTALL_PREFIX/bin/ffmpeg"
patchelf --set-rpath '$ORIGIN/../lib:$ORIGIN/lib' \
  "$INSTALL_PREFIX/bin/ffprobe"
readelf -d "$INSTALL_PREFIX/bin/ffmpeg" |
  grep -F '$ORIGIN/../lib:$ORIGIN/lib'
readelf -d "$INSTALL_PREFIX/bin/ffprobe" |
  grep -F '$ORIGIN/../lib:$ORIGIN/lib'

required_configs=(
  CONFIG_H264_RKMPP_DECODER
  CONFIG_HEVC_RKMPP_DECODER
  CONFIG_H264_RKMPP_ENCODER
  CONFIG_HEVC_RKMPP_ENCODER
  CONFIG_RTSP_DEMUXER
  CONFIG_RTP_DEMUXER
  CONFIG_SDP_DEMUXER
  CONFIG_MPEGTS_DEMUXER
  CONFIG_H264_DEMUXER
  CONFIG_HEVC_DEMUXER
  CONFIG_MOV_DEMUXER
  CONFIG_RTSP_MUXER
  CONFIG_RTP_MUXER
  CONFIG_RTP_MPEGTS_MUXER
  CONFIG_MPEGTS_MUXER
  CONFIG_H264_MUXER
  CONFIG_HEVC_MUXER
  CONFIG_MP4_MUXER
  CONFIG_MOV_MUXER
  CONFIG_NULL_MUXER
  CONFIG_H264_PARSER
  CONFIG_HEVC_PARSER
  CONFIG_H264_MP4TOANNEXB_BSF
  CONFIG_HEVC_MP4TOANNEXB_BSF
  CONFIG_FILE_PROTOCOL
  CONFIG_PIPE_PROTOCOL
  CONFIG_TCP_PROTOCOL
  CONFIG_UDP_PROTOCOL
  CONFIG_RTP_PROTOCOL
  CONFIG_HTTP_PROTOCOL
  CONFIG_HWDOWNLOAD_FILTER
  CONFIG_HWMAP_FILTER
  CONFIG_HWUPLOAD_FILTER
  CONFIG_SCALE_RKRGA_FILTER
  CONFIG_VPP_RKRGA_FILTER
  CONFIG_OVERLAY_RKRGA_FILTER
  CONFIG_FORMAT_FILTER
  CONFIG_NULL_FILTER
)

for config_name in "${required_configs[@]}"; do
  grep -qx "${config_name}=yes" "$FFMPEG_BUILD_DIR/ffbuild/config.mak"
done

mkdir -p "$PACKAGE_DIR/lib"
cp -a "$INSTALL_PREFIX/." "$PACKAGE_DIR/"
cp -a "$INSTALL_PREFIX/bin/ffmpeg" "$PACKAGE_DIR/ffmpeg"
cp -a "$INSTALL_PREFIX/bin/ffprobe" "$PACKAGE_DIR/ffprobe"
rm -rf -- "$PACKAGE_DIR/bin"

shopt -s nullglob
cp -a "$DEPS_PREFIX"/lib/*.so* "$PACKAGE_DIR/lib/"

{
  printf 'target=%s\n' "$target"
  printf 'artifact=%s\n' "$ROCKCHIP_ARTIFACT"
  printf 'runner=%s\n' "$(uname -m)"
  printf 'ffmpeg_arch=%s\n' "$ROCKCHIP_FFMPEG_ARCH"
  printf 'ffmpeg_cpu=%s\n' "$ROCKCHIP_FFMPEG_CPU"
  printf 'target_cflags=%s\n' "$TARGET_CFLAGS"
  getconf GNU_LIBC_VERSION || true
} >"$PACKAGE_DIR/BUILDINFO.txt"

version_date=$(TZ=Asia/Shanghai date +%Y.%m.%d)
short_sha=${source_sha:0:7}
printf '%s-%s+%s\n' "$version_date" "$short_sha" "$target" \
  >"$PACKAGE_DIR/version.txt"

cat >"$PACKAGE_DIR/README.txt" <<EOF
ffmpeg-rockchip low-latency $target build

Built on Ubuntu 22.04 with LTO and target-specific CPU flags.
MPP and RGA are packaged under lib/. FFmpeg is intentionally
component-pruned for RTSP/RTP, H.264/HEVC rkmpp codecs, and RGA
filters instead of full general-purpose codec coverage.
Run ./ffmpeg from this directory so the embedded runtime path can
resolve the bundled lib/ directory.

Smoke checks:
  ./ffmpeg -decoders | grep rkmpp
  ./ffmpeg -encoders | grep rkmpp
  ./ffmpeg -filters | grep rkrga
EOF

test ! -e "$PACKAGE_DIR/bin"
grep -Eq "^[0-9]{4}\\.[0-9]{2}\\.[0-9]{2}-[0-9a-f]{7}\\+$target$" \
  "$PACKAGE_DIR/version.txt"
file "$PACKAGE_DIR/ffmpeg"
readelf -d "$PACKAGE_DIR/ffmpeg" |
  grep -F '$ORIGIN/../lib:$ORIGIN/lib'

verify_bundled_library() {
  local ldd_output=$1
  local soname=$2
  local library_path
  local resolved_path
  local package_lib

  library_path=$(
    awk -v soname="$soname" \
      '$1 == soname { print $3; found = 1 } END { if (!found) exit 1 }' \
      "$ldd_output"
  )
  resolved_path=$(realpath "$library_path")
  package_lib=$(realpath "$PACKAGE_DIR/lib")

  case "$resolved_path" in
    "$package_lib"/*)
      printf '%s resolves to bundled library %s\n' "$soname" "$resolved_path"
      ;;
    *)
      printf 'Expected %s to resolve under %s, got %s\n' \
        "$soname" "$package_lib" "$resolved_path" >&2
      exit 1
      ;;
  esac
}

env -u LD_LIBRARY_PATH ldd "$PACKAGE_DIR/ffmpeg" |
  tee "$BUILD_ROOT/ldd-root.txt"
verify_bundled_library "$BUILD_ROOT/ldd-root.txt" librga.so.2
verify_bundled_library "$BUILD_ROOT/ldd-root.txt" librockchip_mpp.so.1
"$PACKAGE_DIR/ffmpeg" -hide_banner -decoders |
  grep -E 'h264_rkmpp|hevc_rkmpp'
"$PACKAGE_DIR/ffmpeg" -hide_banner -encoders |
  grep -E 'h264_rkmpp|hevc_rkmpp'
"$PACKAGE_DIR/ffmpeg" -hide_banner -demuxers |
  grep -E 'rtsp|rtp|sdp|mpegts'
"$PACKAGE_DIR/ffmpeg" -hide_banner -muxers |
  grep -E 'rtsp|rtp|mpegts'
"$PACKAGE_DIR/ffmpeg" -hide_banner -filters |
  grep -E 'hwdownload|hwmap|hwupload|overlay_rkrga|scale_rkrga|vpp_rkrga'

tar \
  --sort=name \
  --mtime="@$SOURCE_DATE_EPOCH" \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  -C "$SOURCE_DIR/artifact" \
  -cf - \
  "$ROCKCHIP_ARTIFACT" |
  gzip -n >"$PACKAGE_ARCHIVE"

printf 'Build completed: %s\n' "$PACKAGE_DIR"
printf 'Package archive: %s\n' "$PACKAGE_ARCHIVE"
