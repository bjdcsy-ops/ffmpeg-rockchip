#!/usr/bin/env bash

# Packages and verifies the staged FFmpeg runtime.
# This file is sourced by build.sh after FFmpeg has been installed.
# Globals referenced below are initialized by build.sh before these functions run.
# shellcheck disable=SC2016,SC2154

rockchip_verify_runpath() {
  local binary=$1
  local expected_runpath=$2
  local actual_runpath

  actual_runpath=$(patchelf --print-rpath "$binary")
  if [ "$actual_runpath" != "$expected_runpath" ]; then
    printf 'Unexpected RUNPATH for %s: expected %s, got %s\n' \
      "$binary" "$expected_runpath" "$actual_runpath" >&2
    return 1
  fi
}

rockchip_verify_bundled_library() {
  local ldd_output=$1
  local expected_lib_dir=$2
  local soname=$3
  local layout=$4
  local program=$5
  local library_path
  local relative_path
  local resolved_path
  local resolved_lib_dir

  library_path=$(
    awk -v soname="$soname" \
      '$1 == soname { print $3; found = 1 } END { if (!found) exit 1 }' \
      "$ldd_output"
  )
  resolved_path=$(realpath "$library_path")
  resolved_lib_dir=$(realpath "$expected_lib_dir")

  case "$resolved_path" in
    "$resolved_lib_dir"/*)
      relative_path=${resolved_path#"$resolved_lib_dir"/}
      printf \
        'Bundled runtime library: layout=%s program=%s soname=%s file=lib/%s\n' \
        "$layout" "$program" "$soname" "$relative_path"
      ;;
    *)
      printf 'Expected %s to resolve under %s, got %s\n' \
        "$soname" "$resolved_lib_dir" "$resolved_path" >&2
      return 1
      ;;
  esac
}

rockchip_verify_binary_libraries() {
  local binary=$1
  local expected_lib_dir=$2
  local layout=$3
  local program=$4
  local ldd_output="$BUILD_ROOT/ldd-$layout-$program.txt"

  if ! env -u LD_LIBRARY_PATH ldd "$binary" >"$ldd_output" 2>&1; then
    printf 'Failed to inspect runtime libraries for %s\n' "$binary" >&2
    cat "$ldd_output" >&2
    return 1
  fi
  if grep -Fq 'not found' "$ldd_output"; then
    printf 'Unresolved runtime library for %s\n' "$binary" >&2
    cat "$ldd_output" >&2
    return 1
  fi

  if ! rockchip_verify_bundled_library \
    "$ldd_output" "$expected_lib_dir" librga.so.2 "$layout" "$program"; then
    cat "$ldd_output" >&2
    return 1
  fi
  if ! rockchip_verify_bundled_library \
    "$ldd_output" \
    "$expected_lib_dir" \
    librockchip_mpp.so.1 \
    "$layout" \
    "$program"; then
    cat "$ldd_output" >&2
    return 1
  fi

  if [ -n "${ROCKCHIP_BUILD_TRACE:-}" ]; then
    printf 'Raw ldd output: layout=%s program=%s\n' "$layout" "$program"
    cat "$ldd_output"
  fi
}

rockchip_verify_arm64_binary() {
  local binary=$1

  if ! readelf -h "$binary" |
    grep -Eq '^[[:space:]]*Machine:[[:space:]]+AArch64$'; then
    printf 'Expected an AArch64 ELF binary: %s\n' "$binary" >&2
    return 1
  fi
}

rockchip_verify_binary_version() {
  local binary=$1
  local program=$2
  local version_output

  if ! version_output=$("$binary" -hide_banner -version 2>&1); then
    printf 'Version check failed: program=%s\n' "$program" >&2
    printf '%s\n' "$version_output" >&2
    return 1
  fi

  if [ -n "${ROCKCHIP_BUILD_TRACE:-}" ]; then
    printf 'Raw version output: program=%s\n%s\n' "$program" "$version_output"
  fi
  printf 'Version check passed: program=%s\n' "$program"
}

rockchip_verify_component_list() {
  local binary=$1
  local category=$2
  local list_option=$3
  local component
  local component_output

  shift 3
  if ! component_output=$("$binary" -hide_banner "$list_option" 2>&1); then
    printf 'Failed to query FFmpeg components: category=%s\n' \
      "$category" >&2
    printf '%s\n' "$component_output" >&2
    return 1
  fi

  for component in "$@"; do
    if ! awk -v expected="$component" '
      $2 == expected {
        found = 1
      }
      END {
        exit found ? 0 : 1
      }
    ' <<<"$component_output"; then
      printf 'Missing FFmpeg component: category=%s name=%s\n' \
        "$category" "$component" >&2
      return 1
    fi
  done
}

rockchip_verify_muxer_option() {
  local binary=$1
  local muxer=$2
  local option=$3
  local help_output

  if ! help_output=$("$binary" -hide_banner -h "muxer=$muxer" 2>&1); then
    printf 'Failed to query FFmpeg muxer options: muxer=%s\n' "$muxer" >&2
    printf '%s\n' "$help_output" >&2
    return 1
  fi
  if ! grep -Eq "^[[:space:]]+$option([[:space:]]|$)" <<<"$help_output"; then
    printf 'Missing FFmpeg muxer option: muxer=%s name=%s\n' \
      "$muxer" "$option" >&2
    return 1
  fi
}

rockchip_verify_encoder_option() {
  local binary=$1
  local encoder=$2
  local option=$3
  local help_output

  if ! help_output=$("$binary" -hide_banner -h "encoder=$encoder" 2>&1); then
    printf 'Failed to query FFmpeg encoder options: encoder=%s\n' \
      "$encoder" >&2
    printf '%s\n' "$help_output" >&2
    return 1
  fi
  if ! grep -Eq "^[[:space:]]+-$option([[:space:]]|$)" <<<"$help_output"; then
    printf 'Missing FFmpeg encoder option: encoder=%s name=%s\n' \
      "$encoder" "$option" >&2
    return 1
  fi
}

rockchip_write_buildinfo() {
  local artifact_version=$1
  local ffmpeg_script_sha
  local package_script_sha
  local source_date_utc
  local glibc_version

  ffmpeg_script_sha=$(
    sha256sum "$SCRIPT_DIR/build-ffmpeg.sh" | awk '{ print $1 }'
  )
  package_script_sha=$(
    sha256sum "$SCRIPT_DIR/lib/package-runtime.sh" | awk '{ print $1 }'
  )
  source_date_utc=$(
    TZ=UTC date --date="@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ
  )
  glibc_version=$(
    getconf GNU_LIBC_VERSION | awk '{ print $2 }'
  )

  {
    printf '# ffmpeg-rockchip build information\n'
    printf '# Format: key=value; ignore comments and blank lines.\n'
    printf '# Split data lines on the first "=" character.\n'
    printf 'buildinfo_format=2\n'
    printf '\n# Artifact\n'
    printf 'target=%s\n' "$target"
    printf 'artifact=%s\n' "$ROCKCHIP_ARTIFACT"
    printf 'artifact_version=%s\n' "$artifact_version"
    printf 'artifact_manifest=%s\n' "$(basename "$PACKAGE_MANIFEST")"
    printf 'manifest_format=mtree-v1\n'
    printf '\n# Source\n'
    printf 'source_sha=%s\n' "$source_sha"
    printf 'source_state_sha=%s\n' "$source_state_sha"
    printf 'source_dirty=%s\n' "$source_dirty"
    printf 'source_revision=%s\n' "$source_revision"
    printf 'source_date_epoch=%s\n' "$SOURCE_DATE_EPOCH"
    printf 'source_date_utc=%s\n' "$source_date_utc"
    printf '\n# Rockchip dependencies\n'
    printf 'mpp_repository=%s\n' "$MPP_REPOSITORY"
    printf 'mpp_sha=%s\n' "$mpp_sha"
    printf 'rga_repository=%s\n' "$RGA_REPOSITORY"
    printf 'rga_sha=%s\n' "$rga_sha"
    printf '\n# Build inputs\n'
    printf 'builder_fingerprint=%s\n' "$builder_fingerprint"
    printf 'dependency_input_hash=%s\n' "$dependency_input_hash"
    printf 'ffmpeg_script_sha256=%s\n' "$ffmpeg_script_sha"
    printf 'ffmpeg_build_config_sha256=%s\n' \
      "$FFMPEG_BUILD_CONFIG_SHA256"
    printf 'package_script_sha256=%s\n' "$package_script_sha"
    printf '\n# Builder and target\n'
    printf 'runner=%s\n' "$(uname -m)"
    printf 'glibc_version=%s\n' "$glibc_version"
    printf 'ffmpeg_arch=%s\n' "$ROCKCHIP_FFMPEG_ARCH"
    printf 'ffmpeg_cpu=%s\n' "$ROCKCHIP_FFMPEG_CPU"
    printf 'target_cflags=%s\n' "$TARGET_CFLAGS"
    printf 'target_ldflags=%s\n' "$TARGET_LDFLAGS"
  } >"$PACKAGE_DIR/BUILDINFO.txt"
}

rockchip_validate_buildinfo() {
  local expected_version=$1

  awk '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ {
      next
    }
    !/^[a-z][a-z0-9_]*=/ {
      printf "Invalid BUILDINFO line: %s\n", $0 >"/dev/stderr"
      failed = 1
      next
    }
    {
      key = $0
      sub(/=.*/, "", key)
      value = $0
      sub(/^[^=]*=/, "", value)
      if (seen[key]++) {
        printf "Duplicate BUILDINFO key: %s\n", key >"/dev/stderr"
        failed = 1
      }
      values[key] = value
    }
    END {
      required_keys = \
        "buildinfo_format artifact artifact_version artifact_manifest " \
        "manifest_format target source_sha source_state_sha source_dirty " \
        "source_revision source_date_epoch source_date_utc mpp_repository " \
        "mpp_sha rga_repository rga_sha builder_fingerprint " \
        "dependency_input_hash ffmpeg_script_sha256 " \
        "ffmpeg_build_config_sha256 package_script_sha256 runner " \
        "glibc_version ffmpeg_arch ffmpeg_cpu target_cflags target_ldflags"
      required_count = split(required_keys, required, " ")
      for (required_index = 1;
           required_index <= required_count;
           required_index++) {
        key = required[required_index]
        if (!seen[key]) {
          printf "BUILDINFO is missing required key: %s\n", key \
            >"/dev/stderr"
          failed = 1
        }
      }
      if (values["buildinfo_format"] != "2") {
        print "Unsupported BUILDINFO format." >"/dev/stderr"
        failed = 1
      }
      if (values["manifest_format"] != "mtree-v1") {
        print "Unsupported artifact manifest format." >"/dev/stderr"
        failed = 1
      }
      if (values["source_dirty"] != "true" &&
          values["source_dirty"] != "false") {
        print "Invalid source_dirty value." >"/dev/stderr"
        failed = 1
      }
      if (values["source_date_epoch"] !~ /^[0-9]+$/) {
        print "Invalid source_date_epoch value." >"/dev/stderr"
        failed = 1
      }
      exit failed
    }
  ' "$PACKAGE_DIR/BUILDINFO.txt"

  grep -Fqx "artifact_version=$expected_version" \
    "$PACKAGE_DIR/BUILDINFO.txt"
  grep -Fqx "$expected_version" "$PACKAGE_DIR/version.txt"
}

rockchip_write_runtime_readme() {
  cat >"$PACKAGE_DIR/README.txt" <<EOF
ffmpeg-rockchip $target build

Built on Ubuntu 22.04 with LTO and target-specific CPU flags.
Required MPP and RGA runtime libraries are packaged under lib/.
FFmpeg is intentionally component-pruned for RTSP/RTP, H.264/HEVC
rkmpp codecs, and RGA filters instead of full general-purpose codec
or SDK coverage.
Run ./ffmpeg from this directory so the embedded runtime path resolves
the bundled lib/ directory.

Artifact manifest:
  ./$(basename "$PACKAGE_MANIFEST")

Smoke checks:
  ./ffmpeg -decoders | grep rkmpp
  ./ffmpeg -encoders | grep rkmpp
  ./ffmpeg -filters | grep rkrga
EOF
}

rockchip_generate_artifact_manifest() {
  local output_file=$1

  {
    printf '# ffmpeg-rockchip artifact manifest\n'
    printf '# manifest_format=mtree-v1\n'
    printf '# keywords=type,mode,sha256digest,link\n'
    printf '# MANIFEST.mtree is excluded because a manifest cannot hash itself.\n'
    # Ubuntu emits the sha256 alias; macOS accepts only sha256digest.
    mtree \
      -c \
      -P \
      -k type,mode,sha256digest,link \
      -p "$PACKAGE_DIR" |
      awk '!/^#/ && NF { print }' |
      sed -E 's/(^|[[:space:]])sha256=/\1sha256digest=/'
  } >"$output_file"
}

package_rockchip_runtime() (
  set -euo pipefail
  shopt -s nullglob
  umask 022
  export LC_ALL=C

  local artifact_version
  local manifest_tmp
  local -a mpp_libraries
  local -a rga_libraries
  local version_date

  rga_libraries=("$DEPS_PREFIX"/lib/librga.so*)
  mpp_libraries=("$DEPS_PREFIX"/lib/librockchip_mpp.so*)
  if [ "${#rga_libraries[@]}" -eq 0 ] ||
    [ "${#mpp_libraries[@]}" -eq 0 ]; then
    printf 'MPP or RGA runtime libraries are missing from %s\n' \
      "$DEPS_PREFIX/lib" >&2
    return 1
  fi

  mkdir -p "$INSTALL_PREFIX/bin/lib"
  cp -a "${rga_libraries[@]}" "$INSTALL_PREFIX/bin/lib/"
  cp -a "${mpp_libraries[@]}" "$INSTALL_PREFIX/bin/lib/"

  patchelf --set-rpath '$ORIGIN/lib' \
    "$INSTALL_PREFIX/bin/ffmpeg"
  patchelf --set-rpath '$ORIGIN/lib' \
    "$INSTALL_PREFIX/bin/ffprobe"
  rockchip_verify_runpath \
    "$INSTALL_PREFIX/bin/ffmpeg" '$ORIGIN/lib'
  rockchip_verify_runpath \
    "$INSTALL_PREFIX/bin/ffprobe" '$ORIGIN/lib'
  rockchip_verify_binary_libraries \
    "$INSTALL_PREFIX/bin/ffmpeg" "$INSTALL_PREFIX/bin/lib" dist ffmpeg
  rockchip_verify_binary_libraries \
    "$INSTALL_PREFIX/bin/ffprobe" "$INSTALL_PREFIX/bin/lib" dist ffprobe

  mkdir -p "$PACKAGE_DIR/lib"
  cp -a "$INSTALL_PREFIX/bin/ffmpeg" "$PACKAGE_DIR/ffmpeg"
  cp -a "$INSTALL_PREFIX/bin/ffprobe" "$PACKAGE_DIR/ffprobe"
  cp -a "$INSTALL_PREFIX/bin/lib/." "$PACKAGE_DIR/lib/"

  rockchip_verify_runpath "$PACKAGE_DIR/ffmpeg" '$ORIGIN/lib'
  rockchip_verify_runpath "$PACKAGE_DIR/ffprobe" '$ORIGIN/lib'

  chmod 0755 "$PACKAGE_DIR" "$PACKAGE_DIR/lib"
  chmod 0755 "$PACKAGE_DIR/ffmpeg" "$PACKAGE_DIR/ffprobe"
  find "$PACKAGE_DIR/lib" -type f -name '*.so*' -exec chmod 0755 {} +

  version_date=$(
    TZ=Asia/Shanghai date --date="@$SOURCE_DATE_EPOCH" +%Y.%m.%d
  )
  artifact_version="$version_date-$source_revision+$target"
  printf '%s\n' "$artifact_version" >"$PACKAGE_DIR/version.txt"
  rockchip_write_buildinfo "$artifact_version"
  rockchip_write_runtime_readme
  chmod 0644 \
    "$PACKAGE_DIR/BUILDINFO.txt" \
    "$PACKAGE_DIR/README.txt" \
    "$PACKAGE_DIR/version.txt"
  rockchip_validate_buildinfo "$artifact_version"

  test ! -e "$PACKAGE_DIR/bin"
  test ! -e "$PACKAGE_DIR/include"
  test ! -e "$PACKAGE_DIR/share"
  test ! -e "$PACKAGE_DIR/lib/pkgconfig"
  grep -Eq \
    "^[0-9]{4}\\.[0-9]{2}\\.[0-9]{2}-[0-9a-f]{7}(-dirty\\.[0-9a-f]{7})?\\+$target$" \
    "$PACKAGE_DIR/version.txt"

  rockchip_verify_arm64_binary "$PACKAGE_DIR/ffmpeg"
  rockchip_verify_arm64_binary "$PACKAGE_DIR/ffprobe"
  rockchip_verify_binary_libraries \
    "$PACKAGE_DIR/ffmpeg" "$PACKAGE_DIR/lib" artifact ffmpeg
  rockchip_verify_binary_libraries \
    "$PACKAGE_DIR/ffprobe" "$PACKAGE_DIR/lib" artifact ffprobe
  rockchip_verify_binary_version "$PACKAGE_DIR/ffmpeg" ffmpeg
  rockchip_verify_binary_version "$PACKAGE_DIR/ffprobe" ffprobe
  rockchip_verify_component_list \
    "$PACKAGE_DIR/ffmpeg" decoders -decoders aac h264_rkmpp hevc_rkmpp
  rockchip_verify_component_list \
    "$PACKAGE_DIR/ffmpeg" encoders -encoders aac h264_rkmpp hevc_rkmpp
  rockchip_verify_component_list \
    "$PACKAGE_DIR/ffmpeg" demuxers -demuxers rtsp rtp sdp mpegts
  rockchip_verify_component_list \
    "$PACKAGE_DIR/ffmpeg" muxers -muxers rtsp rtp mpegts
  rockchip_verify_muxer_option \
    "$PACKAGE_DIR/ffmpeg" rtsp rtcp_from_packet
  rockchip_verify_encoder_option \
    "$PACKAGE_DIR/ffmpeg" hevc_rkmpp qp_ip
  rockchip_verify_component_list \
    "$PACKAGE_DIR/ffmpeg" \
    filters \
    -filters \
    asetpts \
    hwdownload \
    hwmap \
    hwupload \
    overlay_rkrga \
    scale_rkrga \
    setpts \
    vpp_rkrga
  printf '%s\n' \
    'FFmpeg component check passed: decoders=3 encoders=3 demuxers=4 muxers=3 filters=8 rtsp_options=1 encoder_options=1'

  manifest_tmp=$(mktemp "$BUILD_ROOT/.artifact-manifest.XXXXXX")
  trap 'rm -f -- "$manifest_tmp"' EXIT
  rockchip_generate_artifact_manifest "$manifest_tmp"
  chmod 0644 "$manifest_tmp"
  mv -- "$manifest_tmp" "$PACKAGE_MANIFEST"
  trap - EXIT
  "$SCRIPT_DIR/tools/compare-artifacts.sh" --verify "$PACKAGE_DIR"

  printf 'Build completed: %s\n' "$PACKAGE_DIR"
  printf 'Artifact manifest: %s\n' "$PACKAGE_MANIFEST"
)
