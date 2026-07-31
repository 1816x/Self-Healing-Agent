"""The tools the diagnosis agent can call.

Five read-only investigation tools plus one gated write. That split is
the design decision worth defending: the agent can look at anything in
the repository and nothing else, and the single tool that produces an
artifact (``propose_fix``) still writes nothing itself — it records a
proposal. Everything downstream of it (apply to a throwaway worktree, run
the tests, open a pull request) happens outside the model's control, in
code the model cannot call, and stops short of merging.

Why these five and not more: each maps to a question a human on-call
engineer actually asks, in the order they ask it. What broke (logs),
how badly (metrics), what changed (git log), who changed this line
(git blame), what does the code say (source). A bash tool would cover
all five and more, but it would also be unauditable — a single opaque
command string instead of five typed, individually-bounded calls.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .guardrails import (
    GuardrailError,
    positive_int,
    read_file_range,
    resolve_within,
    truncate,
)

# The terminal tool: when the model calls this, the loop ends.
PROPOSE_FIX = "propose_fix"

_GIT_TIMEOUT_SECONDS = 15
_SCRAPE_TIMEOUT_SECONDS = 5


def tool_definitions() -> list[dict[str, Any]]:
    """JSON schemas sent to the API.

    ``strict`` + ``additionalProperties: false`` on ``propose_fix`` because
    its output is parsed and stored; a malformed diff field there is worth
    catching at the API boundary rather than three layers down.
    """
    return [
        {
            "name": "read_logs",
            "description": (
                "Read the demo app's structured JSON log lines. Use this first to see "
                "what actually failed. Returns the most recent matching lines."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "enum": ["error", "info", "any"],
                        "description": "Filter by log level. Default 'any'.",
                    },
                    "route": {
                        "type": "string",
                        "description": "Only lines for this route, e.g. '/checkout'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum lines to return (default 50, max 200).",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "get_metrics",
            "description": (
                "Scrape the demo app's current Prometheus metrics. Use this to see "
                "present-tense state: request counts, error counts, latency buckets, "
                "cache size. Returns an error if the app is not running."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name_contains": {
                        "type": "string",
                        "description": "Only metric lines containing this substring.",
                    }
                },
                "required": [],
            },
        },
        {
            "name": "git_log_recent",
            "description": (
                "List recent commits, newest first, as '<short-sha> <subject>'. Use this "
                "to find what changed around the time the incident started. Commit "
                "subjects can be misleading — verify with git_blame and read_source."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many commits (default 10, max 50).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Only commits touching this repo-relative path.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "git_blame",
            "description": (
                "Show which commit last modified each line in a range. This is how you "
                "identify the specific commit responsible for the failing code."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Repo-relative path, e.g. 'demo-app/app/main.py'.",
                    },
                    "start_line": {"type": "integer", "description": "First line (1-based)."},
                    "end_line": {"type": "integer", "description": "Last line, inclusive."},
                },
                "required": ["file"],
            },
        },
        {
            "name": "read_source",
            "description": (
                "Read source code from the repository, with line numbers. Use this to "
                "confirm what the implicated code actually does before concluding."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Repo-relative path."},
                    "start_line": {"type": "integer", "description": "First line (1-based)."},
                    "end_line": {"type": "integer", "description": "Last line, inclusive."},
                },
                "required": ["file"],
            },
        },
        {
            "name": PROPOSE_FIX,
            "description": (
                "Record your final diagnosis and a proposed fix, then stop. Call this "
                "exactly once, when you can name the root cause and the commit that "
                "introduced it. This call does not change the repository: afterwards "
                "the diff is applied to a throwaway checkout and the tests are run, and "
                "only if that passes may a pull request be opened — for a human to "
                "review and merge. Nothing is ever merged automatically."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "root_cause": {
                        "type": "string",
                        "description": "What is actually broken and why, in two or three sentences.",
                    },
                    "suspect_commit": {
                        "type": "string",
                        "description": "Short SHA of the commit that introduced the bug.",
                    },
                    "diff": {
                        "type": "string",
                        "description": (
                            "A unified diff that fixes the root cause, applied with "
                            "`git apply`. Requires '--- a/<path>' and '+++ b/<path>' "
                            "header lines with repository-relative paths, and a full "
                            "hunk header with line ranges such as '@@ -12,7 +12,7 @@' — "
                            "a bare '@@' is rejected. Include three lines of exact "
                            "context around each change."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this fix addresses the root cause rather than the symptom.",
                    },
                },
                "required": ["root_cause", "suspect_commit", "diff", "rationale"],
                "additionalProperties": False,
            },
        },
    ]


class Toolbox:
    """Executes tool calls against a specific repository and demo app.

    Holds the paths and URLs the tools need so the tool functions
    themselves stay free of global state — which is also what makes them
    straightforward to point at a fixture repo in tests.
    """

    def __init__(
        self,
        repo_root: Path,
        log_file: Path | None = None,
        metrics_url: str = "http://127.0.0.1:8000/metrics",
    ):
        self.repo_root = repo_root.resolve()
        self.log_file = log_file or (self.repo_root / "demo-app" / "logs" / "demo-app.jsonl")
        self.metrics_url = metrics_url
        # Every call the model made, in order, for the incident record and
        # the Phase 5 dashboard's trace view.
        self.calls: list[dict[str, Any]] = []

    def dispatch(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Runs one tool call. Returns (result_text, is_error).

        Guardrail rejections and unknown tools come back as errors the model
        can read and recover from, rather than exceptions that kill the loop:
        a model that asked for a path outside the repo should be told so and
        given another turn, not silently dropped.
        """
        handler = {
            "read_logs": self.read_logs,
            "get_metrics": self.get_metrics,
            "git_log_recent": self.git_log_recent,
            "git_blame": self.git_blame,
            "read_source": self.read_source,
        }.get(name)

        if handler is None:
            self.calls.append({"tool": name, "input": arguments, "error": "unknown tool"})
            return (f"Unknown tool {name!r}. Available: {self.available_names()}.", True)

        try:
            result = handler(**arguments)
            is_error = False
        except GuardrailError as exc:
            result, is_error = f"Rejected: {exc}", True
        except TypeError as exc:
            # Wrong/extra argument names for this tool.
            result, is_error = f"Invalid arguments for {name}: {exc}", True

        self.calls.append(
            {
                "tool": name,
                "input": arguments,
                "error": result if is_error else None,
                "output_chars": len(result),
            }
        )
        return result, is_error

    @staticmethod
    def available_names() -> str:
        return ", ".join(
            d["name"] for d in tool_definitions() if d["name"] != PROPOSE_FIX
        )

    # --- the read-only tools ---

    def read_logs(
        self,
        level: str = "any",
        route: str | None = None,
        limit: int | None = None,
    ) -> str:
        count = positive_int(limit, "limit", default=50, maximum=200)
        if level not in ("error", "info", "any"):
            raise GuardrailError(f"level must be one of error/info/any, got {level!r}")

        if not self.log_file.exists():
            raise GuardrailError(
                f"log file {self.log_file} does not exist — is the demo app running?"
            )

        matched: list[str] = []
        malformed = 0
        for raw in self.log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if level != "any" and entry.get("level") != level:
                continue
            if route and entry.get("route") != route:
                continue
            matched.append(raw)

        if not matched:
            hint = f" ({malformed} malformed lines skipped)" if malformed else ""
            return f"No log lines matched level={level} route={route}.{hint}"

        # Most recent last: the model reads top-to-bottom and the newest
        # lines are the ones that matter for a live incident.
        tail = matched[-count:]
        header = f"{len(tail)} of {len(matched)} matching lines (most recent last):"
        return truncate(header + "\n" + "\n".join(tail))

    def get_metrics(self, name_contains: str | None = None) -> str:
        try:
            with urlopen(self.metrics_url, timeout=_SCRAPE_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (URLError, OSError) as exc:
            raise GuardrailError(
                f"could not scrape {self.metrics_url}: {exc}. The demo app may not be running; "
                f"rely on read_logs and the incident evidence instead."
            ) from exc

        lines = [
            line
            for line in body.splitlines()
            # Comments are 60% of the payload and carry no signal the model needs.
            if line and not line.startswith("#")
        ]
        if name_contains:
            lines = [line for line in lines if name_contains in line]
        if not lines:
            return f"No metric lines matched {name_contains!r}."
        return truncate("\n".join(lines))

    def git_log_recent(self, limit: int | None = None, path: str | None = None) -> str:
        count = positive_int(limit, "limit", default=10, maximum=50)
        argv = ["git", "log", f"-{count}", "--format=%h %s"]
        if path:
            # Confine the pathspec, and pass it after `--` so a path that
            # looks like a flag can't turn into one.
            resolve_within(self.repo_root, path)
            argv += ["--", path]
        return truncate(self._git(argv) or "No commits matched.")

    def git_blame(
        self,
        file: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        path = resolve_within(self.repo_root, file)
        if not path.exists():
            raise GuardrailError(f"file not found: {file}")

        # -s keeps the output to sha + line, which is all the model needs to
        # name a suspect; the full porcelain format is mostly author metadata
        # that would just spend tokens.
        argv = ["git", "blame", "-s"]
        if start_line is not None:
            end = end_line if end_line is not None else start_line
            if end < start_line:
                raise GuardrailError(f"end_line {end} is before start_line {start_line}")
            argv += ["-L", f"{start_line},{end}"]
        argv += ["--", file]

        output = self._git(argv)
        return truncate(output or f"git blame produced no output for {file}.")

    def read_source(
        self,
        file: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        return read_file_range(self.repo_root, file, start_line, end_line)

    def _git(self, argv: list[str]) -> str:
        try:
            completed = subprocess.run(
                argv,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GuardrailError(f"git command timed out: {' '.join(argv)}") from exc

        if completed.returncode != 0:
            raise GuardrailError(
                f"git command failed ({' '.join(argv)}): {completed.stderr.strip()}"
            )
        return completed.stdout.strip()
