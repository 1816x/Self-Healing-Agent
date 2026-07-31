"""Opening the pull request — the one outward-facing thing this agent does.

Everything upstream of here is confined to a checkout nobody else sees.
This module pushes a branch to a real remote and creates a real pull
request, so its guardrails are about blast radius rather than correctness:

- **An allowlist, not just a check that a remote exists.** The agent reads
  a repository whose contents it did not write — log lines, commit
  subjects, source comments — and a prompt injection's most valuable
  target is precisely the step that writes somewhere public. The owner and
  repo are pinned in code; a remote pointing anywhere else is refused.
- **Never merge.** No auto-merge call, no approving review. A human reads
  it. This is why `propose_fix` can be a "gated write" without the gate
  being a matter of trust.
- **Never push to a protected branch.** The head is always a fresh
  ``fix/incident-<id>`` branch, and the base is whatever branch the bug
  actually lives on — never the head.

Plain ``urllib`` rather than a GitHub SDK: this makes exactly two HTTP
calls, and the agent's only current dependency is ``anthropic``.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_API_ROOT = "https://api.github.com"
_HTTP_TIMEOUT_SECONDS = 30
_GIT_TIMEOUT_SECONDS = 60

# The only repository this agent may ever open a pull request against.
# Deliberately a constant and not configuration: a flag that widens blast
# radius is a flag someone eventually sets.
ALLOWED_REPOSITORIES = frozenset({"1816x/self-healing-agent"})

# Matches the shapes an origin remote actually takes: scp-style SSH,
# HTTPS (with or without embedded credentials), and the path-prefixed form
# a local git proxy uses. Host and slug are captured separately because
# they are checked against different rules.
_REMOTE_RE = re.compile(
    r"^(?:"
    r"(?:git\+)?ssh://(?:[^@/]+@)?(?P<ssh_host>[^/:]+)(?::\d+)?/"
    r"|(?P<scp_user>[^@/\s]+)@(?P<scp_host>[^:/]+):"
    r"|https?://(?:[^@/]+@)?(?P<http_host>[^/:]+)(?::\d+)?/"
    r")"
    r"(?:.*/)??(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

# github.com is the real destination. Loopback is allowed because this
# repo is developed inside sandboxes whose outbound git goes through a
# local proxy, and the remote is literally configured as
# http://127.0.0.1:<port>/git/<owner>/<repo>. That is the environment's
# own plumbing, not a third party — and the guarantee people care about
# is "never write to a different repository", which the slug allowlist
# enforces independently of which host fronts it. Any other host is
# refused, so a remote quietly repointed at an attacker's mirror still
# cannot be pushed to.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class PROpenError(RuntimeError):
    """Opening the pull request failed. Never raised for a rejected fix."""


class RepositoryNotAllowed(PROpenError):
    """The origin remote is not the repository this agent may write to."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


def resolve_repository(repo_root: Path) -> str:
    """Reads `origin` and returns 'owner/repo', refusing anything unlisted.

    Two independent checks, and the slug one is the load-bearing half:
    whatever host fronts the remote, the repository it names must be on
    the allowlist. Matched case-insensitively because GitHub treats owner
    and repo names that way, so a differently-capitalised remote is the
    same repository — not a reason to fail, and not a reason to hand-widen
    the allowlist.
    """
    remote = _git(repo_root, ["remote", "get-url", "origin"]).strip()
    if not remote:
        raise PROpenError("no 'origin' remote is configured")

    match = _REMOTE_RE.match(remote)
    if not match:
        raise RepositoryNotAllowed(f"origin {remote!r} is not a recognised git remote")

    host = (match.group("ssh_host") or match.group("scp_host") or match.group("http_host") or "").lower()
    if host != "github.com" and host not in _LOCAL_HOSTS:
        raise RepositoryNotAllowed(
            f"refusing to act on origin {remote!r}: host {host!r} is neither github.com "
            f"nor a local git proxy"
        )

    slug = f"{match.group('owner')}/{match.group('repo')}"
    if slug.lower() not in ALLOWED_REPOSITORIES:
        raise RepositoryNotAllowed(
            f"refusing to open a pull request against {slug!r}: this agent may only "
            f"write to {', '.join(sorted(ALLOWED_REPOSITORIES))}"
        )
    return slug


def branch_name(incident_id: int) -> str:
    return f"fix/incident-{incident_id}"


def build_body(
    incident: Any,
    diagnosis: dict,
    proposed_fix: dict,
    validation: Any,
    *,
    base: str,
) -> str:
    """The PR description: what broke, why, what was checked, what wasn't.

    Written for a reviewer who did not see the incident. The provenance
    line is not decoration — a diff proposed by a scripted transcript and
    one proposed by a live model must not read identically in a pull
    request, or the offline demo quietly becomes a claim about the model.
    """
    source = diagnosis.get("source", "unknown")
    provenance = {
        "live": "diagnosed by a live model run",
        "replay": "replayed from a recorded model session",
        "replay-scripted": (
            "**replayed from a hand-authored transcript — these turns are scripted, "
            "not model output**"
        ),
        "heuristic": "**produced by rule-based triage, not a model diagnosis**",
    }.get(source, f"source: {source}")

    tools = ", ".join(call["tool"] for call in diagnosis.get("tool_calls", [])) or "none recorded"
    suspect = diagnosis.get("suspect_commit") or "not identified"

    lines = [
        f"Automated fix for incident #{incident.id} (`{incident.kind}`).",
        "",
        "## What the monitor saw",
        "",
        f"- Kind: `{incident.kind}`",
        f"- Window: {incident.window_start} → {incident.window_end}",
        f"- Times fired: {incident.occurrences}",
    ]
    if summary := incident.evidence.get("summary"):
        lines.append(f"- Summary: {summary}")

    lines += [
        "",
        "## Root cause",
        "",
        diagnosis.get("root_cause") or "_not recorded_",
        "",
        f"Suspect commit: `{suspect}`",
        "",
        "## Why this fix",
        "",
        proposed_fix.get("rationale") or "_not recorded_",
        "",
        "## What was verified",
        "",
        f"- Applied to a throwaway checkout of `{base}`: "
        f"{'yes' if validation.applied else 'no'}",
        f"- Demo app tests before the patch: `{validation.tests_before}`",
        f"- Demo app tests after the patch: `{validation.tests_after}`",
    ]
    if validation.proves_a_fix:
        lines.append(
            "- The suite was red before this diff and green after it, so the change is "
            "demonstrably responsible for the difference."
        )
    else:
        lines.append(
            "- The suite passed both before and after, so these tests do **not** "
            "witness the regression. Reviewer judgement carries more weight here."
        )
    if validation.recounted:
        lines.append("- The diff needed `--recount`: its hunk line counts were off.")

    lines += [
        "",
        "## Provenance",
        "",
        f"- Diagnosis {provenance}.",
        f"- Tools used: {tools}",
        f"- Model turns: {diagnosis.get('turns', '?')}",
        "",
        "---",
        "",
        "Opened by the self-healing agent. Nothing here was merged automatically — "
        "this branch exists for a human to review.",
    ]
    return "\n".join(lines)


def push_branch(tree: Path, branch: str, message: str, *, remote: str = "origin") -> str:
    """Commits everything in `tree` and pushes it as `branch`.

    The tree is the one the validation gate just tested in. Re-applying the
    diff to push it would be a second chance to diverge from what was
    actually verified.
    """
    _git(tree, ["add", "-A"])
    _git(
        tree,
        [
            "-c", "user.name=self-healing-agent",
            "-c", "user.email=self-healing-agent@users.noreply.github.com",
            "commit", "-m", message,
        ],
    )
    head = _git(tree, ["rev-parse", "HEAD"]).strip()
    _git(tree, ["push", "--force-with-lease", remote, f"HEAD:refs/heads/{branch}"])
    return head


def create_pull_request(
    repository: str,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
    token: str,
) -> PullRequest:
    """POSTs the pull request. Does not merge it, and never will."""
    if head == base:
        raise PROpenError(f"refusing to open a pull request from {head!r} onto itself")

    payload = json.dumps(
        {"title": title, "body": body, "head": head, "base": base, "maintainer_can_modify": True}
    ).encode("utf-8")

    request = Request(
        f"{_API_ROOT}/repos/{repository}/pulls",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "self-healing-agent",
        },
    )

    try:
        with urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise PROpenError(f"GitHub refused the pull request (HTTP {exc.code}): {detail}") from exc
    except (URLError, OSError) as exc:
        raise PROpenError(f"could not reach the GitHub API: {exc}") from exc

    return PullRequest(number=data["number"], url=data["html_url"])


def _git(cwd: Path, argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PROpenError(f"git timed out: git {' '.join(argv)}") from exc
    except FileNotFoundError as exc:
        raise PROpenError("git is not installed or not on PATH") from exc

    if completed.returncode != 0:
        raise PROpenError(f"git {' '.join(argv)} failed: {completed.stderr.strip()}")
    return completed.stdout
