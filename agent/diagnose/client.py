"""The live completer: one Claude API call per loop turn.

API-surface notes, because several of these are things a stale mental
model gets wrong (verified against current docs, not recalled):

- ``thinking`` is ``{"type": "adaptive"}``. The older
  ``{"type": "enabled", "budget_tokens": N}`` form is *removed* on this
  model family and returns a 400.
- ``effort`` lives inside ``output_config``, not at the top level.
- ``temperature`` / ``top_p`` / ``top_k`` are rejected outright. Steering
  is prompt-only.
- ``display: "summarized"`` is opt-in; the default returns thinking blocks
  with empty text, which would leave the dashboard's reasoning panel blank.
"""

from __future__ import annotations

from typing import Any

import anthropic

from .prompts import SYSTEM_PROMPT
from .tools import tool_definitions

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
# Generous: a diagnosis turn carries tool results and adaptive thinking, and
# a truncated turn mid-reasoning is worse than a slightly expensive one.
DEFAULT_MAX_TOKENS = 16_000


class LiveCompleter:
    """Calls the Messages API, recording each raw turn for later replay.

    The recording is why offline mode can be honest: a replayed session is
    a real session that happened, not a hand-written script of what we hope
    the model would do.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any | None = None,
    ):
        # Zero-arg construction resolves credentials from the environment
        # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login`
        # profile) — so an unset API key does not mean "no credentials".
        self._client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.transcript: list[dict[str, Any]] = []
        self.usage: list[dict[str, int]] = []

    def __call__(self, messages: list[dict[str, Any]]) -> Any:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            tools=tool_definitions(),
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            # The system prompt and tool definitions are byte-identical on
            # every turn of the loop, and they render ahead of the messages.
            # Caching that prefix makes turns 2..N read it instead of
            # reprocessing it. Verified via usage.cache_read_input_tokens
            # rather than assumed.
            cache_control={"type": "ephemeral"},
        )

        self.transcript.append(_serialize_response(response))
        if usage := getattr(response, "usage", None):
            self.usage.append(
                {
                    "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                    "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                    "cache_creation_input_tokens": (
                        getattr(usage, "cache_creation_input_tokens", 0) or 0
                    ),
                }
            )
        return response

    def cache_reads(self) -> int:
        """Total tokens served from cache across the session."""
        return sum(entry["cache_read_input_tokens"] for entry in self.usage)


def _serialize_response(response: Any) -> dict[str, Any]:
    """Flattens an SDK response into the plain dict the replay format uses."""
    blocks: list[dict[str, Any]] = []
    for block in getattr(response, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            blocks.append({"type": "text", "text": block.text})
        elif kind == "thinking":
            blocks.append({"type": "thinking", "thinking": getattr(block, "thinking", "")})
        elif kind == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                }
            )
        # Any other block type is intentionally dropped from the *recording*
        # only; the live loop still echoes the real blocks back to the API.
    return {"stop_reason": getattr(response, "stop_reason", None), "content": blocks}


def describe_api_error(exc: Exception) -> str:
    """Turns an SDK exception into a message worth storing on the incident.

    Ordered most-specific first. The distinction that matters operationally
    is retryable (rate limit, overload, connection) versus not (bad request,
    auth, missing model) — a single catch-all would erase it.
    """
    if isinstance(exc, anthropic.NotFoundError):
        return f"model or endpoint not found: {exc}"
    if isinstance(exc, anthropic.AuthenticationError):
        return "authentication failed: no valid API key or profile. Try --offline."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"permission denied for this model: {exc}"
    if isinstance(exc, anthropic.RateLimitError):
        return f"rate limited (retryable): {exc}"
    if isinstance(exc, anthropic.BadRequestError):
        return f"invalid request (not retryable — likely a request-shape bug): {exc}"
    if isinstance(exc, anthropic.APIStatusError):
        retryable = "retryable" if exc.status_code >= 500 else "not retryable"
        return f"API error {exc.status_code} ({retryable}): {exc}"
    if isinstance(exc, anthropic.APIConnectionError):
        return f"connection error (retryable): {exc}"
    return f"unexpected error: {exc!r}"
