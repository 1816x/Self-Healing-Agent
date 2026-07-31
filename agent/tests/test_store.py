"""Tests for the incident store, against the real Go-created schema."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from conftest import insert_incident

from diagnose import store as store_mod
from diagnose.store import SchemaTooOldError, Store


def test_opens_a_real_monitor_database(real_schema_db):
    """The schema the monitor actually creates satisfies the agent."""
    with Store(str(real_schema_db)) as s:
        assert s.count_by_status(store_mod.STATUS_DETECTED) == 0


def test_rejects_a_database_older_than_required(tmp_path):
    """A database one version behind fails loudly, naming the fix.

    This is the guard that keeps the cross-language schema contract honest:
    the agent never silently operates on a database missing its columns.
    Both versions come from the constant rather than being written out, so
    the next schema bump doesn't have to remember to edit this test.
    """
    stale = store_mod.REQUIRED_SCHEMA_VERSION - 1
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY)")
        db.execute(f"PRAGMA user_version = {stale}")

    with pytest.raises(SchemaTooOldError) as excinfo:
        Store(str(path))
    message = str(excinfo.value)
    assert f"v{stale}" in message and f"v{store_mod.REQUIRED_SCHEMA_VERSION}" in message
    assert "monitor" in message.lower(), "error should point at who owns migrations"


def test_reads_incident_with_parsed_evidence(real_schema_db):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as s:
        incident = s.get(incident_id)

    assert incident is not None
    assert incident.kind == "error_rate"
    assert incident.evidence["metrics"]["error_count"] == 5
    assert incident.evidence["routes"]["/checkout"] == 5
    assert "#" in incident.summary_line()


def test_corrupt_evidence_does_not_make_an_incident_unreadable(real_schema_db):
    """A bad evidence blob degrades to empty, it doesn't raise.

    The agent has tools to go read the logs itself, so a corrupt summary
    shouldn't take an incident permanently out of the pipeline.
    """
    incident_id = insert_incident(real_schema_db)
    with closing(sqlite3.connect(real_schema_db)) as db, db:
        db.execute("UPDATE incidents SET evidence = ? WHERE id = ?", ("not json{", incident_id))

    with Store(str(real_schema_db)) as s:
        incident = s.get(incident_id)
    assert incident is not None
    assert incident.evidence == {}


def test_get_returns_none_for_unknown_id(real_schema_db):
    with Store(str(real_schema_db)) as s:
        assert s.get(9999) is None


# --- claiming ---


def test_claim_next_detected_transitions_to_diagnosing(real_schema_db):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as s:
        claimed = s.claim_next_detected()
        assert claimed is not None
        assert claimed.id == incident_id
        assert claimed.status == store_mod.STATUS_DIAGNOSING
        assert s.count_by_status(store_mod.STATUS_DETECTED) == 0


def test_claim_next_detected_returns_none_when_nothing_pending(real_schema_db):
    with Store(str(real_schema_db)) as s:
        assert s.claim_next_detected() is None


def test_claim_is_exclusive_across_two_stores(real_schema_db):
    """Two agent processes racing on one store: exactly one wins.

    This is the property the conditional UPDATE exists for — without the
    status guard, both connections would 'claim' the same incident and
    diagnose it twice (two model runs, two diffs, one incident).
    """
    insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as first, Store(str(real_schema_db)) as second:
        first_claim = first.claim_next_detected()
        second_claim = second.claim_next_detected()

    assert first_claim is not None
    assert second_claim is None


def test_claim_specific_id_refuses_an_already_claimed_incident(real_schema_db):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as s:
        assert s.claim(incident_id) is not None
        assert s.claim(incident_id) is None, "second claim of the same id must fail"


def test_claim_skips_incidents_not_in_detected(real_schema_db):
    insert_incident(real_schema_db, status=store_mod.STATUS_DIAGNOSED)
    with Store(str(real_schema_db)) as s:
        assert s.claim_next_detected() is None


def test_claim_next_takes_the_oldest_first(real_schema_db):
    first_id = insert_incident(real_schema_db)
    insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as s:
        claimed = s.claim_next_detected()
    assert claimed is not None and claimed.id == first_id


# --- writing back ---


def _read_row(db_path, incident_id: int) -> sqlite3.Row:
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()


def test_write_diagnosis_records_source_and_status(real_schema_db):
    incident_id = insert_incident(real_schema_db)
    diagnosis = {
        "source": "replay",
        "root_cause": "price lookup keyed by str while validation checks int",
        "suspect_commit": "abc1234",
    }
    with Store(str(real_schema_db)) as s:
        s.claim(incident_id)
        s.write_diagnosis(incident_id, diagnosis)

    row = _read_row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_DIAGNOSED
    assert row["diagnosed_at"] != ""
    stored = json.loads(row["diagnosis"])
    assert stored["source"] == "replay", "source must survive so a canned answer is never mistaken for live"
    assert stored["suspect_commit"] == "abc1234"


def test_write_proposed_fix_records_diff_without_applying_it(real_schema_db):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as s:
        s.claim(incident_id)
        s.write_proposed_fix(
            incident_id,
            {"source": "live", "root_cause": "KeyError on int/str mismatch"},
            {"diff": "--- a/x\n+++ b/x\n", "rationale": "key the table by int"},
        )

    row = _read_row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_PROPOSED
    assert json.loads(row["proposed_fix"])["rationale"] == "key the table by int"
    assert json.loads(row["diagnosis"])["source"] == "live"


def test_mark_failed_and_mark_refused_are_distinct_outcomes(real_schema_db):
    failed_id = insert_incident(real_schema_db)
    refused_id = insert_incident(real_schema_db)

    with Store(str(real_schema_db)) as s:
        s.claim(failed_id)
        s.mark_failed(failed_id, "iteration cap reached")
        s.claim(refused_id)
        s.mark_refused(refused_id, {"category": "cyber"})

    failed = _read_row(real_schema_db, failed_id)
    refused = _read_row(real_schema_db, refused_id)

    assert failed["status"] == store_mod.STATUS_DIAGNOSIS_FAILED
    assert refused["status"] == store_mod.STATUS_DIAGNOSIS_REFUSED
    assert failed["status"] != refused["status"], (
        "a model refusal is not a bug; conflating them hides why a diagnosis is missing"
    )
    assert json.loads(failed["diagnosis"])["error"] == "iteration cap reached"
    assert json.loads(refused["diagnosis"])["refusal"]["category"] == "cyber"


def test_validation_outcome_is_recorded_separately_from_diagnosis(real_schema_db):
    """A verified fix must be distinguishable from a mere proposal.

    The model's claim lands in `diagnosis`; what the gate independently
    checked lands in `validation`. If these shared a column a dashboard
    could not tell "the model says this works" from "this was run and it
    works" — which is the whole point of having a gate.
    """
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as s:
        s.claim(incident_id)
        s.write_proposed_fix(
            incident_id,
            {"source": "replay", "root_cause": "int/str key mismatch"},
            {"diff": "--- a/x\n+++ b/x\n", "rationale": "key by int"},
        )
        s.write_validated(
            incident_id,
            {"applied": True, "tests_before": "failed", "tests_after": "passed"},
        )

    row = _read_row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_VALIDATED
    assert json.loads(row["validation"])["tests_after"] == "passed"
    # The Phase 3 diagnosis is still intact underneath it.
    assert json.loads(row["diagnosis"])["root_cause"] == "int/str key mismatch"


def test_validation_failure_is_terminal_and_keeps_the_reason(real_schema_db):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as s:
        s.claim(incident_id)
        s.write_validation_failed(
            incident_id,
            {"applied": False, "error": "error: patch failed: demo-app/app/main.py:29"},
        )

    row = _read_row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_FAILED
    assert row["pr_url"] == "", "a failed validation must never carry a PR"
    assert "patch failed" in json.loads(row["validation"])["error"], (
        "git's own message is kept so the failure is diagnosable without a rerun"
    )


def test_write_pr_opened_records_the_url(real_schema_db):
    incident_id = insert_incident(real_schema_db)
    url = "https://github.com/1816x/Self-Healing-Agent/pull/7"
    with Store(str(real_schema_db)) as s:
        s.claim(incident_id)
        s.write_pr_opened(incident_id, url)

    row = _read_row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_PR_OPENED
    assert row["pr_url"] == url
