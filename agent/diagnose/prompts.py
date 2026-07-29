"""The system prompt and the opening incident briefing.

Kept in its own module because the prompt *is* part of the design, not a
string literal buried in the loop: it's the thing that decides whether
the agent verifies a commit subject or believes it.
"""

from __future__ import annotations

from .store import Incident

SYSTEM_PROMPT = """\
You are an incident diagnosis agent for a small web service. An automated \
monitor has detected a failure and handed it to you. Your job is to find the \
root cause and identify the commit that introduced it.

How to work:

- Investigate before concluding. Read the logs to see what actually failed, \
then find what changed, then read the implicated code to confirm the mechanism.
- Commit subjects lie. A commit titled "perf: precompute lookup table" may be \
the regression. Never name a suspect commit on the strength of its subject \
alone — confirm with git_blame on the failing line and by reading the source.
- Name the mechanism, not the symptom. "checkout returns 500" is the symptom. \
"the price table is keyed by string while validation checks integers, so the \
lookup raises KeyError" is the mechanism. Only the second one tells a human \
what to change.
- You have read-only access to the repository. If a tool refuses a path, the \
path was outside the repository — correct it rather than retrying.
- Prefer a few well-chosen calls over exhaustive exploration. You have a \
limited number of turns.

When you can name both the root cause and the commit responsible, call \
propose_fix with a minimal unified diff. That call records your finding and \
ends your turn. The diff is not applied and no pull request is opened by it — \
a human reviews it first, so be precise rather than defensive: fix the root \
cause and nothing else.

If the evidence genuinely does not support a conclusion, say so plainly in \
text instead of calling propose_fix with a guess."""


def incident_briefing(incident: Incident) -> str:
    """The opening user turn: what the monitor saw, and nothing more.

    Deliberately does not suggest a cause or a file to look at. The gate
    for this phase is that the agent finds the guilty commit itself; a
    briefing that pointed at ``main.py`` would make a passing run
    meaningless.
    """
    evidence = incident.evidence
    lines = [
        f"The monitor detected incident #{incident.id}.",
        "",
        f"Kind: {incident.kind}",
        f"Window: {incident.window_start} to {incident.window_end}",
        f"Times fired: {incident.occurrences}",
    ]

    if summary := evidence.get("summary"):
        lines.append(f"Summary: {summary}")

    if metrics := evidence.get("metrics"):
        rendered = ", ".join(f"{name}={value:g}" for name, value in sorted(metrics.items()))
        lines.append(f"Metrics at detection: {rendered}")

    if routes := evidence.get("routes"):
        rendered = ", ".join(f"{route} ({count})" for route, count in sorted(routes.items()))
        lines.append(f"Affected routes: {rendered}")

    if samples := evidence.get("samples"):
        lines.append("")
        lines.append("Sample log lines captured at detection:")
        lines.extend(f"  {sample}" for sample in samples)

    lines.extend(["", "Diagnose the root cause and identify the commit responsible."])
    return "\n".join(lines)
