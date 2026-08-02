#!/usr/bin/env bash
# Demo loop: demo app + monitor + steady request load + the diagnosis agent.
#
#   terminal 1:  scripts/run_demo.sh
#   terminal 2:  scripts/inject_bug.sh b1   # or b2, or b3 — one at a time
#
# Within seconds of the injection, the corresponding detector fires and
# an incident row lands in incidents.db:
#
#   b1  checkout 500s          -> error_rate    (log-based)
#   b2  /products goes slow    -> latency_p95   (metric-based)
#   b3  cache grows unbounded  -> memory_growth (metric-based)
#
# The agent then picks that incident up automatically and diagnoses it
# offline — no API key, no second command. It drives its real read-only
# tools (read_logs -> git_log_recent -> git_blame -> read_source) against
# this repository and records a root cause plus a proposed diff.
#
# Offline diagnosis says what it is. With no recorded model session it
# replays a hand-authored transcript or falls back to rule-based triage,
# and labels both — see agent/diagnose/transcripts/README.md.
#
#   sqlite3 incidents.db 'SELECT id, kind, status, occurrences, evidence FROM incidents;'
#
# Flags:
#   --no-agent   detection only, the Phase 1/2 demo
#   --live       diagnose with a real model call (needs ANTHROPIC_API_KEY)
#
# Undo an injection before trying another: git reset --hard HEAD~1
set -euo pipefail

cd "$(dirname "$0")/.."

RUN_AGENT=1
AGENT_MODE="--offline"
for arg in "$@"; do
  case "$arg" in
    --no-agent) RUN_AGENT=0 ;;
    --live) AGENT_MODE="" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

VENV="demo-app/.venv"
if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "setting up demo-app virtualenv..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -e "./demo-app[dev]"
fi

# The agent lives in the same virtualenv. Installing it here is what makes
# the whole loop one command: Phase 5's gate is that a stranger clones the
# repo, runs this, and watches detection *and* diagnosis happen.
if [ "$RUN_AGENT" -eq 1 ] && [ ! -f "$VENV/lib/agent-installed" ]; then
  echo "installing the diagnosis agent..."
  "$VENV/bin/pip" install -q -e "./agent"
  touch "$VENV/lib/agent-installed"
fi

echo "building monitor..."
(cd monitor && go build -o bin/monitor ./cmd/monitor)

# --reload matters: bug injection edits the source, and uvicorn hot-reloads
# it into the running app — no restart choreography needed for the demo.
"$VENV/bin/uvicorn" --app-dir demo-app app.main:app --port 8000 --reload &
APP_PID=$!
./monitor/bin/monitor \
  --log-file demo-app/logs/demo-app.jsonl \
  --db incidents.db \
  --window 60s --threshold 5 \
  --scrape-url http://127.0.0.1:8000/metrics --scrape-interval 2s \
  --latency-threshold 100ms --latency-min-breaches 2 --latency-min-samples 3 \
  --mem-window 20s --mem-rate 0.5 --mem-min-samples 5 &
MONITOR_PID=$!

AGENT_PID=""
if [ "$RUN_AGENT" -eq 1 ]; then
  # Poll for detected incidents and diagnose them one at a time. The agent's
  # claim is a conditional UPDATE, so a second copy of this loop could not
  # double-diagnose an incident even if one were running.
  #
  # --no-validate on purpose: the gate cuts a git worktree and runs pytest in
  # it, which is the right thing for a real run and the wrong thing to do
  # unattended in a loop every few seconds. Incidents therefore rest at
  # `fix_proposed`, which is exactly what `--resume` was built to pick up —
  # the command printed below drives one through the gate for real.
  (
    while true; do
      sleep 3
      # shellcheck disable=SC2086  # AGENT_MODE is empty for --live, deliberately
      "$VENV/bin/python" -m diagnose \
        --db incidents.db --repo-root . $AGENT_MODE --no-validate 2>&1 |
        grep -v '^nothing to do' || true
    done
  ) &
  AGENT_PID=$!
fi

cleanup() {
  # The agent poller is a subshell around a python process. Killing the
  # subshell alone orphans that child, which then keeps a handle on
  # incidents.db after Ctrl-C — so its children go first, by name of parent.
  if [ -n "$AGENT_PID" ]; then
    pkill -P "$AGENT_PID" 2>/dev/null || true
    kill "$AGENT_PID" 2>/dev/null || true
  fi
  kill "$APP_PID" "$MONITOR_PID" 2>/dev/null || true
}
trap cleanup EXIT

sleep 2
echo
echo "demo running — app :8000, incidents -> incidents.db. Ctrl-C stops everything."
echo "inject a failure with: scripts/inject_bug.sh b1   (or b2, or b3)"
if [ "$RUN_AGENT" -eq 1 ]; then
  echo "the agent will pick up each incident and diagnose it automatically."
  echo "then validate one of its fixes for real (applies the diff, runs the tests):"
  echo "  $VENV/bin/python -m diagnose --db incidents.db --repo-root . --offline --resume"
else
  echo "detection only (--no-agent). Diagnose by hand with:"
  echo "  python -m diagnose --db incidents.db --offline --repo-root ."
fi
echo "watch it in a browser:  cd dashboard && npm install && npm run dev"
echo

while true; do
  curl -s -o /dev/null http://127.0.0.1:8000/products || true
  curl -s -o /dev/null -X POST http://127.0.0.1:8000/checkout \
    -H 'Content-Type: application/json' -d '{"product_ids":[1,2]}' || true
  sleep 0.5
done
