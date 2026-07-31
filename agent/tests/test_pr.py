"""Tests for the pull-request opener.

The GitHub call itself is stubbed — these tests must never reach the
network, and asserting that urlopen was handed the right request is the
whole of what there is to check. The guardrails around it (allowlist,
branch naming, never-merge) are tested against real git.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from diagnose import pr
from diagnose.patch import ValidationResult
from diagnose.store import Incident


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=test", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file.txt").write_text("x\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


def incident(**overrides) -> Incident:
    fields = {
        "id": 7,
        "kind": "error_rate",
        "dedup_key": "error_rate:/checkout",
        "status": "fix_validated",
        "window_start": "2026-07-29T12:00:00Z",
        "window_end": "2026-07-29T12:00:03Z",
        "occurrences": 2,
        "evidence": {"summary": "12 errors on POST /checkout"},
    }
    fields.update(overrides)
    return Incident(**fields)


def validation(**overrides) -> ValidationResult:
    fields = {
        "ok": True,
        "applied": True,
        "tests_before": "failed",
        "tests_after": "passed",
        "proves_a_fix": True,
        "files": ("demo-app/app/main.py",),
    }
    fields.update(overrides)
    return ValidationResult(**fields)


# --- the allowlist ---


def test_resolves_the_allowed_repository_from_origin(repo):
    _git(repo, "remote", "add", "origin", "git@github.com:1816x/Self-Healing-Agent.git")
    assert pr.resolve_repository(repo) == "1816x/Self-Healing-Agent"


def test_https_and_ssh_remotes_both_resolve(repo):
    _git(repo, "remote", "add", "origin", "https://github.com/1816x/Self-Healing-Agent")
    assert pr.resolve_repository(repo) == "1816x/Self-Healing-Agent"


def test_a_token_bearing_https_remote_still_resolves(repo):
    """Credentials embedded in the remote URL are common in CI checkouts."""
    _git(repo, "remote", "add", "origin", "https://x-access-token:secret@github.com/1816x/Self-Healing-Agent.git")
    assert pr.resolve_repository(repo) == "1816x/Self-Healing-Agent"


def test_refuses_a_third_party_repository(repo):
    """The guardrail that matters most.

    The agent reads content it did not write — log lines, commit subjects,
    source comments. The step that writes somewhere public is the one a
    prompt injection would most want, so the destination is pinned in code
    rather than derived from whatever the remote happens to say.
    """
    _git(repo, "remote", "add", "origin", "git@github.com:someone-else/their-repo.git")
    with pytest.raises(pr.RepositoryNotAllowed) as excinfo:
        pr.resolve_repository(repo)
    assert "someone-else/their-repo" in str(excinfo.value)


def test_refuses_a_non_github_remote(repo):
    _git(repo, "remote", "add", "origin", "https://gitlab.com/1816x/Self-Healing-Agent.git")
    with pytest.raises(pr.RepositoryNotAllowed):
        pr.resolve_repository(repo)


def test_accepts_a_loopback_git_proxy_remote(repo):
    """This repo is developed in sandboxes whose git goes via a local proxy.

    The remote is literally `http://127.0.0.1:<port>/git/<owner>/<repo>`,
    so refusing every non-github.com host would make the agent unable to
    run where it is actually built.
    """
    _git(repo, "remote", "add", "origin", "http://local_proxy@127.0.0.1:41729/git/1816x/Self-Healing-Agent")
    assert pr.resolve_repository(repo) == "1816x/Self-Healing-Agent"


def test_the_allowlist_still_applies_behind_a_local_proxy(repo):
    _git(repo, "remote", "add", "origin", "http://127.0.0.1:41729/git/someone-else/their-repo")
    with pytest.raises(pr.RepositoryNotAllowed):
        pr.resolve_repository(repo)


def test_a_remote_host_cannot_impersonate_the_proxy_path_layout(repo):
    """Allowing the proxy's path shape must not allow it from anywhere.

    Without the host check, `https://evil.com/git/1816x/Self-Healing-Agent`
    parses to an allowlisted slug and would be pushed to.
    """
    _git(repo, "remote", "add", "origin", "https://evil.com/git/1816x/Self-Healing-Agent")
    with pytest.raises(pr.RepositoryNotAllowed) as excinfo:
        pr.resolve_repository(repo)
    assert "evil.com" in str(excinfo.value)


def test_refuses_when_there_is_no_origin(repo):
    with pytest.raises(pr.PROpenError):
        pr.resolve_repository(repo)


# --- the request ---


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_creates_a_pull_request_without_merging_it(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.get_header("Authorization")
        return _FakeResponse({"number": 12, "html_url": "https://github.com/o/r/pull/12"})

    monkeypatch.setattr(pr, "urlopen", fake_urlopen)

    result = pr.create_pull_request(
        "1816x/Self-Healing-Agent",
        title="fix: incident #7",
        body="because",
        head="fix/incident-7",
        base="demo/b1-abc1234",
        token="t0ken",
    )

    assert result.number == 12
    assert result.url.endswith("/pull/12")
    assert captured["url"].endswith("/repos/1816x/Self-Healing-Agent/pulls")
    assert captured["method"] == "POST"
    assert captured["body"]["base"] == "demo/b1-abc1234"
    assert captured["auth"] == "Bearer t0ken"
    # There is no merge call anywhere in this module, by design.
    assert not hasattr(pr, "merge_pull_request")


def test_refuses_to_open_a_pull_request_onto_itself(monkeypatch):
    def explode(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("must not call the API")

    monkeypatch.setattr(pr, "urlopen", explode)

    with pytest.raises(pr.PROpenError):
        pr.create_pull_request(
            "1816x/Self-Healing-Agent",
            title="t", body="b", head="main", base="main", token="t",
        )


def test_api_errors_are_reported_not_swallowed(monkeypatch):
    from urllib.error import HTTPError

    def fake_urlopen(request, timeout=None):
        raise HTTPError(request.full_url, 422, "Unprocessable", {}, None)

    monkeypatch.setattr(pr, "urlopen", fake_urlopen)

    with pytest.raises(pr.PROpenError) as excinfo:
        pr.create_pull_request(
            "1816x/Self-Healing-Agent",
            title="t", body="b", head="h", base="b2", token="t",
        )
    assert "422" in str(excinfo.value)


# --- the body ---


def test_body_states_what_was_verified():
    body = pr.build_body(
        incident(),
        {"source": "live", "root_cause": "int/str key mismatch", "suspect_commit": "abc1234",
         "tool_calls": [{"tool": "read_logs"}, {"tool": "git_blame"}], "turns": 4},
        {"diff": "...", "rationale": "key by int"},
        validation(),
        base="demo/b1-abc1234",
    )

    assert "int/str key mismatch" in body
    assert "abc1234" in body
    assert "read_logs, git_blame" in body
    assert "`failed`" in body and "`passed`" in body
    assert "red before this diff and green after" in body
    assert "merged automatically" in body


def test_body_flags_a_scripted_diagnosis_as_not_model_output():
    """The offline demo must not read as a claim about the model.

    A hand-authored transcript and a live run produce the same shape of
    diff. If the pull request bodies were identical, replaying a scripted
    transcript would quietly become a statement that a model diagnosed it.
    """
    body = pr.build_body(
        incident(),
        {"source": "replay-scripted", "root_cause": "x", "tool_calls": [], "turns": 1},
        {"rationale": "y"},
        validation(),
        base="demo/b1",
    )
    assert "hand-authored" in body
    assert "not model output" in body


def test_body_is_honest_when_the_tests_do_not_witness_the_regression():
    """B2/B3: the suite is green before and after, so it proves less."""
    body = pr.build_body(
        incident(kind="latency_p95"),
        {"source": "live", "root_cause": "N+1 lookup", "tool_calls": [], "turns": 3},
        {"rationale": "batch the lookup"},
        validation(tests_before="passed", proves_a_fix=False),
        base="demo/b2",
    )
    assert "do **not** witness the regression" in body
    assert "Reviewer judgement" in body


def test_body_mentions_a_recounted_diff():
    body = pr.build_body(
        incident(),
        {"source": "live", "root_cause": "x", "tool_calls": [], "turns": 2},
        {"rationale": "y"},
        validation(recounted=True),
        base="demo/b1",
    )
    assert "--recount" in body


# --- branch naming and pushing ---


def test_branch_name_is_scoped_to_the_incident():
    assert pr.branch_name(7) == "fix/incident-7"


def test_push_branch_commits_the_validated_tree(repo, tmp_path):
    """The commit comes from the tree the gate tested, not a re-application."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(bare))

    work = tmp_path / "work"
    _git(repo, "worktree", "add", "-q", "--detach", str(work))
    (work / "file.txt").write_text("fixed\n")

    head = pr.push_branch(work, "fix/incident-7", "fix: incident #7")

    assert len(head) == 40
    listed = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare), "fix/incident-7"],
        capture_output=True, text=True, check=True,
    )
    assert "fix/incident-7" in listed.stdout
    assert head[:8] in listed.stdout
