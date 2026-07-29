"""The function-calling loop.

Hand-written rather than delegated to the SDK's ``tool_runner`` helper.
That is a deliberate deviation from Anthropic's own default recommendation,
for two reasons specific to this project:

1. The offline replay mode this repo needs for a keyless demo wants a
   single seam — a ``completer`` callable — that can be swapped between a
   live API call, a recorded transcript, and a scripted test double. The
   loop below has exactly one such seam.
2. The repo's stated purpose is a system whose every layer is explainable
   in an interview. A loop you can read top to bottom is worth more here
   than one you configure.

For a production agent with no replay requirement, the tool runner is the
better default and this file would be a liability. See
``docs/design-decisions.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .tools import PROPOSE_FIX, Toolbox


class Completer(Protocol):
    """One model turn.

    Live, replay, and test implementations all satisfy this by returning
    an object with ``.stop_reason`` and ``.content``. The Anthropic SDK's
    response objects already have that shape, so the live path passes them
    through untouched — no adapter layer, and nothing in this loop knows
    whether it's talking to an API.
    """

    def __call__(self, messages: list[dict[str, Any]]) -> Any: ...


@dataclass
class Outcome:
    """What the loop concluded.

    ``status`` is one of: ``fix_proposed`` (the model called propose_fix),
    ``no_conclusion`` (it stopped without one), ``refused`` (the model
    declined), ``iteration_cap`` (it ran out of turns).
    """

    status: str
    text: str = ""
    proposed_fix: dict[str, Any] | None = None
    refusal: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    reasoning: list[str] = field(default_factory=list)


def run(
    completer: Completer,
    toolbox: Toolbox,
    briefing: str,
    max_turns: int = 12,
) -> Outcome:
    """Drives the model until it proposes a fix, stops, or runs out of turns.

    ``max_turns`` is a hard stop, not a suggestion: without it a model that
    keeps calling read_logs would run until the API bill said otherwise.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": briefing}]
    reasoning: list[str] = []

    for turn in range(1, max_turns + 1):
        response = completer(messages)

        # Check stop_reason before touching content. On a refusal the content
        # array is empty or partial, so indexing into it blindly is how this
        # code would break in production rather than in a test.
        if getattr(response, "stop_reason", None) == "refusal":
            return Outcome(
                status="refused",
                refusal=_refusal_detail(response),
                tool_calls=toolbox.calls,
                turns=turn,
                reasoning=reasoning,
            )

        blocks = list(getattr(response, "content", None) or [])
        reasoning.extend(
            block.thinking
            for block in blocks
            if getattr(block, "type", None) == "thinking" and getattr(block, "thinking", "")
        )

        # Append the assistant turn verbatim — including thinking blocks.
        # The API rejects modified thinking blocks, and dropping them can
        # break block ordering, so the whole content array goes back as-is.
        messages.append({"role": "assistant", "content": blocks})

        tool_uses = [block for block in blocks if getattr(block, "type", None) == "tool_use"]
        if not tool_uses:
            return Outcome(
                status="no_conclusion",
                text=_text_of(blocks),
                tool_calls=toolbox.calls,
                turns=turn,
                reasoning=reasoning,
            )

        # propose_fix is terminal. Handle it before executing anything else in
        # the same turn: once the model has concluded, running further reads
        # would spend tokens on results nobody will look at.
        for block in tool_uses:
            if block.name == PROPOSE_FIX:
                return Outcome(
                    status="fix_proposed",
                    text=_text_of(blocks),
                    proposed_fix=dict(block.input),
                    tool_calls=toolbox.calls,
                    turns=turn,
                    reasoning=reasoning,
                )

        # A single turn may contain several tool_use blocks. Every one needs a
        # matching tool_result in a single following user message — splitting
        # them across messages teaches the model to stop calling tools in
        # parallel, and omitting one is an API error.
        results: list[dict[str, Any]] = []
        for block in tool_uses:
            output, is_error = toolbox.dispatch(block.name, dict(block.input))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})

    return Outcome(
        status="iteration_cap",
        tool_calls=toolbox.calls,
        turns=max_turns,
        reasoning=reasoning,
    )


def _text_of(blocks: list[Any]) -> str:
    return "\n".join(
        block.text for block in blocks if getattr(block, "type", None) == "text"
    ).strip()


def _refusal_detail(response: Any) -> dict[str, Any]:
    """Extracts what the API said about a refusal, defensively.

    ``stop_details`` is documented as informational and can be absent or
    null even on a refusal, so nothing here assumes its presence.
    """
    details = getattr(response, "stop_details", None)
    return {
        "category": getattr(details, "category", None),
        "explanation": getattr(details, "explanation", None),
    }
