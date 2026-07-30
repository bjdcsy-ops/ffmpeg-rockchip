#!/usr/bin/env bash

# Builds the locked MPP and RGA revisions into DEPS_PREFIX.
# This file is hashed into the dependency cache key.

build_rockchip_dependencies() {
  local -a mpp_cmake_args
  local -a rga_meson_args
  local deps_build_root="$BUILD_ROOT/dependencies"
  local locked_mpp_sha=$1
  local locked_rga_sha=$2

  rm -rf -- "$deps_build_root" "$DEPS_PREFIX"
  mkdir -p "$deps_build_root" "$DEPS_PREFIX"

  git_clone_commit_with_retry \
    "$MPP_REPOSITORY" "$locked_mpp_sha" "$deps_build_root/rkmpp"
  git_verify_head "$deps_build_root/rkmpp" "$locked_mpp_sha"

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

  git_clone_commit_with_retry \
    "$RGA_REPOSITORY" "$locked_rga_sha" "$deps_build_root/rkrga"
  git_verify_head "$deps_build_root/rkrga" "$locked_rga_sha"

  rga_meson_args=(
    --prefix="$DEPS_PREFIX"
    --libdir=lib
    --buildtype=release
    --default-library=shared
    -Dlibdrm=false
    -Dlibrga_demo=false
  )

  (
    export CC="ccache gcc"
    export CXX="ccache g++"
    export CFLAGS="$TARGET_CFLAGS"
    export CXXFLAGS="$TARGET_CFLAGS -fpermissive"
    export LDFLAGS="$TARGET_LDFLAGS"

    meson setup \
      "$deps_build_root/rkrga" \
      "$deps_build_root/rkrga-build" \
      "${rga_meson_args[@]}"
    meson compile -C "$deps_build_root/rkrga-build"
    meson install -C "$deps_build_root/rkrga-build"
  )
}
