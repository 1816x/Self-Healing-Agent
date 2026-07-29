"""Offline mode: replay a recorded session, or fall back to a heuristic.

Two layers, and the distinction between them is load-bearing:

- **Replay** returns the turns a real model actually produced against this
  repository, recorded by ``LiveCompleter``. Deterministic, needs no API
  key, and shows a genuine tool-call sequence.
- **Heuristic** is a rule-based diagnoser for incidents no recording
  covers. It is *not* the agent, and it says so — every diagnosis carries
  ``source: "heuristic"`` so nothing downstream can present a canned
  answer as a model run.

Making that second layer clearly-labeled rather than silently
agent-shaped is the whole point. A demo that fabricates model output and
calls it a diagnosis is worse than one that admits it is guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRANSCRIPT_DIR = Path(__file__).resolve().parent / "transcripts"


@dataclass
class Block:
    """A content block with the same attribute names the SDK uses.

    Structural typing means the loop cannot tell these from real SDK
    objects, so offline mode exercises the same code path as live — no
    separate branch that could rot untested.
    """

    type: str
    text: str = ""
    thinking: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] | None = None


@dataclass
class Response:
    stop_reason: str | None
    content: list[Block]
    stop_details: Any = None


class TranscriptExhaustedError(RuntimeError):
    """The loop asked for more turns than the recording contains.

    Signals that the recording no longer matches the code under test —
    surfaced loudly rather than papered over with an empty turn, which
    would look like the model giving up.
    """


class ReplayCompleter:
    """Yields recorded turns in order."""

    def __init__(self, turns: list[dict[str, Any]]):
        self._turns = turns
        self._index = 0

    @classmethod
    def for_kind(cls, kind: str, directory: Path | None = None) -> ReplayCompleter | None:
        """Loads the transcript recorded for an incident kind, if one exists."""
        directory = directory or TRANSCRIPT_DIR
        path = directory / f"{kind}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(payload["turns"])

    def __call__(self, messages: list[dict[str, Any]]) -> Response:
        if self._index >= len(self._turns):
            raise TranscriptExhaustedError(
                f"recorded transcript has {len(self._turns)} turns; the loop asked for "
                f"{self._index + 1}. The recording is stale — re-record it against "
                f"current code."
            )
        turn = self._turns[self._index]
        self._index += 1
        return Response(
            stop_reason=turn.get("stop_reason"),
            content=[_block(raw) for raw in turn.get("content", [])],
        )


def _block(raw: dict[str, Any]) -> Block:
    return Block(
        type=raw["type"],
        text=raw.get("text", ""),
        thinking=raw.get("thinking", ""),
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        input=raw.get("input") or {},
    )


def save_transcript(kind: str, turns: list[dict[str, Any]], directory: Path | None = None) -> Path:
    """Writes a recorded session so offline mode can replay it."""
    directory = directory or TRANSCRIPT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{kind}.json"
    path.write_text(
        json.dumps({"kind": kind, "turns": turns}, indent=2) + "\n", encoding="utf-8"
    )
    return path
