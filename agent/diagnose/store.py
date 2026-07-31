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
# reads and writes columns v4 introduced, so anything older can't serve it.
REQUIRED_SCHEMA_VERSION = 4

STATUS_DETECTED = "detected"
STATUS_DIAGNOSING = "diagnosing"
STATUS_DIAGNOSED = "diagnosed"
STATUS_FIX_PROPOSED = "fix_proposed"
STATUS_DIAGNOSIS_FAILED = "diagnosis_failed"
STATUS_DIAGNOSIS_REFUSED = "diagnosis_refused"
# Phase 4. fix_validated and fix_failed are the two outcomes of the local
# validation gate; pr_opened is the separate, opt-in outward-facing step.
STATUS_FIX_VALIDATED = "fix_validated"
STATUS_FIX_FAILED = "fix_failed"
STATUS_PR_OPENED = "pr_opened"


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

        Recording is still separate from validating: this only says the
        model produced a diff. Whether that diff applies and passes tests
        is `write_validated` / `write_validation_failed`, which run against
        the repository rather than trusting the proposal.
        """
        self._set(
            incident_id,
            status=STATUS_FIX_PROPOSED,
            diagnosis=json.dumps(diagnosis),
            diagnosed_at=_utcnow(),
            proposed_fix=json.dumps(proposed_fix),
        )

    def write_validated(self, incident_id: int, validation: dict) -> None:
        """The gate applied the diff and the tests passed with it.

        `validation` is stored in its own column rather than merged into
        `diagnosis` on purpose: one is the model's claim, the other is what
        this machine independently checked, and a dashboard must be able to
        tell a proposal from a verified fix.
        """
        self._set(
            incident_id,
            status=STATUS_FIX_VALIDATED,
            validation=json.dumps(validation),
        )

    def write_validation_failed(self, incident_id: int, validation: dict) -> None:
        """The diff didn't apply, or the tests didn't pass with it.

        Terminal — no PR is opened. The recorded evidence carries git's or
        pytest's own output so the failure is diagnosable without a rerun.
        """
        self._set(
            incident_id,
            status=STATUS_FIX_FAILED,
            validation=json.dumps(validation),
        )

    def write_pr_opened(self, incident_id: int, pr_url: str) -> None:
        self._set(incident_id, status=STATUS_PR_OPENED, pr_url=pr_url)

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
