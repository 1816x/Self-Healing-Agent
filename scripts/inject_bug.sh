#!/usr/bin/env bash
# Injects a catalogued bug (see PLAN.md) into the demo app as a REAL local
# commit, so the monitor has something genuine to detect and `git blame`
# has a genuine suspect for the diagnosis agent to find.
#
# The commit message is deliberately innocent-looking — real regressions
# don't announce themselves in their commit message.
#
# The injected commit NEVER goes to main. By default it stays local and you
# undo it with `git reset --hard HEAD~1`.
#
# `--push` publishes it to a throwaway `demo/<bug>-<sha>` branch. That exists
# for one reason: Phase 4's fix pull request needs a base branch that actually
# contains the bug. A PR against main would revert code main has never had —
# it could not apply, and CI could not judge it. Publishing the bug to its own
# branch is what makes the fix PR a real, mergeable, CI-verifiable change while
# keeping main clean.
set -euo pipefail

cd "$(dirname "$0")/.."

PUSH=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    *) ARGS+=("$arg") ;;
  esac
done
set -- ${ARGS[@]+"${ARGS[@]}"}

BUG="${1:-}"
case "$BUG" in
  b1)
    PATCH="scripts/bugs/b1.patch"
    MESSAGE="perf(checkout): precompute price lookup table"
    ;;
  b2)
    PATCH="scripts/bugs/b2.patch"
    MESSAGE="feat(products): enrich listing with live price lookups"
    ;;
  b3)
    PATCH="scripts/bugs/b3.patch"
    MESSAGE="feat(checkout): cache results for idempotent retries"
    ;;
  *)
    echo "usage: $0 <bug-id> [--push]" >&2
    echo "  b1   checkout regression: price lookup raises KeyError -> 500s on every checkout" >&2
    echo "  b2   products latency regression: per-item simulated round trip -> high p95 on GET /products" >&2
    echo "  b3   checkout memory leak: idempotency cache never evicted -> unbounded cache growth" >&2
    echo "" >&2
    echo "  --push  also publish the bug commit to demo/<bug>-<sha> so a fix PR has a base" >&2
    exit 1
    ;;
esac

if ! git diff --quiet -- demo-app; then
  echo "error: demo-app has uncommitted changes; refusing to inject on top of them" >&2
  exit 1
fi

# Checked before anything is committed, so a refusal leaves the tree as it
# was found rather than stranding a bug commit that cannot be published.
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$PUSH" -eq 1 ] && { [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; }; then
  echo "error: refusing to publish a bug commit from $BRANCH" >&2
  echo "       switch to a working branch first; main must never carry an injected bug" >&2
  exit 1
fi

git apply "$PATCH"
git add demo-app
git commit -q -m "$MESSAGE"

SHA="$(git rev-parse --short HEAD)"
echo "injected $BUG as local commit $SHA: \"$MESSAGE\""

if [ "$PUSH" -eq 0 ]; then
  echo "undo with: git reset --hard HEAD~1"
  exit 0
fi

# The branch name carries the bug id and the sha, so it is obvious what it
# is and which run produced it.
DEMO_BRANCH="demo/$BUG-$SHA"
git push -q -u origin "HEAD:refs/heads/$DEMO_BRANCH"

echo "published to $DEMO_BRANCH — use it as the base for the fix PR:"
echo "  python -m diagnose --db incidents.db --offline --open-pr --base-branch $DEMO_BRANCH"
echo ""
echo "clean up when done:"
echo "  git reset --hard HEAD~1"
echo "  git push origin --delete $DEMO_BRANCH"
