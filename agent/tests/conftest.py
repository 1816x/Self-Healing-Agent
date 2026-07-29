"""Shared fixtures.

The incident-store fixtures build databases with the *real* Go schema by
shelling out to the monitor binary. That makes the cross-language schema
contract a tested property rather than an assumption: if the Go migration
ladder and the Python agent's expectations drift apart, these tests fail
instead of production.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_DIR = REPO_ROOT / "monitor"


@pytest.fixture(scope="session")
def monitor_binary(tmp_path_factory) -> Path:
    """Builds the monitor once per test session.

    Skips (rather than fails) when Go isn't installed, so the Python suite
    stays runnable on its own — but the skip is visible in pytest output,
    not silent.
    """
    if shutil.which("go") is None:
        pytest.skip("go toolchain not available; skipping real-schema tests")
    binary = tmp_path_factory.mktemp("bin") / "monitor"
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/monitor"],
        cwd=MONITOR_DIR,
        check=True,
        capture_output=True,
    )
    return binary


@pytest.fixture
def real_schema_db(monitor_binary: Path, tmp_path: Path) -> Path:
    """An empty incidents.db created by the real monitor.

    Runs the monitor against a log file that doesn't exist and a metrics
    URL nothing is listening on, with a short poll interval — it creates
    and migrates the database, finds nothing to do, and we stop it. That
    exercises the actual migration path rather than a hand-copied CREATE
    TABLE.
    """
    db_path = tmp_path / "incidents.db"
    process = subprocess.Popen(
        [
            str(monitor_binary),
            "--db", str(db_path),
            "--log-file", str(tmp_path / "nonexistent.jsonl"),
            "--scrape-url", "http://127.0.0.1:1/metrics",
            "--poll", "10ms",
            "--scrape-interval", "10ms",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_schema(db_path)
    finally:
        process.terminate()
        process.wait(timeout=10)
    return db_path


def _wait_for_schema(db_path: Path, attempts: int = 200) -> None:
    """Polls until the monitor has created and migrated the database.

    Every connection is closed explicitly. ``with sqlite3.connect(...)``
    commits the transaction but does *not* close the connection — leaking
    one handle per poll onto a database the monitor is concurrently writing
    was enough to cause lock contention and make this wedge.
    """
    import time

    for _ in range(attempts):
        if db_path.exists():
            try:
                with closing(sqlite3.connect(db_path)) as db:
                    if db.execute("PRAGMA user_version").fetchone()[0] >= 3:
                        return
            except sqlite3.DatabaseError:
                pass  # mid-write; try again
        time.sleep(0.05)
    raise AssertionError(f"monitor did not initialize {db_path} in time")


def insert_incident(
    db_path: Path,
    *,
    kind: str = "error_rate",
    dedup_key: str = "error_rate",
    status: str = "detected",
    evidence: dict | None = None,
) -> int:
    """Inserts an incident the way the monitor does, returning its id."""
    evidence = evidence or {
        "summary": "5 errors across 1 route(s)",
        "metrics": {"error_count": 5},
        "routes": {"/checkout": 5},
        "samples": ["2026-07-29T12:00:00Z error request.completed /checkout status=500"],
    }
    with closing(sqlite3.connect(db_path)) as db, db:
        cursor = db.execute(
            """INSERT INTO incidents
                 (created_at, kind, dedup_key, status, window_start, window_end,
                  evidence, updated_at, occurrences)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-07-29T12:00:00Z",
                kind,
                dedup_key,
                status,
                "2026-07-29T12:00:00Z",
                "2026-07-29T12:00:03Z",
                json.dumps(evidence),
                "2026-07-29T12:00:03Z",
                1,
            ),
        )
        return cursor.lastrowid
