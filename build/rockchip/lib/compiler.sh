#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -lt 1 ]; then
  printf 'Usage: %s <gcc|g++> [compiler arguments...]\n' "$0" >&2
  exit 2
fi

compiler=$1
shift

random_seed=ffmpeg-rockchip
expect_output=false
for argument in "$@"; do
  if [ "$expect_output" = true ]; then
    random_seed=$argument
    expect_output=false
    continue
  fi

  case "$argument" in
    -o)
      expect_output=true
      ;;
    -o?*)
      random_seed=${argument#-o}
      ;;
  esac
done

exec ccache "$compiler" "-frandom-seed=$random_seed" "$@"
