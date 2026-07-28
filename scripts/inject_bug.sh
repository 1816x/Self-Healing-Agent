#!/usr/bin/env bash
# Injects a catalogued bug (see PLAN.md) into the demo app as a REAL local
# commit, so the monitor has something genuine to detect and `git blame`
# has a genuine suspect for the diagnosis agent to find.
#
# The commit message is deliberately innocent-looking — real regressions
# don't announce themselves in their commit message.
#
# The injected commit is for the local demo loop ONLY. Never push it.
# Undo with:  git reset --hard HEAD~1
set -euo pipefail

cd "$(dirname "$0")/.."

BUG="${1:-}"
case "$BUG" in
  b1)
    PATCH="scripts/bugs/b1.patch"
    MESSAGE="perf(checkout): precompute price lookup table"
    ;;
  *)
    echo "usage: $0 <bug-id>" >&2
    echo "  b1   checkout regression: price lookup raises KeyError -> 500s on every checkout" >&2
    exit 1
    ;;
esac

if ! git diff --quiet -- demo-app; then
  echo "error: demo-app has uncommitted changes; refusing to inject on top of them" >&2
  exit 1
fi

git apply "$PATCH"
git add demo-app
git commit -q -m "$MESSAGE"

echo "injected $BUG as local commit $(git rev-parse --short HEAD): \"$MESSAGE\""
echo "do NOT push this commit — undo with: git reset --hard HEAD~1"
