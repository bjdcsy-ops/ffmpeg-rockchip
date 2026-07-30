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

    printf 'git %s failed, retrying in %s seconds (attempt %s/%s)\n' \
      "$1" "$delay" "$attempt" "$attempts" >&2
    sleep "$delay"
    delay=$((delay * 2))
  done
}

git_branch_sha() {
  local repository=$1
  local branch=$2

  git_with_retry ls-remote --exit-code "$repository" "refs/heads/$branch" |
    awk '{ print $1 }'
}

git_clone_commit_with_retry() {
  local repository=$1
  local commit=$2
  local destination=$3
  local attempt
  local attempts
  local delay
  local status

  attempts=${GIT_RETRY_ATTEMPTS:-5}
  delay=${GIT_RETRY_INITIAL_DELAY_SECONDS:-10}
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    rm -rf -- "$destination"
    mkdir -p "$destination"
    git -C "$destination" init --quiet
    git -C "$destination" remote add origin "$repository"
    if git -c credential.helper= -c core.askPass= \
      -C "$destination" fetch --depth=1 origin "$commit" &&
      git -C "$destination" checkout --detach --quiet FETCH_HEAD; then
      return 0
    else
      status=$?
    fi

    if [ "$attempt" -eq "$attempts" ]; then
      return "$status"
    fi

    printf 'git fetch %s failed, retrying in %s seconds (attempt %s/%s)\n' \
      "$commit" "$delay" "$attempt" "$attempts" >&2
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
