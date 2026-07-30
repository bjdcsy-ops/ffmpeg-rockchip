#!/usr/bin/env bash

# Verifies one artifact against its sibling mtree manifest, or compares two.

set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage:
  $0 --verify <artifact-directory>
  $0 <left-artifact-directory> <right-artifact-directory>
EOF
}

resolve_mtree() {
  if command -v mtree >/dev/null 2>&1; then
    command -v mtree
  elif [ -x /usr/sbin/mtree ]; then
    printf '/usr/sbin/mtree\n'
  else
    printf 'mtree is required to verify artifact manifests.\n' >&2
    return 1
  fi
}

resolve_artifact_dir() {
  local requested_dir=$1

  if [ ! -d "$requested_dir" ]; then
    printf 'Artifact directory not found: %s\n' "$requested_dir" >&2
    return 1
  fi

  (
    cd -- "$requested_dir"
    pwd -P
  )
}

verify_artifact() {
  local artifact_dir=$1
  local manifest="$artifact_dir.mtree"
  local mtree_bin=$2
  local mtree_output
  local mtree_status

  if [ ! -f "$manifest" ]; then
    printf 'Artifact manifest not found: %s\n' "$manifest" >&2
    return 1
  fi

  if mtree_output=$(
    "$mtree_bin" -P -p "$artifact_dir" -f "$manifest" 2>&1
  ); then
    mtree_status=0
  else
    mtree_status=$?
  fi

  if [ "$mtree_status" -ne 0 ] || [ -n "$mtree_output" ]; then
    printf 'Artifact does not match manifest: %s\n' "$manifest" >&2
    if [ -n "$mtree_output" ]; then
      printf '%s\n' "$mtree_output" >&2
    fi
    return 1
  fi

  printf 'Verified artifact manifest: %s\n' "$manifest"
}

mtree_bin=$(resolve_mtree)

if [ "${1:-}" = --verify ]; then
  if [ "$#" -ne 2 ]; then
    usage
    exit 2
  fi

  artifact_dir=$(resolve_artifact_dir "$2")
  verify_artifact "$artifact_dir" "$mtree_bin"
  exit 0
fi

if [ "$#" -ne 2 ]; then
  usage
  exit 2
fi

left_artifact=$(resolve_artifact_dir "$1")
right_artifact=$(resolve_artifact_dir "$2")
left_manifest="$left_artifact.mtree"
right_manifest="$right_artifact.mtree"

verify_artifact "$left_artifact" "$mtree_bin"
verify_artifact "$right_artifact" "$mtree_bin"

if ! cmp -s "$left_manifest" "$right_manifest"; then
  printf 'Artifact manifests differ:\n' >&2
  diff -u "$left_manifest" "$right_manifest" >&2 || true
  exit 1
fi

printf 'Artifacts are identical: %s == %s\n' \
  "$left_artifact" "$right_artifact"
