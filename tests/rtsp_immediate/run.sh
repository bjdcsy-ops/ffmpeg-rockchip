#!/usr/bin/env bash
set -euo pipefail
src=${1:-/workspace}
target=${2:-rk3588}
bld=$src/.build/rockchip/$target/ffmpeg
deps_key=$(sed -n 's/^dependency_cache_key=//p' "$bld/.rockchip-config-stamp")
test -n "$deps_key"
deps=$src/.rockchip-cache/deps/$target/$deps_key
test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
test_tmp=$(mktemp -d)
trap 'rm -rf -- "$test_tmp"' EXIT
cd "$test_dir"
gcc -O2 -Werror=implicit-function-declaration -DHAVE_AV_CONFIG_H -I"$bld" -I"$src" -I"$src/libavformat" -I"$deps/include" -I/usr/include/libdrm \
    -o "$test_tmp/rtsp_driver" rtsp_driver.c "$bld/libavformat/libavformat.a" \
    "$bld/libavcodec/libavcodec.a" "$bld/libswresample/libswresample.a" "$bld/libavutil/libavutil.a" \
    -L"$deps/lib" -lrga -lrockchip_mpp -ldrm -lm -latomic -pthread
export LD_LIBRARY_PATH=$deps/lib
export RTSP_TEST_DRIVER=$test_tmp/rtsp_driver
python3 test_rtsp_integration.py
