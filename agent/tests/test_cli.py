"""Tests for the CLI's shipping and resume steps.

The seam that matters here is the one between "the model proposed
something" and "the incident reached a terminal status". Every path
through `_ship` must leave the incident somewhere a later run can reason
about — a claimed incident parked in `fix_proposed` forever is the failure
mode these tests exist to prevent.

Phase 5 added the other half: `--resume` picks an incident back up when a
previous run left it stranded, and must drive the *stored* fix through the
gate rather than re-deriving it. `_refuse_to_diagnose` is how that is
asserted — a resumed fix that touches the model fails the test.

Nothing here touches the network: the PR path is exercised via
--dry-run-pr, which is also the flag a human would use before trusting it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
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
    # The base a fix PR targets: the branch carrying the injected bug, as
    # `inject_bug.sh --push` would have published it.
    _git(root, "branch", "demo/b1-abc1234")
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


def _resume_argv(db_path, repo: Path, *extra: str) -> list[str]:
    return [
        "--db", str(db_path),
        "--repo-root", str(repo),
        "--base-branch", "demo/b1-abc1234",
        "--test-command", "python -m pytest -q",
        "--test-cwd", "pkg",
        *extra,
    ]


def _park_at_fix_proposed(db_path, diff: str = GOOD_DIFF) -> int:
    """Puts an incident exactly where --no-validate leaves one."""
    incident_id = insert_incident(db_path)
    with Store(str(db_path)) as store:
        store.claim(incident_id)
        store.write_proposed_fix(incident_id, DIAGNOSIS, {"diff": diff, "rationale": "r"})
    return incident_id


def _refuse_to_diagnose(monkeypatch) -> None:
    """Makes any model/replay turn a hard failure.

    The point of resuming a stored fix is that it does *not* re-run the loop:
    the diff is already in the row, and re-deriving it would spend a real API
    call and overwrite the diagnosis that produced it.
    """
    def boom(*_a, **_k):
        raise AssertionError("resume must not re-run the diagnosis loop")

    monkeypatch.setattr(cli, "_run_offline", boom)
    monkeypatch.setattr(cli, "_run_live", boom)


# --- Phase 5: resuming ---


def test_a_parked_fix_is_ignored_without_resume(real_schema_db, repo, capsys):
    incident_id = _park_at_fix_proposed(real_schema_db)

    assert cli.main(_resume_argv(real_schema_db, repo)) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _row(real_schema_db, incident_id)["status"] == store_mod.STATUS_FIX_PROPOSED


def test_resume_drives_a_parked_fix_through_the_gate(real_schema_db, repo, monkeypatch, capsys):
    incident_id = _park_at_fix_proposed(real_schema_db)
    _refuse_to_diagnose(monkeypatch)

    assert cli.main(_resume_argv(real_schema_db, repo, "--resume")) == 0

    row = _row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_VALIDATED
    assert json.loads(row["validation"])["proves_a_fix"] is True
    assert json.loads(row["diagnosis"])["turns"] == 3, "the stored diagnosis must survive"
    assert "not the model" in capsys.readouterr().out


def test_resume_records_a_failing_parked_fix_as_fix_failed(real_schema_db, repo, monkeypatch):
    incident_id = _park_at_fix_proposed(real_schema_db, diff=NO_OP_DIFF)
    _refuse_to_diagnose(monkeypatch)

    assert cli.main(_resume_argv(real_schema_db, repo, "--resume")) == 1
    assert _row(real_schema_db, incident_id)["status"] == store_mod.STATUS_FIX_FAILED


def test_resume_with_no_validate_does_not_strand_the_incident(real_schema_db, repo, monkeypatch):
    """--resume claims the row, so bailing out must put the status back.

    Otherwise the flag that created this resting state would, on a rerun,
    leave the incident in `diagnosing` — a worse version of the bug being
    fixed here.
    """
    incident_id = _park_at_fix_proposed(real_schema_db)
    _refuse_to_diagnose(monkeypatch)

    assert cli.main(_resume_argv(real_schema_db, repo, "--resume", "--no-validate")) == 0
    assert _row(real_schema_db, incident_id)["status"] == store_mod.STATUS_FIX_PROPOSED


def test_resume_reclaims_a_dead_claim_and_rediagnoses_it(real_schema_db, repo, capsys):
    """A row stuck in `diagnosing` has no stored fix, so it re-runs the loop."""
    incident_id = insert_incident(real_schema_db, status=store_mod.STATUS_DIAGNOSING)
    with closing(sqlite3.connect(real_schema_db)) as db, db:
        db.execute(
            "UPDATE incidents SET updated_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", incident_id),
        )

    cli.main(_resume_argv(real_schema_db, repo, "--resume", "--offline", "--no-validate"))

    out = capsys.readouterr().out
    assert "reclaiming incident" in out
    assert _row(real_schema_db, incident_id)["status"] != store_mod.STATUS_DIAGNOSING


def test_resume_leaves_a_live_claim_alone(real_schema_db, repo, capsys):
    """The lease must not expire under a run that is still working."""
    incident_id = insert_incident(real_schema_db, status=store_mod.STATUS_DIAGNOSING)
    fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(sqlite3.connect(real_schema_db)) as db, db:
        db.execute("UPDATE incidents SET updated_at = ? WHERE id = ?", (fresh, incident_id))

    assert cli.main(_resume_argv(real_schema_db, repo, "--resume")) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _row(real_schema_db, incident_id)["status"] == store_mod.STATUS_DIAGNOSING


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
    assert "GITHUB_TOKEN" in json.loads(row["validation"])["pr_error"]


def test_a_failed_pull_request_does_not_erase_a_passing_validation(real_schema_db, repo, monkeypatch):
    """A GitHub outage is not a verdict on the fix.

    The first version of this collapsed both into fix_failed, so a 403
    from GitHub overwrote a genuine "applied cleanly, red before, green
    after" record — discarding the only thing the run had established.
    Found on the first real end-to-end run, where the sandbox's token
    could not reach the API.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setattr(cli.pr, "push_branch", lambda *a, **k: "0" * 40)

    def refuse(*_a, **_k):
        raise cli.pr.PROpenError("GitHub refused the pull request (HTTP 403)")

    monkeypatch.setattr(cli.pr, "create_pull_request", refuse)

    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(
            store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "r"},
            _args(repo, open_pr=True),
        )

    assert code == 1, "the run did not achieve what was asked of it"
    row = _row(real_schema_db, incident_id)
    assert row["status"] == store_mod.STATUS_FIX_VALIDATED, (
        "the fix did validate; only the PR failed"
    )
    record = json.loads(row["validation"])
    assert record["proves_a_fix"] is True, "the validation evidence must survive"
    assert "403" in record["pr_error"]
    assert row["pr_url"] == ""


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
    assert row["pr_url"] == "", "a refused repository must never produce a PR"
    assert "someone-else/their-repo" in json.loads(row["validation"])["pr_error"]


def test_base_branch_defaults_to_the_branch_the_bug_is_on(repo):
    """Not 'main': the injected bug commit is local, so a PR against a base
    without it could not apply, let alone pass CI."""
    _git(repo, "checkout", "-q", "-b", "demo/b1-deadbee")
    assert cli._current_branch(repo) == "demo/b1-deadbee"


def test_validation_uses_the_base_branch_not_head(real_schema_db, repo, capsys):
    """Validate the tree the PR targets, or the PR carries unrelated commits.

    The first real PR this pipeline produced contained an unrelated
    refactor, because the gate cut its worktree from HEAD while the PR
    targeted a base branch published several commits earlier. Everything
    in between rode along.
    """
    _git(repo, "branch", "demo/b1-base")
    # A commit that exists on HEAD but not on the base branch.
    (repo / "unrelated.txt").write_text("not part of the fix\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated work")

    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(
            store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "r"},
            _args(repo, base_branch="demo/b1-base"),
        )

    assert code == 0
    assert "demo/b1-base" in capsys.readouterr().out


def test_a_missing_base_branch_is_reported_rather_than_guessed(real_schema_db, repo):
    incident_id = insert_incident(real_schema_db)
    with Store(str(real_schema_db)) as store:
        incident = store.claim(incident_id)
        code = cli._ship(
            store, incident, DIAGNOSIS, {"diff": GOOD_DIFF, "rationale": "r"},
            _args(repo, base_branch="demo/never-existed"),
        )

    assert code == 1
    row = _row(real_schema_db, incident_id)
    assert "demo/never-existed" in json.loads(row["validation"])["error"]
