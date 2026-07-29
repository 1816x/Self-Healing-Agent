"""Tests for the function-calling loop.

Driven by a scripted completer rather than the API. The fakes reuse
``replay.Block``/``Response`` — the same types offline mode uses — so
these tests exercise the exact code path a real replay does instead of a
parallel one that could drift.
"""

from __future__ import annotations

import subprocess

import pytest

from diagnose import loop
from diagnose.replay import Block, ReplayCompleter, Response, TranscriptExhaustedError
from diagnose.tools import Toolbox


@pytest.fixture
def repo(tmp_path):
    """A minimal git repo so the real tools have something to read."""
    root = tmp_path / "repo"
    (root / "demo-app" / "app").mkdir(parents=True)
    (root / "demo-app" / "app" / "main.py").write_text("def checkout():\n    return 1\n")
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=root, check=True, capture_output=True, env=env,
    )
    return root


@pytest.fixture
def toolbox(repo, tmp_path):
    logs = tmp_path / "demo.jsonl"
    logs.write_text(
        '{"ts":"2026-07-29T12:00:00Z","level":"error","event":"boom","route":"/checkout"}\n'
    )
    return Toolbox(repo, log_file=logs)


class ScriptedCompleter:
    """Returns pre-built turns and records the messages it was handed."""

    def __init__(self, turns: list[Response]):
        self._turns = turns
        self.seen: list[list[dict]] = []

    def __call__(self, messages):
        self.seen.append([dict(m) for m in messages])
        return self._turns[len(self.seen) - 1]


def tool_use(name, arguments, block_id="toolu_1"):
    return Block(type="tool_use", id=block_id, name=name, input=arguments)


def propose(**fields):
    return tool_use("propose_fix", fields, block_id="toolu_fix")


# --- terminal tool ---


def test_propose_fix_ends_the_loop_with_the_proposal(toolbox):
    completer = ScriptedCompleter([
        Response("tool_use", [propose(
            root_cause="price table keyed by str, validation checks int",
            suspect_commit="abc1234",
            diff="--- a/x\n+++ b/x\n",
            rationale="key the table by int",
        )]),
    ])

    outcome = loop.run(completer, toolbox, "briefing")

    assert outcome.status == "fix_proposed"
    assert outcome.proposed_fix["suspect_commit"] == "abc1234"
    assert outcome.turns == 1


def test_investigates_then_proposes(toolbox):
    """The normal shape: read something, then conclude."""
    completer = ScriptedCompleter([
        Response("tool_use", [tool_use("read_logs", {"level": "error"})]),
        Response("tool_use", [tool_use("git_log_recent", {"limit": 3}, "toolu_2")]),
        Response("tool_use", [propose(
            root_cause="rc", suspect_commit="deadbee", diff="d", rationale="r"
        )]),
    ])

    outcome = loop.run(completer, toolbox, "briefing")

    assert outcome.status == "fix_proposed"
    assert outcome.turns == 3
    assert [call["tool"] for call in outcome.tool_calls] == ["read_logs", "git_log_recent"]


def test_propose_fix_short_circuits_other_calls_in_the_same_turn(toolbox):
    """Once the model has concluded, remaining reads in that turn are skipped.

    Executing them would spend time and tokens producing results no one
    will ever look at.
    """
    completer = ScriptedCompleter([
        Response("tool_use", [
            propose(root_cause="rc", suspect_commit="s", diff="d", rationale="r"),
            tool_use("read_logs", {}, "toolu_late"),
        ]),
    ])

    outcome = loop.run(completer, toolbox, "briefing")

    assert outcome.status == "fix_proposed"
    assert outcome.tool_calls == [], "no tool should run after propose_fix in the same turn"


# --- message protocol ---


def test_all_tool_results_come_back_in_one_user_message(toolbox):
    """Parallel tool calls must be answered in a single user turn.

    Splitting them across messages trains the model out of parallel calls,
    and omitting one is an API error.
    """
    completer = ScriptedCompleter([
        Response("tool_use", [
            tool_use("read_logs", {"level": "error"}, "toolu_a"),
            tool_use("git_log_recent", {"limit": 2}, "toolu_b"),
        ]),
        Response("end_turn", [Block(type="text", text="done")]),
    ])

    loop.run(completer, toolbox, "briefing")

    second_call_messages = completer.seen[1]
    results_message = second_call_messages[-1]
    assert results_message["role"] == "user"
    ids = [block["tool_use_id"] for block in results_message["content"]]
    assert ids == ["toolu_a", "toolu_b"], "both results, one message, original order"


def test_assistant_turns_are_echoed_back_verbatim_including_thinking(toolbox):
    """Thinking blocks must survive the round trip unmodified.

    The API rejects altered thinking blocks, and dropping them can break
    block ordering — so the whole content array goes back as-is.
    """
    thinking = Block(type="thinking", thinking="considering the logs")
    completer = ScriptedCompleter([
        Response("tool_use", [thinking, tool_use("read_logs", {})]),
        Response("end_turn", [Block(type="text", text="done")]),
    ])

    outcome = loop.run(completer, toolbox, "briefing")

    echoed = completer.seen[1][1]
    assert echoed["role"] == "assistant"
    assert echoed["content"][0] is thinking, "thinking block must be passed through untouched"
    assert outcome.reasoning == ["considering the logs"]


# --- non-conclusive exits ---


def test_text_only_turn_ends_as_no_conclusion(toolbox):
    """Stopping with prose is legitimate when evidence doesn't support a call."""
    completer = ScriptedCompleter([
        Response("end_turn", [Block(type="text", text="Evidence is inconclusive.")]),
    ])

    outcome = loop.run(completer, toolbox, "briefing")

    assert outcome.status == "no_conclusion"
    assert "inconclusive" in outcome.text


def test_iteration_cap_stops_a_model_that_never_concludes(toolbox):
    """The hard stop. Without it, a read_logs loop runs until the bill says stop."""
    completer = ScriptedCompleter([
        Response("tool_use", [tool_use("read_logs", {}, f"toolu_{n}")]) for n in range(10)
    ])

    outcome = loop.run(completer, toolbox, "briefing", max_turns=4)

    assert outcome.status == "iteration_cap"
    assert outcome.turns == 4
    assert len(outcome.tool_calls) == 4, "must stop calling tools at the cap, not run on"


def test_refusal_is_detected_before_content_is_read(toolbox):
    """A refusal has empty content; indexing it blindly is the bug this avoids."""
    completer = ScriptedCompleter([Response("refusal", [])])

    outcome = loop.run(completer, toolbox, "briefing")

    assert outcome.status == "refused"
    assert outcome.refusal == {"category": None, "explanation": None}


def test_refusal_captures_stop_details_when_present(toolbox):
    class Details:
        category = "cyber"
        explanation = "declined"

    response = Response("refusal", [])
    response.stop_details = Details()
    outcome = loop.run(ScriptedCompleter([response]), toolbox, "briefing")

    assert outcome.refusal == {"category": "cyber", "explanation": "declined"}


# --- error recovery ---


def test_a_refused_path_is_reported_to_the_model_and_the_loop_continues(toolbox):
    """The guardrail test with teeth: rejection is a recoverable turn.

    The model asks for something outside the repo, is told so via an
    is_error tool_result, and gets another turn to correct itself.
    """
    completer = ScriptedCompleter([
        Response("tool_use", [tool_use("read_source", {"file": "../../../etc/passwd"})]),
        Response("tool_use", [
            tool_use("read_source", {"file": "demo-app/app/main.py"}, "toolu_2")
        ]),
        Response("tool_use", [propose(
            root_cause="rc", suspect_commit="s", diff="d", rationale="r"
        )]),
    ])

    outcome = loop.run(completer, toolbox, "briefing")

    rejection = completer.seen[1][-1]["content"][0]
    assert rejection["is_error"] is True
    assert "outside the repository" in rejection["content"]
    assert outcome.status == "fix_proposed", "loop must survive a rejected path"


def test_unknown_tool_is_reported_as_an_error_result(toolbox):
    completer = ScriptedCompleter([
        Response("tool_use", [tool_use("delete_everything", {})]),
        Response("end_turn", [Block(type="text", text="ok")]),
    ])

    loop.run(completer, toolbox, "briefing")

    result = completer.seen[1][-1]["content"][0]
    assert result["is_error"] is True
    assert "Unknown tool" in result["content"]


# --- replay completer ---


def test_replay_completer_yields_recorded_turns_in_order():
    completer = ReplayCompleter([
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "read_logs", "input": {}}
        ]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
    ])

    first = completer([])
    second = completer([])

    assert first.content[0].name == "read_logs"
    assert second.content[0].text == "done"


def test_replay_completer_raises_when_the_recording_runs_out():
    """A stale recording fails loudly instead of looking like the model quitting."""
    completer = ReplayCompleter([
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "read_logs", "input": {}}
        ]},
    ])
    completer([])

    with pytest.raises(TranscriptExhaustedError, match="stale"):
        completer([])


def test_replay_for_kind_returns_none_when_no_transcript_exists(tmp_path):
    assert ReplayCompleter.for_kind("no_such_kind", directory=tmp_path) is None
