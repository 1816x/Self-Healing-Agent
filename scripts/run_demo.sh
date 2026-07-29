#!/usr/bin/env bash
# Demo loop: demo app + monitor + steady request load, all local.
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
#   sqlite3 incidents.db 'SELECT id, kind, status, occurrences, evidence FROM incidents;'
#
# Undo an injection before trying another: git reset --hard HEAD~1
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
  --window 60s --threshold 5 \
  --scrape-url http://127.0.0.1:8000/metrics --scrape-interval 2s \
  --latency-threshold 100ms --latency-min-breaches 2 --latency-min-samples 3 \
  --mem-window 20s --mem-rate 0.5 --mem-min-samples 5 &
MONITOR_PID=$!
trap 'kill $APP_PID $MONITOR_PID 2>/dev/null || true' EXIT

sleep 2
echo
echo "demo running — app :8000, incidents -> incidents.db. Ctrl-C stops everything."
echo "inject a failure with: scripts/inject_bug.sh b1   (or b2, or b3)"
echo

while true; do
  curl -s -o /dev/null http://127.0.0.1:8000/products || true
  curl -s -o /dev/null -X POST http://127.0.0.1:8000/checkout \
    -H 'Content-Type: application/json' -d '{"product_ids":[1,2]}' || true
  sleep 0.5
done
