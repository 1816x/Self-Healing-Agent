#!/usr/bin/env bash
# The sequence shown in the README's terminal cast, run for real.
#
# Assumes scripts/run_demo.sh is already running in another terminal — this
# is the "terminal 2" half: inject a bug, watch the incident appear, watch
# the agent diagnose it, then drive the fix through the validation gate.
#
#   python scripts/record_cast.py docs/assets/demo.cast -- scripts/demo_script.sh
#
# Nothing here is faked. The pauses are waits for real work to finish.
set -uo pipefail

cd "$(dirname "$0")/.."

say() { printf '\n\033[1;36m$ %s\033[0m\n' "$*"; }
note() { printf '\033[0;90m# %s\033[0m\n' "$*"; }

query() {
  python3 - "$@" <<'PY'
import sqlite3, sys, json
db = sqlite3.connect("incidents.db")
db.row_factory = sqlite3.Row
rows = list(db.execute("SELECT id, kind, status, occurrences, evidence, diagnosis FROM incidents"))
if not rows:
    print("(no incidents yet)")
for r in rows:
    ev = json.loads(r["evidence"] or "{}")
    line = f"#{r['id']}  {r['kind']:<14} {r['status']:<14} {ev.get('summary','')}"
    print(line)
    if r["diagnosis"]:
        d = json.loads(r["diagnosis"])
        if d.get("tool_calls"):
            print(f"      tools: {' -> '.join(t['tool'] for t in d['tool_calls'])}")
        if d.get("root_cause"):
            print(f"      cause: {d['root_cause'][:96]}...")
PY
}

note "the demo app and monitor are already running (scripts/run_demo.sh)"
note "no incidents yet — the app is healthy"
say "sqlite3 incidents.db 'select id, kind, status from incidents'"
query
sleep 2

say "scripts/inject_bug.sh b1"
note "commits a real bug with an innocent-looking message"
scripts/inject_bug.sh b1
# The base a fix PR targets has to contain the bug, or the diff reverting it
# could not apply. `inject_bug.sh --push` publishes exactly this branch; here
# it stays local so the cast can be recorded without pushing anything.
git branch -f demo/b1 HEAD >/dev/null
sleep 6

say "sqlite3 incidents.db 'select id, kind, status from incidents'"
note "the monitor's error-rate detector fired, and the agent picked it up"
query
sleep 4

say "sqlite3 incidents.db  # again, a few seconds later"
query
sleep 3

note "the agent proposed a diff. now validate it for real:"
say "python -m diagnose --offline --resume --base-branch demo/b1"
demo-app/.venv/bin/python -m diagnose \
  --db incidents.db --repo-root . --offline --resume --base-branch demo/b1 2>&1 | tail -6
sleep 3

say "sqlite3 incidents.db 'select id, status from incidents'"
note "red before the diff, green after it — that is what fix_validated means"
query
sleep 3
