#!/usr/bin/env python3
"""Fills an incident store with one incident per pipeline outcome.

The demo produces `detected → … → pr_opened` reliably; it does not produce a
refusal, a gate rejection, or a validated fix whose pull request failed. Those
arms are exactly where a dashboard is most likely to be wrong, so this seeds
them deliberately for screenshots and for eyeballing the UI.

Every row is written through the agent's own `Store`, never with hand-rolled
SQL, so a schema change breaks this script instead of silently producing rows
the real pipeline could never create. The database itself must already exist —
the Go monitor owns the schema:

    go build -o monitor/bin/monitor ./monitor/cmd/monitor
    ./monitor/bin/monitor --db demo.db --log-file /dev/null   # ctrl-c once created
    python scripts/seed_dashboard_db.py demo.db
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from diagnose.store import Store  # noqa: E402

B1_DIFF = """--- a/demo-app/app/main.py
+++ b/demo-app/app/main.py
@@ -28,7 +28,7 @@ PRODUCTS = [

 # Precomputed lookups so /checkout stops scanning PRODUCTS per item.
 KNOWN_IDS = {p["id"] for p in PRODUCTS}
-PRICE_CENTS_BY_ID = {str(p["id"]): p["price_cents"] for p in PRODUCTS}
+PRICE_CENTS_BY_ID = {p["id"]: p["price_cents"] for p in PRODUCTS}


 @app.middleware("http")
"""

TOOL_CALLS = [
    {"tool": "read_logs", "input": {"level": "error"}, "error": None, "output_chars": 4210},
    {"tool": "git_log_recent", "input": {"n": 5}, "error": None, "output_chars": 612},
    {
        "tool": "git_blame",
        "input": {"file": "demo-app/app/main.py", "start": 26, "end": 32},
        "error": None,
        "output_chars": 388,
    },
    {
        "tool": "read_source",
        "input": {"file": "demo-app/app/main.py", "start": 1, "end": 60},
        "error": None,
        "output_chars": 1904,
    },
]

MODEL_DIAGNOSIS = {
    "source": "replay-scripted",
    "turns": 5,
    "tool_calls": TOOL_CALLS,
    "reasoning": [
        "The error rate is confined to /checkout, so the regression is in that "
        "handler rather than app-wide.",
        "git blame points at the commit that precomputed the price table.",
    ],
    "root_cause": (
        'PRICE_CENTS_BY_ID is built with str(p["id"]) as its keys, while validation '
        "checks the incoming product_id against KNOWN_IDS, which holds the raw ints. "
        "Validation passes and the lookup then raises KeyError."
    ),
    "suspect_commit": "8f2e079",
}

PASSING_GATE = {
    "ok": True,
    "applied": True,
    "reason": "",
    "recounted": False,
    "tests_before": "failed",
    "tests_after": "passed",
    "files": ["demo-app/app/main.py"],
    "proves_a_fix": True,
}

EVIDENCE = {
    "error_rate": {
        "summary": "5 errors across 1 route(s)",
        "metrics": {"error_count": 5},
        "routes": {"/checkout": 5},
        "samples": [
            "2026-07-31T09:14:02Z error request.completed /checkout status=500",
            "2026-07-31T09:14:03Z error request.completed /checkout status=500",
        ],
    },
    "latency_p95": {
        "summary": "p95 243ms on GET /products",
        "metrics": {"p95_ms": 243.0, "threshold_ms": 100.0, "sample_count": 7},
        "routes": {"/products": 7},
        "samples": [],
    },
    "memory_growth": {
        "summary": "cache grew 0 -> 13 items at 0.67/s",
        "metrics": {"slope_per_sec": 0.67, "start_value": 0.0, "end_value": 13.0},
        "samples": None,
    },
}


def _insert(db_path: Path, kind: str, dedup_key: str) -> int:
    """Inserts a `detected` incident exactly as the Go monitor would."""
    with closing(sqlite3.connect(db_path)) as db, db:
        cursor = db.execute(
            """INSERT INTO incidents
                 (created_at, kind, dedup_key, status, window_start, window_end,
                  evidence, updated_at, occurrences)
               VALUES (?, ?, ?, 'detected', ?, ?, ?, ?, ?)""",
            (
                "2026-07-31T09:14:00Z",
                kind,
                dedup_key,
                "2026-07-31T09:14:00Z",
                "2026-07-31T09:14:05Z",
                json.dumps(EVIDENCE[kind]),
                "2026-07-31T09:14:05Z",
                2 if kind == "error_rate" else 1,
            ),
        )
        return cursor.lastrowid


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    db_path = Path(argv[1])
    if not db_path.exists():
        print(f"error: {db_path} does not exist — the monitor creates it", file=sys.stderr)
        return 2

    with Store(str(db_path)) as store:
        # 1. A fresh detection nobody has picked up yet.
        _insert(db_path, "memory_growth", "memory_growth")

        # 2. The full happy path, all the way to a pull request.
        shipped = _insert(db_path, "error_rate", "error_rate")
        store.claim(shipped)
        store.write_proposed_fix(shipped, MODEL_DIAGNOSIS, {
            "diff": B1_DIFF,
            "rationale": "Key the price table by the same type validation checks against.",
        })
        store.write_validated(shipped, PASSING_GATE)
        store.write_pr_opened(shipped, "https://github.com/1816x/Self-Healing-Agent/pull/6")

        # 3. Verified fix, pull request refused — NOT a failed fix. This is the
        #    distinction Phase 4 split apart, and the one a dashboard flattens
        #    if nobody looks at it.
        blocked = _insert(db_path, "error_rate", "error_rate:blocked")
        store.claim(blocked)
        store.write_proposed_fix(blocked, MODEL_DIAGNOSIS, {
            "diff": B1_DIFF, "rationale": "Same fix, different run.",
        })
        store.write_validated(blocked, {**PASSING_GATE, "pr_error": "GitHub returned 403"})

        # 4. The gate rejected the diff: it applied, the tests stayed red.
        rejected = _insert(db_path, "latency_p95", "latency_p95:/products")
        store.claim(rejected)
        store.write_proposed_fix(rejected, MODEL_DIAGNOSIS, {
            "diff": B1_DIFF, "rationale": "Cache the product lookup.",
        })
        store.write_validation_failed(rejected, {
            "ok": False, "applied": True, "reason": "", "recounted": False,
            "tests_before": "failed", "tests_after": "failed",
            "files": ["demo-app/app/main.py"], "proves_a_fix": False,
        })

        # 5. A refusal — recorded as its own outcome, not as an error.
        refused = _insert(db_path, "error_rate", "error_rate:refused")
        store.claim(refused)
        store.mark_refused(refused, {"category": "cyber", "explanation": None})

        # 6. A run that broke: an error, with no diagnosis behind it.
        failed = _insert(db_path, "latency_p95", "latency_p95:failed")
        store.claim(failed)
        store.mark_failed(failed, "iteration cap reached after 12 turns")

        # 7. Parked at fix_proposed by --no-validate: what --resume picks up.
        parked = _insert(db_path, "memory_growth", "memory_growth:parked")
        store.claim(parked)
        store.write_proposed_fix(parked, MODEL_DIAGNOSIS, {
            "diff": B1_DIFF, "rationale": "Bound the cache.",
        })

    print(f"seeded {db_path} with one incident per pipeline outcome")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
