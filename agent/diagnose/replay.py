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


#: A transcript produced by an actual model run (LiveCompleter --record).
ORIGIN_RECORDED = "recorded"
#: A transcript written by hand to demonstrate the loop without an API key.
#: Not model output. Tracked separately so it can never be presented as one.
ORIGIN_HAND_AUTHORED = "hand_authored"


class ReplayCompleter:
    """Yields transcript turns in order.

    Carries its ``origin`` so a hand-authored demo transcript can never be
    reported as a real model run. The CLI warns on replay and the stored
    diagnosis gets ``source: "replay-scripted"`` instead of ``"replay"``.
    """

    def __init__(self, turns: list[dict[str, Any]], origin: str = ORIGIN_RECORDED):
        self._turns = turns
        self._index = 0
        self.origin = origin

    @property
    def is_recorded(self) -> bool:
        return self.origin == ORIGIN_RECORDED

    @classmethod
    def for_kind(cls, kind: str, directory: Path | None = None) -> ReplayCompleter | None:
        """Loads the transcript for an incident kind, if one exists."""
        directory = directory or TRANSCRIPT_DIR
        path = directory / f"{kind}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Absent origin means hand-authored: the conservative default, so a
        # transcript can only claim to be a real recording by saying so.
        return cls(payload["turns"], payload.get("origin", ORIGIN_HAND_AUTHORED))

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


def save_transcript(
    kind: str,
    turns: list[dict[str, Any]],
    directory: Path | None = None,
    origin: str = ORIGIN_RECORDED,
) -> Path:
    """Writes a session so offline mode can replay it.

    Called from ``--record`` after a live run, so the default origin is
    ``recorded``: only an actual model session gets written this way.
    """
    directory = directory or TRANSCRIPT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{kind}.json"
    path.write_text(
        json.dumps({"kind": kind, "origin": origin, "turns": turns}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
