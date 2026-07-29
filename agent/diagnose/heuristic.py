"""Rule-based fallback diagnoser for incidents no recording covers.

This is not the agent and does not pretend to be. It correlates the
incident's evidence with recent commits and produces a low-confidence
starting point, labeled as such. Every diagnosis it emits carries
``source: "heuristic"`` and an explicit ``confidence`` field.

It exists so ``--offline`` degrades honestly on a bug nobody recorded,
instead of either crashing or fabricating model output.
"""

from __future__ import annotations

from typing import Any

from .store import Incident
from .tools import Toolbox

# What each detector kind implies about where to look. Deliberately coarse:
# the honest output of a rule engine is "here is where to start", not a
# root cause.
_KIND_GUIDANCE = {
    "error_rate": (
        "Requests are failing outright. The most recent commit touching the "
        "affected route is the first thing to check."
    ),
    "latency_p95": (
        "Requests still succeed but got materially slower. Look for work added "
        "inside a request path — a call added per item, or a loop that now does IO."
    ),
    "memory_growth": (
        "A collection is growing without bound. Look for something added to a "
        "dict, list, or cache on every request with no eviction."
    ),
}


def diagnose(incident: Incident, toolbox: Toolbox) -> dict[str, Any]:
    """Produces a labeled, low-confidence diagnosis from evidence and git log."""
    routes = sorted((incident.evidence.get("routes") or {}).keys())
    guidance = _KIND_GUIDANCE.get(
        incident.kind, "Unrecognized incident kind; inspect the evidence directly."
    )

    recent_commits, commit_error = _recent_commits(toolbox)

    observations = [guidance]
    if routes:
        observations.append(f"Affected route(s): {', '.join(routes)}.")
    if metrics := incident.evidence.get("metrics"):
        rendered = ", ".join(f"{k}={v:g}" for k, v in sorted(metrics.items()))
        observations.append(f"Metrics at detection: {rendered}.")
    if commit_error:
        observations.append(f"Could not read git history: {commit_error}")
    elif recent_commits:
        observations.append(f"Most recent commit: {recent_commits[0]}")

    return {
        "source": "heuristic",
        "confidence": "low",
        "root_cause": (
            "NOT A MODEL DIAGNOSIS. Rule-based triage only: "
            + " ".join(observations)
        ),
        # Named as a suspect only in the weakest sense — the newest commit,
        # not one blame confirmed. Left as None when history is unreadable
        # rather than guessed at.
        "suspect_commit": recent_commits[0].split()[0] if recent_commits else None,
        "recent_commits": recent_commits,
        "note": (
            "Produced without an API key and without a recorded transcript for "
            "this incident kind. Run the agent live, or record a transcript for "
            f"kind '{incident.kind}', to get a real diagnosis."
        ),
    }


def _recent_commits(toolbox: Toolbox, limit: int = 5) -> tuple[list[str], str | None]:
    output, is_error = toolbox.dispatch("git_log_recent", {"limit": limit})
    if is_error:
        return [], output
    return [line for line in output.splitlines() if line.strip()], None
