"""Tests for the tool implementations.

The git tools run against a real temporary git repository rather than a
mocked subprocess: the thing worth testing is that the argv we build
actually produces the blame output the agent reasons over, and a mock
would only assert that we call the function we wrote.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from diagnose.tools import PROPOSE_FIX, Toolbox, tool_definitions


@pytest.fixture
def git_repo(tmp_path):
    """A repo with two commits, so blame has something to distinguish."""
    root = tmp_path / "repo"
    (root / "demo-app" / "app").mkdir(parents=True)
    source = root / "demo-app" / "app" / "main.py"

    def git(*args):
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
            },
        )

    git("init", "-q")
    source.write_text("def checkout():\n    return total\n")
    git("add", ".")
    git("commit", "-q", "-m", "feat: add checkout")

    source.write_text("def checkout():\n    return PRICES[pid]\n    # regression here\n")
    git("add", ".")
    git("commit", "-q", "-m", "perf(checkout): precompute price lookup table")

    return root


@pytest.fixture
def logs(tmp_path):
    """A JSONL log file shaped exactly like the demo app's output."""
    path = tmp_path / "demo-app.jsonl"
    lines = [
        {"ts": "2026-07-29T12:00:00Z", "level": "info", "event": "request.completed",
         "route": "/products", "method": "GET", "status": 200, "duration_ms": 1.2},
        {"ts": "2026-07-29T12:00:01Z", "level": "error", "event": "request.unhandled_exception",
         "route": "/checkout", "method": "POST", "status": 500, "duration_ms": 3.1},
        {"ts": "2026-07-29T12:00:02Z", "level": "error", "event": "request.unhandled_exception",
         "route": "/checkout", "method": "POST", "status": 500, "duration_ms": 2.8},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


# --- tool definitions ---


def test_propose_fix_is_strict_and_closed():
    """The one tool whose output we parse and store is schema-locked."""
    definition = next(d for d in tool_definitions() if d["name"] == PROPOSE_FIX)
    assert definition["strict"] is True
    assert definition["input_schema"]["additionalProperties"] is False
    assert set(definition["input_schema"]["required"]) == {
        "root_cause", "suspect_commit", "diff", "rationale"
    }


def test_every_tool_has_a_description_and_schema():
    for definition in tool_definitions():
        assert definition["description"].strip(), f"{definition['name']} needs a description"
        assert definition["input_schema"]["type"] == "object"


def test_propose_fix_description_says_it_does_not_open_a_pr():
    """The description is the model's only source of truth about the gate."""
    definition = next(d for d in tool_definitions() if d["name"] == PROPOSE_FIX)
    text = definition["description"].lower()
    assert "not applied" in text and "pull request" in text


# --- read_logs ---


def test_read_logs_filters_by_level(git_repo, logs):
    box = Toolbox(git_repo, log_file=logs)
    out = box.read_logs(level="error")
    assert out.count("unhandled_exception") == 2
    assert "/products" not in out


def test_read_logs_filters_by_route(git_repo, logs):
    box = Toolbox(git_repo, log_file=logs)
    assert "/products" in box.read_logs(route="/products")
    assert "/checkout" not in box.read_logs(route="/products")


def test_read_logs_reports_no_matches_rather_than_empty_string(git_repo, logs):
    box = Toolbox(git_repo, log_file=logs)
    assert "No log lines matched" in box.read_logs(route="/nonexistent")


def test_read_logs_skips_malformed_lines_and_says_so(git_repo, tmp_path):
    path = tmp_path / "mixed.jsonl"
    path.write_text("garbage not json\n")
    box = Toolbox(git_repo, log_file=path)
    out = box.read_logs()
    assert "malformed" in out


def test_read_logs_errors_helpfully_when_the_app_never_ran(git_repo, tmp_path):
    box = Toolbox(git_repo, log_file=tmp_path / "missing.jsonl")
    result, is_error = box.dispatch("read_logs", {})
    assert is_error
    assert "does not exist" in result and "demo app" in result


# --- git tools ---


def test_git_log_recent_lists_newest_first(git_repo):
    box = Toolbox(git_repo)
    out = box.git_log_recent()
    lines = out.splitlines()
    assert "precompute price lookup table" in lines[0]
    assert "add checkout" in lines[1]


def test_git_log_respects_limit(git_repo):
    box = Toolbox(git_repo)
    assert len(box.git_log_recent(limit=1).splitlines()) == 1


def test_git_blame_names_the_commit_for_a_line(git_repo):
    """The core capability: blame maps a failing line to a suspect commit."""
    box = Toolbox(git_repo)
    out = box.git_blame("demo-app/app/main.py", start_line=2, end_line=2)

    expected_sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert expected_sha in out, "blame must attribute the changed line to the second commit"


def test_git_blame_confines_paths(git_repo):
    box = Toolbox(git_repo)
    result, is_error = box.dispatch("git_blame", {"file": "../../../etc/passwd"})
    assert is_error
    assert "outside the repository" in result


def test_git_blame_rejects_inverted_range(git_repo):
    box = Toolbox(git_repo)
    result, is_error = box.dispatch(
        "git_blame", {"file": "demo-app/app/main.py", "start_line": 9, "end_line": 2}
    )
    assert is_error and "before" in result


def test_git_tool_failure_surfaces_stderr(tmp_path):
    """Outside a git repo, the tool reports git's own error instead of crashing."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    box = Toolbox(not_a_repo)
    result, is_error = box.dispatch("git_log_recent", {})
    assert is_error
    # git's own diagnostic reaches the model, rather than a bare
    # "command failed" that gives it nothing to act on.
    assert "git command failed" in result
    assert "repository" in result.lower()


# --- read_source ---


def test_read_source_returns_numbered_lines(git_repo):
    box = Toolbox(git_repo)
    out = box.read_source("demo-app/app/main.py")
    assert "1\tdef checkout():" in out


def test_read_source_confines_paths(git_repo):
    box = Toolbox(git_repo)
    result, is_error = box.dispatch("read_source", {"file": "/etc/passwd"})
    assert is_error and "absolute" in result


# --- get_metrics ---


def test_get_metrics_errors_helpfully_when_app_is_down(git_repo):
    """Port 1 is never listening; the error must tell the model what to do next."""
    box = Toolbox(git_repo, metrics_url="http://127.0.0.1:1/metrics")
    result, is_error = box.dispatch("get_metrics", {})
    assert is_error
    assert "read_logs" in result, "error should redirect the model to a tool that still works"


# --- dispatch behavior ---


def test_unknown_tool_is_an_error_the_model_can_recover_from(git_repo):
    box = Toolbox(git_repo)
    result, is_error = box.dispatch("rm_rf", {"path": "/"})
    assert is_error
    assert "Unknown tool" in result
    assert "read_logs" in result, "should list what is actually available"


def test_bad_arguments_are_reported_not_raised(git_repo):
    box = Toolbox(git_repo)
    result, is_error = box.dispatch("read_logs", {"nonexistent_arg": 1})
    assert is_error and "Invalid arguments" in result


def test_dispatch_records_a_call_trace(git_repo, logs):
    """Every call is recorded for the incident record and the F5 trace view."""
    box = Toolbox(git_repo, log_file=logs)
    box.dispatch("read_logs", {"level": "error"})
    box.dispatch("git_log_recent", {"limit": 2})
    box.dispatch("read_source", {"file": "../escape"})

    assert [call["tool"] for call in box.calls] == ["read_logs", "git_log_recent", "read_source"]
    assert box.calls[0]["error"] is None
    assert box.calls[2]["error"] is not None, "rejections must be visible in the trace"


def test_guardrail_rejection_does_not_stop_later_calls(git_repo, logs):
    """A refused path is a recoverable turn, not a dead loop."""
    box = Toolbox(git_repo, log_file=logs)
    _, first_error = box.dispatch("read_source", {"file": "../../secret"})
    result, second_error = box.dispatch("read_logs", {"level": "error"})
    assert first_error
    assert not second_error and "unhandled_exception" in result
