#!/usr/bin/env bash

# Stage, commit, and push using defaults derived from the current directory.
git_push_from_dir() {
  local dir_slug branch message
  dir_slug="$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')"
  branch="${1:-main}"
  message="${2:-chore(${dir_slug}): update}"

  git add -A
  git commit -m "$message" || true
  git push -u origin "$branch"
}
