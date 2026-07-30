#!/usr/bin/env bash

# FFmpeg-specific configure flags and compile/install commands.
# This file is sourced by build.sh after Rockchip dependencies are ready.
# Globals referenced below are initialized by build.sh before this function runs.
# shellcheck disable=SC2154

build_ffmpeg() {
  local ambient_environment_sha
  local -a configure_args
  local configure_args_sha
  local configure_sha
  local compiler_sha
  local -a required_configs
  local build_ffmpeg_sha
  local config_name
  local environment_name
  local stamp_changed
  local stamp_file
  local stamp_tmp

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
    "--cc=$SCRIPT_DIR/lib/compiler.sh gcc"
    "--cxx=$SCRIPT_DIR/lib/compiler.sh g++"
    "--host-cc=$SCRIPT_DIR/lib/compiler.sh gcc"
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

  configure_args_sha=$(
    printf '%s\0' "${configure_args[@]}" |
      sha256sum |
      awk '{ print $1 }'
  )
  ambient_environment_sha=$(
    for environment_name in \
      PATH \
      PKG_CONFIG_PATH \
      PKG_CONFIG_LIBDIR \
      PKG_CONFIG_SYSROOT_DIR \
      CC \
      CXX \
      CPPFLAGS \
      CFLAGS \
      CXXFLAGS \
      ASFLAGS \
      OBJCFLAGS \
      LDFLAGS \
      LDEXEFLAGS \
      LDSOFLAGS \
      AR \
      LD \
      NM \
      RANLIB \
      STRIP \
      CPATH \
      C_INCLUDE_PATH \
      CPLUS_INCLUDE_PATH \
      LIBRARY_PATH \
      LD_LIBRARY_PATH; do
      printf '%s\0%s\0' \
        "$environment_name" \
        "${!environment_name-}"
    done |
      sha256sum |
      awk '{ print $1 }'
  )
  build_ffmpeg_sha=$(
    sha256sum "$SCRIPT_DIR/build-ffmpeg.sh" | awk '{ print $1 }'
  )
  compiler_sha=$(
    sha256sum "$SCRIPT_DIR/lib/compiler.sh" | awk '{ print $1 }'
  )
  configure_sha=$(
    sha256sum "$SOURCE_DIR/configure" | awk '{ print $1 }'
  )
  FFMPEG_BUILD_CONFIG_SHA256=$(
    printf '%s\0' \
      'stamp_format=1' \
      "configure_args_sha256=$configure_args_sha" \
      "ambient_environment_sha256=$ambient_environment_sha" \
      "build_ffmpeg_sha256=$build_ffmpeg_sha" \
      "compiler_sha256=$compiler_sha" \
      "configure_sha256=$configure_sha" \
      "builder_fingerprint=$builder_fingerprint" \
      "dependency_cache_key=$deps_cache_key" \
      "dependency_input_hash=$dependency_input_hash" \
      "source_date_epoch=$SOURCE_DATE_EPOCH" |
      sha256sum |
      awk '{ print $1 }'
  )
  export FFMPEG_BUILD_CONFIG_SHA256

  if [ -L "$FFMPEG_BUILD_DIR" ]; then
    printf 'Refusing symlinked FFmpeg build directory: %s\n' \
      "$FFMPEG_BUILD_DIR" >&2
    return 1
  fi

  stamp_file="$FFMPEG_BUILD_DIR/.rockchip-config-stamp"
  stamp_tmp=$(mktemp "$BUILD_ROOT/.ffmpeg-config-stamp.XXXXXX")
  {
    printf 'stamp_format=1\n'
    printf 'ffmpeg_build_config_sha256=%s\n' \
      "$FFMPEG_BUILD_CONFIG_SHA256"
    printf 'configure_args_sha256=%s\n' "$configure_args_sha"
    printf 'ambient_environment_sha256=%s\n' \
      "$ambient_environment_sha"
    printf 'build_ffmpeg_sha256=%s\n' "$build_ffmpeg_sha"
    printf 'compiler_sha256=%s\n' "$compiler_sha"
    printf 'configure_sha256=%s\n' "$configure_sha"
    printf 'builder_fingerprint=%s\n' "$builder_fingerprint"
    printf 'dependency_cache_key=%s\n' "$deps_cache_key"
    printf 'dependency_input_hash=%s\n' "$dependency_input_hash"
    printf 'source_date_epoch=%s\n' "$SOURCE_DATE_EPOCH"
    printf 'target=%s\n' "$target"
    printf 'ffmpeg_arch=%s\n' "$ROCKCHIP_FFMPEG_ARCH"
    printf 'ffmpeg_cpu=%s\n' "$ROCKCHIP_FFMPEG_CPU"
    printf 'target_cflags=%s\n' "$TARGET_CFLAGS"
    printf 'target_ldflags=%s\n' "$TARGET_LDFLAGS"
  } >"$stamp_tmp"

  stamp_changed=true
  if [ -f "$stamp_file" ] && cmp -s "$stamp_tmp" "$stamp_file"; then
    stamp_changed=false
    printf 'Reusing FFmpeg build directory: configuration stamp unchanged\n'
  else
    if [ -d "$FFMPEG_BUILD_DIR" ]; then
      printf 'Resetting FFmpeg build directory: configuration stamp changed\n'
    else
      printf 'Preparing FFmpeg build directory: no configuration stamp found\n'
    fi
    rm -rf -- "$FFMPEG_BUILD_DIR"
  fi

  mkdir -p "$FFMPEG_BUILD_DIR"
  if ! (
    cd "$FFMPEG_BUILD_DIR" || exit 1
    if [ -e src ] && [ ! -L src ]; then
      printf 'FFmpeg build path exists but is not a symlink: %s/src\n' \
        "$FFMPEG_BUILD_DIR" >&2
      exit 1
    fi
    ln -sfnT "$SOURCE_DIR" src
    ./src/configure "${configure_args[@]}"
  ); then
    rm -f -- "$stamp_tmp" "$stamp_file"
    return 1
  fi

  if [ "$stamp_changed" = true ]; then
    mv -- "$stamp_tmp" "$stamp_file"
  else
    rm -f -- "$stamp_tmp"
  fi

  (
    cd "$FFMPEG_BUILD_DIR" || exit 1
    rm -f -- .version
    make -j"$(nproc)"
    make install
  )

  required_configs=(
    CONFIG_H264_RKMPP_DECODER
    CONFIG_HEVC_RKMPP_DECODER
    CONFIG_PCM_MULAW_DECODER
    CONFIG_H264_RKMPP_ENCODER
    CONFIG_HEVC_RKMPP_ENCODER
    CONFIG_PCM_MULAW_ENCODER
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
    CONFIG_ARESAMPLE_FILTER
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
    if ! grep -qx "${config_name}=yes" \
      "$FFMPEG_BUILD_DIR/ffbuild/config.mak"; then
      printf 'Required FFmpeg configuration is missing: %s\n' \
        "$config_name" >&2
      return 1
    fi
  done
  printf 'FFmpeg configuration check passed: required_configs=%d\n' \
    "${#required_configs[@]}"
}
