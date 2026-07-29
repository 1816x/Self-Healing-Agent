"""Read/write access to the shared incident store.

The Go monitor owns every schema change (it has the ``PRAGMA
user_version`` migration ladder). This module deliberately carries no
DDL: it asserts the version it needs and tells the operator to run the
monitor if the database is older. One language owning all schema beats
two languages agreeing on it — see ``docs/design-decisions.md``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

# Must match monitor/internal/store/store.go's schemaVersion. The agent
# reads and writes columns v3 introduced, so anything older can't serve it.
REQUIRED_SCHEMA_VERSION = 3

STATUS_DETECTED = "detected"
STATUS_DIAGNOSING = "diagnosing"
STATUS_DIAGNOSED = "diagnosed"
STATUS_FIX_PROPOSED = "fix_proposed"
STATUS_DIAGNOSIS_FAILED = "diagnosis_failed"
STATUS_DIAGNOSIS_REFUSED = "diagnosis_refused"


class SchemaTooOldError(RuntimeError):
    """The database predates the columns this agent needs."""


@dataclass(frozen=True)
class Incident:
    """One incident row, as the agent needs to see it."""

    id: int
    kind: str
    dedup_key: str
    status: str
    window_start: str
    window_end: str
    occurrences: int
    evidence: dict

    def summary_line(self) -> str:
        """One-line description for the agent's opening prompt."""
        summary = self.evidence.get("summary") or self.kind
        return f"incident #{self.id} ({self.kind}): {summary}"


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str):
        # isolation_level=None puts the connection in autocommit mode so an
        # explicit BEGIN IMMEDIATE in claim() means what it says instead of
        # racing sqlite3's implicit transaction handling.
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._assert_schema_version()

    def _assert_schema_version(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version < REQUIRED_SCHEMA_VERSION:
            raise SchemaTooOldError(
                f"incident store is at schema v{version}, agent needs "
                f"v{REQUIRED_SCHEMA_VERSION}. The monitor owns migrations — "
                f"run it once against this database to migrate it forward."
            )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- reading ---

    def get(self, incident_id: int) -> Incident | None:
        row = self._db.execute(
            """SELECT id, kind, dedup_key, status, window_start, window_end,
                      occurrences, evidence
               FROM incidents WHERE id = ?""",
            (incident_id,),
        ).fetchone()
        return _row_to_incident(row) if row else None

    def claim_next_detected(self) -> Incident | None:
        """Claims the oldest `detected` incident, or returns None.

        The claim is a conditional UPDATE guarded by the current status, so
        two agent processes racing on the same store can't both pick up the
        same incident: exactly one UPDATE reports a changed row.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                """SELECT id FROM incidents WHERE status = ?
                   ORDER BY id LIMIT 1""",
                (STATUS_DETECTED,),
            ).fetchone()
            if row is None:
                self._db.execute("COMMIT")
                return None

            incident_id = row["id"]
            cursor = self._db.execute(
                """UPDATE incidents SET status = ?, updated_at = ?
                   WHERE id = ? AND status = ?""",
                (STATUS_DIAGNOSING, _utcnow(), incident_id, STATUS_DETECTED),
            )
            if cursor.rowcount != 1:
                # Another process claimed it between the SELECT and UPDATE.
                self._db.execute("COMMIT")
                return None
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

        return self.get(incident_id)

    def claim(self, incident_id: int) -> Incident | None:
        """Claims one specific incident by id. Same guard as claim_next_detected."""
        cursor = self._db.execute(
            """UPDATE incidents SET status = ?, updated_at = ?
               WHERE id = ? AND status = ?""",
            (STATUS_DIAGNOSING, _utcnow(), incident_id, STATUS_DETECTED),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(incident_id)

    def count_by_status(self, status: str) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM incidents WHERE status = ?", (status,)
        ).fetchone()[0]

    # --- writing ---

    def write_diagnosis(self, incident_id: int, diagnosis: dict) -> None:
        """Records a completed diagnosis and moves the incident to `diagnosed`.

        `diagnosis` carries a `source` field ("live" / "replay" /
        "heuristic") so a consumer can never mistake a canned offline
        answer for a real model run.
        """
        self._set(
            incident_id,
            status=STATUS_DIAGNOSED,
            diagnosis=json.dumps(diagnosis),
            diagnosed_at=_utcnow(),
        )

    def write_proposed_fix(self, incident_id: int, diagnosis: dict, proposed_fix: dict) -> None:
        """Records a diagnosis plus a proposed diff, without applying it.

        Phase 3 only records. Phase 4 adds the validation gate (does the
        diff apply? do the demo app's tests pass?) and opens the PR.
        """
        self._set(
            incident_id,
            status=STATUS_FIX_PROPOSED,
            diagnosis=json.dumps(diagnosis),
            diagnosed_at=_utcnow(),
            proposed_fix=json.dumps(proposed_fix),
        )

    def mark_failed(self, incident_id: int, reason: str) -> None:
        self._set(
            incident_id,
            status=STATUS_DIAGNOSIS_FAILED,
            diagnosis=json.dumps({"error": reason}),
            diagnosed_at=_utcnow(),
        )

    def mark_refused(self, incident_id: int, detail: dict) -> None:
        """The model declined the request (stop_reason == "refusal").

        Distinct from mark_failed: nothing is broken, and retrying the same
        prompt won't help — so this is recorded as its own outcome rather
        than as an error.
        """
        self._set(
            incident_id,
            status=STATUS_DIAGNOSIS_REFUSED,
            diagnosis=json.dumps({"refusal": detail}),
            diagnosed_at=_utcnow(),
        )

    def _set(self, incident_id: int, *, status: str, **columns) -> None:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        values = list(columns.values())
        self._db.execute(
            f"UPDATE incidents SET status = ?, updated_at = ?, {assignments} WHERE id = ?",
            [status, _utcnow(), *values, incident_id],
        )


def _row_to_incident(row: sqlite3.Row) -> Incident:
    try:
        evidence = json.loads(row["evidence"])
    except (json.JSONDecodeError, TypeError):
        # A corrupt evidence blob shouldn't make the incident undiagnosable —
        # the agent has tools to go read the logs itself.
        evidence = {}
    return Incident(
        id=row["id"],
        kind=row["kind"],
        dedup_key=row["dedup_key"],
        status=row["status"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        occurrences=row["occurrences"],
        evidence=evidence,
    )
