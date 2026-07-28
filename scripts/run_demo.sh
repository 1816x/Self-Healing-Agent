#!/usr/bin/env bash
# Phase 1 demo loop: demo app + monitor + steady request load, all local.
#
#   terminal 1:  scripts/run_demo.sh
#   terminal 2:  scripts/inject_bug.sh b1
#
# Within seconds of the injection, checkouts start failing, the monitor's
# error-rate detector crosses its threshold, and an incident row lands in
# incidents.db:
#
#   sqlite3 incidents.db 'SELECT id, kind, status, evidence FROM incidents;'
set -euo pipefail

cd "$(dirname "$0")/.."

VENV="demo-app/.venv"
if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "setting up demo-app virtualenv..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -e "./demo-app[dev]"
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
  --window 60s --threshold 5 &
MONITOR_PID=$!
trap 'kill $APP_PID $MONITOR_PID 2>/dev/null || true' EXIT

sleep 2
echo
echo "demo running — app :8000, incidents -> incidents.db. Ctrl-C stops everything."
echo "inject a failure with: scripts/inject_bug.sh b1"
echo

while true; do
  curl -s -o /dev/null http://127.0.0.1:8000/products || true
  curl -s -o /dev/null -X POST http://127.0.0.1:8000/checkout \
    -H 'Content-Type: application/json' -d '{"product_ids":[1,2]}' || true
  sleep 0.5
done
