"""Input validation for tool arguments that come from the model.

Every value the model passes to a tool is untrusted input. Not because
the model is adversarial, but because the tool surface is the entire
security boundary of this agent: whatever these functions permit is
exactly what a prompt-injected or confused model can reach. A log line
in the demo app's own output is enough to carry an instruction, and the
agent reads those by design.

The rule that matters most: resolve every model-supplied path to its
canonical form and verify it stays inside the repository, *before*
opening anything. Blocklisting ``..`` is not sufficient — symlinks,
absolute paths, and encoded traversal all bypass a substring check.
"""

from __future__ import annotations

from pathlib import Path

# Reading a whole file into a prompt is both a cost and a relevance
# problem, so every read is bounded. The agent can ask for another range
# if it needs more; it cannot ask for a megabyte in one call.
MAX_READ_LINES = 400
MAX_OUTPUT_CHARS = 20_000


class GuardrailError(ValueError):
    """A tool argument was rejected.

    Raised rather than returned so a caller can't accidentally ignore it;
    the tool dispatcher turns it into an ``is_error`` tool_result so the
    model sees the rejection and can correct course.
    """


def resolve_within(repo_root: Path, candidate: str) -> Path:
    """Resolves `candidate` against `repo_root`, refusing anything outside it.

    Uses ``Path.resolve()`` so symlinks are followed *before* the
    containment check — a symlink inside the repo pointing at /etc/passwd
    resolves to /etc/passwd and is rejected, which a lexical check on the
    original string would miss.

    Absolute paths are rejected outright rather than silently reinterpreted:
    an absolute path from the model means it has the wrong mental model of
    the tool, and quietly treating it as relative would hide that.
    """
    if not candidate or not candidate.strip():
        raise GuardrailError("path must not be empty")

    raw = Path(candidate)
    if raw.is_absolute():
        raise GuardrailError(
            f"path must be relative to the repository root, got absolute path {candidate!r}"
        )

    root = repo_root.resolve()
    resolved = (root / raw).resolve()

    if resolved != root and root not in resolved.parents:
        raise GuardrailError(
            f"path {candidate!r} resolves outside the repository and was refused"
        )
    return resolved


def read_file_range(
    repo_root: Path,
    candidate: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Reads a confined, line-bounded, size-capped slice of a text file."""
    path = resolve_within(repo_root, candidate)

    if not path.exists():
        raise GuardrailError(f"file not found: {candidate}")
    if path.is_dir():
        raise GuardrailError(f"{candidate} is a directory, not a file")

    try:
        # errors="replace" keeps a stray non-UTF8 byte from taking down a
        # diagnosis; the agent gets a readable file with a marker in it.
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise GuardrailError(f"could not read {candidate}: {exc}") from exc

    start = 1 if start_line is None else max(1, start_line)
    end = len(lines) if end_line is None else min(len(lines), end_line)
    if start > len(lines):
        raise GuardrailError(
            f"start_line {start} is past the end of {candidate} ({len(lines)} lines)"
        )
    if end < start:
        raise GuardrailError(f"end_line {end} is before start_line {start}")

    end = min(end, start + MAX_READ_LINES - 1)
    numbered = [f"{n}\t{lines[n - 1]}" for n in range(start, end + 1)]
    return truncate(f"{candidate} (lines {start}-{end} of {len(lines)}):\n" + "\n".join(numbered))


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Caps tool output, saying so explicitly when it cuts.

    Silent truncation is worse than none: the model would reason over a
    partial file believing it saw all of it.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated at {limit} characters]"


def positive_int(value: object, name: str, *, default: int, maximum: int) -> int:
    """Coerces and clamps a model-supplied count."""
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise GuardrailError(f"{name} must be an integer, got {value!r}") from exc
    if number < 1:
        raise GuardrailError(f"{name} must be at least 1, got {number}")
    return min(number, maximum)
