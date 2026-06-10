#!/usr/bin/env bash

git_with_retry() {
  local attempt
  local attempts
  local delay
  local status

  attempts=${GIT_RETRY_ATTEMPTS:-5}
  delay=${GIT_RETRY_INITIAL_DELAY_SECONDS:-10}
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if git -c credential.helper= -c core.askPass= "$@"; then
      return 0
    else
      status=$?
    fi

    if [ "$attempt" -eq "$attempts" ]; then
      return "$status"
    fi

    printf 'git %s failed, retrying in %s seconds (attempt %s/%s)\n' "$1" "$delay" "$attempt" "$attempts" >&2
    sleep "$delay"
    delay=$((delay * 2))
  done
}

git_branch_sha() {
  local repository=$1
  local branch=$2

  git_with_retry ls-remote --exit-code "$repository" "refs/heads/$branch" | awk '{ print $1 }'
}

git_clone_branch_with_retry() {
  local repository=$1
  local branch=$2
  local destination=$3
  local attempt
  local attempts
  local delay
  local status

  attempts=${GIT_RETRY_ATTEMPTS:-5}
  delay=${GIT_RETRY_INITIAL_DELAY_SECONDS:-10}
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    rm -rf "$destination"
    if git -c credential.helper= -c core.askPass= clone --depth=1 --branch "$branch" "$repository" "$destination"; then
      return 0
    else
      status=$?
    fi

    if [ "$attempt" -eq "$attempts" ]; then
      return "$status"
    fi

    printf 'git clone failed, retrying in %s seconds (attempt %s/%s)\n' "$delay" "$attempt" "$attempts" >&2
    sleep "$delay"
    delay=$((delay * 2))
  done
}

git_verify_head() {
  local repository_dir=$1
  local expected_sha=$2
  local actual_sha

  actual_sha=$(git -C "$repository_dir" rev-parse HEAD)
  test "$actual_sha" = "$expected_sha"
}
