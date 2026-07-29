"""Tests for the offline fallbacks and API-error classification."""

from __future__ import annotations

import json
import subprocess

import anthropic
import httpx
import pytest

from diagnose import heuristic
from diagnose.client import describe_api_error
from diagnose.replay import ReplayCompleter, save_transcript
from diagnose.store import Incident
from diagnose.tools import Toolbox


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file.txt").write_text("x\n")
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "perf(checkout): precompute price lookup table"],
        cwd=root, check=True, capture_output=True, env=env,
    )
    return root


def incident(kind="error_rate", **evidence_overrides) -> Incident:
    evidence = {
        "summary": "5 errors across 1 route(s)",
        "metrics": {"error_count": 5},
        "routes": {"/checkout": 5},
        "samples": [],
    }
    evidence.update(evidence_overrides)
    return Incident(
        id=1, kind=kind, dedup_key=kind, status="diagnosing",
        window_start="2026-07-29T12:00:00Z", window_end="2026-07-29T12:00:03Z",
        occurrences=1, evidence=evidence,
    )


# --- heuristic fallback ---


def test_heuristic_labels_itself_as_not_a_model_diagnosis(repo):
    """The property that makes the fallback honest rather than misleading."""
    result = heuristic.diagnose(incident(), Toolbox(repo))

    assert result["source"] == "heuristic"
    assert result["confidence"] == "low"
    assert "NOT A MODEL DIAGNOSIS" in result["root_cause"]
    assert "record a transcript" in result["note"]


def test_heuristic_names_the_newest_commit_as_a_weak_suspect(repo):
    result = heuristic.diagnose(incident(), Toolbox(repo))

    expected = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert result["suspect_commit"] == expected
    assert any("precompute price lookup" in line for line in result["recent_commits"])


def test_heuristic_guidance_differs_by_incident_kind(repo):
    box = Toolbox(repo)
    latency = heuristic.diagnose(incident(kind="latency_p95"), box)["root_cause"]
    memory = heuristic.diagnose(incident(kind="memory_growth"), box)["root_cause"]

    assert "slower" in latency
    assert "without bound" in memory
    assert latency != memory


def test_heuristic_survives_an_unreadable_git_history(tmp_path):
    """No git repo: reports the problem instead of raising, and guesses nothing."""
    plain = tmp_path / "plain"
    plain.mkdir()
    result = heuristic.diagnose(incident(), Toolbox(plain))

    assert result["suspect_commit"] is None, "must not invent a commit it couldn't read"
    assert "Could not read git history" in result["root_cause"]


def test_heuristic_handles_an_unknown_incident_kind(repo):
    result = heuristic.diagnose(incident(kind="something_new"), Toolbox(repo))
    assert "Unrecognized incident kind" in result["root_cause"]


# --- transcript round trip ---


def test_saved_transcript_replays(tmp_path):
    turns = [
        {"stop_reason": "tool_use", "content": [
            {"type": "thinking", "thinking": "checking logs"},
            {"type": "tool_use", "id": "t1", "name": "read_logs", "input": {"level": "error"}},
        ]},
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t2", "name": "propose_fix",
             "input": {"root_cause": "rc", "suspect_commit": "abc1234",
                       "diff": "d", "rationale": "r"}},
        ]},
    ]
    path = save_transcript("error_rate", turns, directory=tmp_path)
    assert json.loads(path.read_text())["kind"] == "error_rate"

    completer = ReplayCompleter.for_kind("error_rate", directory=tmp_path)
    assert completer is not None

    first = completer([])
    assert first.content[0].thinking == "checking logs"
    assert first.content[1].input == {"level": "error"}
    second = completer([])
    assert second.content[0].input["suspect_commit"] == "abc1234"


# --- API error classification ---


def _status_error(cls, status_code):
    """Builds a real SDK exception, since these carry required constructor args."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return cls("boom", response=response, body=None)


def test_retryable_and_non_retryable_errors_are_distinguished():
    """The operational distinction a single catch-all would erase."""
    rate_limited = describe_api_error(_status_error(anthropic.RateLimitError, 429))
    bad_request = describe_api_error(_status_error(anthropic.BadRequestError, 400))

    assert "retryable" in rate_limited
    assert "not retryable" in bad_request


def test_authentication_error_points_at_offline_mode():
    """The most likely first-run failure gets an actionable message."""
    message = describe_api_error(_status_error(anthropic.AuthenticationError, 401))
    assert "--offline" in message


def test_unknown_exception_is_labeled_rather_than_swallowed():
    message = describe_api_error(RuntimeError("something odd"))
    assert "unexpected error" in message
    assert "something odd" in message
