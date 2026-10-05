#!/usr/bin/env bash
# Create (or refresh) one git worktree per line of work, each on its own branch off main.
#   tools/worktrees.sh create backend display
#   tools/worktrees.sh list | remove <name>
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
WT="${EPITAPH_WORKTREES:-$(dirname "$ROOT")/epitaph-wt}"
case "${1:-list}" in
  create)
    shift
    for name in "$@"; do
      branch="ws/$name"
      if [ -d "$WT/$name" ]; then
        git -C "$WT/$name" fetch -q "$ROOT" main 2>/dev/null || true
        echo "exists: $WT/$name ($branch)"; continue
      fi
      git -C "$ROOT" branch -q "$branch" main 2>/dev/null || true
      git -C "$ROOT" worktree add -q "$WT/$name" "$branch"
      ln -sfn "$ROOT/.venv" "$WT/$name/.venv"
      echo "created: $WT/$name ($branch)"
    done ;;
  list) git -C "$ROOT" worktree list ;;
  remove) git -C "$ROOT" worktree remove --force "$WT/${2:?name}" ;;
  *) echo "usage: worktrees.sh create <names...> | list | remove <name>" >&2; exit 2 ;;
esac
