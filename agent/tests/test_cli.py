"""Tests for the CLI's Phase 4 shipping step.

The seam that matters here is the one between "the model proposed
something" and "the incident reached a terminal status". Every path
through `_ship` must leave the incident somewhere a later run can reason
about — a claimed incident parked in `fix_proposed` forever is the failure
mode these tests exist to prevent.

Nothing here touches the network: the PR path is exercised via
--dry-run-pr, which is also the flag a human would use before trusting it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from conftest import insert_incident

from diagnose import __main__ as cli
from diagnose import store as store_mod
from diagnose.store import Store

_SOURCE_BROKEN = 'VALUE = "broken"\n'
_TEST_FILE = 'from src import VALUE\n\n\ndef test_value():\n    assert VALUE == "fixed"\n'

GOOD_DIFF = """--- a/pkg/src.py
+++ b/pkg/src.py
@@ -1 +1 @@
-VALUE = "broken"
+VALUE = "fixed"
"""

BROKEN_DIFF = """--- a/pkg/src.py
+++ b/pkg/src.py
@@
-VALUE = "broken"
+VALUE = "fixed"
"""

NO_OP_DIFF = """--- a/pkg/src.py
+++ b/pkg/src.py
@@ -1 +1 @@
-VALUE = "broken"
+VALUE = "also broken"
"""


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=test", *argv],
        cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "src.py").write_text(_SOURCE_BROKEN)
    (root / "pkg" / "test_src.py").write_text(_TEST_FILE)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "perf: precompute lookup table")
    _git(root, "remote", "add", "origin", "https://github.com/1816x/Self-Healing-Agent.git")
    return root


def _args(repo: Path, **overrides) -> argparse.Namespace:
    fields = {
        "repo_root": str(repo),
        "base_branch": "demo/b1-abc1234",
        "test_command": "python -m pytest -q",
        "test_cwd": "pkg",
        "open_pr": False,
        "dry_run_pr": False,
        "no_validate": False,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


DIAGNOSIS = {
    "source": "replay-scripted",
    "root_cause": "VALUE is wrong",
    "suspect_commit": "abc1234",
    "tool_calls": [{"tool": "read_logs"}],
    "turns": 3,
}


def _row(db_path, incident_id: int) -> sqlite3.Row:
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()


def test_a_working_fix_reaches_fix_validated(real_schema_db, repo, capsys):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "r"}, _args(repo))

    assert code == 0
    row = _row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_VALIDATED
    assert json.loads(row["validation"])["proves_a_fix"] is True
    assert row["pr_url"] == "", "validation alone must not open a PR"
    assert "--open-pr" in capsys.readouterr().out


def test_a_diff_that_does_not_apply_reaches_fix_failed(real_schema_db, repo):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(store, incident, DIAGNOSIS, {"diff": BROKEN_DIFF, "rationale": "r"}, _args(repo))

    assert code == 1
    row = _row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_FAILED
    assert json.loads(row["validation"])["applied"] is False


def test_a_diff_that_leaves_tests_red_reaches_fix_failed(real_schema_db, repo):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(store, incident, DIAGNOSIS, {"diff": NO_OP_DIFF, "rationale": "r"}, _args(repo))

    assert code == 1
    row = _row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_FAILED
    record = json.loads(row["validation"])
    assert record["applied"] is True, "it applied — it just didn't work"
    assert record["tests_after"] == "failed"


def test_an_unexpected_error_still_leaves_a_terminal_status(real_schema_db, repo, monkeypatch):
    """The property that keeps incidents from getting stuck.

    An incident is already past 'detected' by the time _ship runs, so an
    unhandled exception here would park it in a status no later run
    reclaims.
    """
    def explode(*_a, **_k):
        raise RuntimeError("disk fell over")

    monkeypatch.setattr(cli.patch, "worktree", explode)

    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "r"}, _args(repo))

    assert code == 1
    row = _row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_FAILED
    assert "disk fell over" in json.loads(row["validation"])["error"]


def test_dry_run_prints_the_pull_request_and_sends_nothing(real_schema_db, repo, capsys, monkeypatch):
    def explode(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("dry run must not call the API")

    monkeypatch.setattr(cli.pr, "create_pull_request", explode)
    monkeypatch.setattr(cli.pr, "push_branch", explode)

    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(
            store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "key by int"},
            _args(repo, dry_run_pr=True),
        )

    assert code == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "demo/b1-abc1234" in out
    assert "fix/incident-" in out
    assert "nothing was pushed" in out

    row = _row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_VALIDATED, "a dry run must not claim a PR was opened"
    assert row["pr_url"] == ""


def test_open_pr_without_a_token_fails_loudly_rather_than_silently(real_schema_db, repo, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(
            store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "r"},
            _args(repo, open_pr=True),
        )

    assert code == 1
    row = _row(real_schema_db, incident_id)
    assert "GITHUB_TOKEN" in json.loads(row["validation"])["error"]


def test_open_pr_records_the_url_on_success(real_schema_db, repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setattr(cli.pr, "push_branch", lambda *a, **k: "0" * 40)
    monkeypatch.setattr(
        cli.pr, "create_pull_request",
        lambda *a, **k: cli.pr.PullRequest(9, "https://github.com/1816x/Self-Healing-Agent/pull/9"),
    )

    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(
            store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "r"},
            _args(repo, open_pr=True),
        )

    assert code == 0
    row = _row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_PR_OPENED
    assert row["pr_url"].endswith("/pull/9")


def test_a_third_party_remote_stops_the_pr_and_marks_it_failed(real_schema_db, repo, monkeypatch):
    _git(repo, "remote", "set-url", "origin", "https://github.com/someone-else/their-repo.git")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")

    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(
            store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "r"},
            _args(repo, open_pr=True),
        )

    assert code == 1
    row = _row(real_schema_db, incident_id)
    assert "someone-else/their-repo" in json.loads(row["validation"])["error"]


def test_base_branch_defaults_to_the_branch_the_bug_is_on(repo):
    """Not 'main': the injected bug commit is local, so a PR against a base
    without it could not apply, let alone pass CI."""
    _git(repo, "checkout", "-q", "-b", "demo/b1-deadbee")
    assert cli._current_branch(repo) == "demo/b1-deadbee"
