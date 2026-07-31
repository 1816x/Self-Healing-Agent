"""The validation gate: does the proposed diff actually work?

Phase 3 ends with a diff the model wrote and nobody checked. This module
is the difference between "the model says this fixes it" and "this was
applied and the tests passed" — the only claim worth putting in a pull
request.

Two decisions worth defending:

**Everything happens in a throwaway ``git worktree``.** Applying a
model-authored patch to the user's checkout would risk their uncommitted
work, and ``docs/design-decisions.md`` already records a session where a
``git reset --hard`` destroyed exactly that. A worktree gives the patch a
real, complete, disposable checkout at the same commit, and the operator's
tree is never touched — not even transiently.

**Tests run before the patch as well as after.** After-green alone proves
only that the diff didn't break anything; it can't distinguish a real fix
from a no-op. Before-red plus after-green is the pair that says the diff
did something. Before-red is recorded as evidence but *not required*,
because B2 and B3 are latency and memory regressions that no unit test
turns red — requiring it would make those two permanently unfixable.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .guardrails import GuardrailError, resolve_within

_GIT_TIMEOUT_SECONDS = 30
# A test suite that hangs must not hang the agent. The demo app's suite
# runs in under a second; a minute is a generous ceiling that still ends.
_TEST_TIMEOUT_SECONDS = 120

DEFAULT_TEST_COMMAND = ("python", "-m", "pytest", "-q")
DEFAULT_TEST_CWD = "demo-app"

# Unified-diff target lines. Only the '+++ b/path' side is authoritative
# for what the patch writes; '--- a/path' is captured too so a rename or a
# deletion can't smuggle in a path that was never checked.
_TARGET_RE = re.compile(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(?P<path>[^\t\n]+)", re.MULTILINE)

# Result of a test run, in the store record.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"


class ValidationError(RuntimeError):
    """The gate could not run at all (bad worktree, missing git)."""


@dataclass(frozen=True)
class ValidationResult:
    """What the gate concluded, and the evidence for it.

    ``ok`` is the only field the caller branches on; everything else exists
    so a failure is diagnosable from the incident record without rerunning
    anything.
    """

    ok: bool
    applied: bool
    reason: str = ""
    recounted: bool = False
    tests_before: str = SKIPPED
    tests_after: str = SKIPPED
    files: tuple[str, ...] = ()
    test_output: str = ""
    proves_a_fix: bool = False

    def as_record(self) -> dict:
        """The JSON blob stored in the incident's `validation` column."""
        return {
            "ok": self.ok,
            "applied": self.applied,
            "reason": self.reason,
            "recounted": self.recounted,
            "tests_before": self.tests_before,
            "tests_after": self.tests_after,
            "files": list(self.files),
            "proves_a_fix": self.proves_a_fix,
        }

    def summary(self) -> str:
        if not self.applied:
            return f"diff did not apply: {self.reason}"
        if not self.ok:
            return f"tests {self.tests_after} after applying: {self.reason}"
        if self.proves_a_fix:
            return "applied cleanly; tests failed before and pass after"
        return "applied cleanly; tests pass (they also passed before)"


@dataclass
class _Run:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


def diff_targets(diff: str) -> tuple[str, ...]:
    """Every repo-relative path the diff claims to touch.

    ``/dev/null`` is dropped: it's how a unified diff spells "this file
    didn't exist", not a path anyone needs to check.
    """
    seen: list[str] = []
    for match in _TARGET_RE.finditer(diff):
        path = match.group("path").strip()
        if path in ("/dev/null", "") or path in seen:
            continue
        seen.append(path)
    return tuple(seen)


def check_paths(repo_root: Path, diff: str) -> tuple[str, ...]:
    """Confirms every path in the diff stays inside the repository.

    Same containment rule the read-only tools use, for the same reason:
    the diff is model-authored input, and a patch that writes outside the
    repo is the one thing this gate must never hand onward. Raises
    GuardrailError on the first path that escapes.
    """
    targets = diff_targets(diff)
    if not targets:
        raise GuardrailError(
            "diff names no files — a unified diff needs '--- a/<path>' and "
            "'+++ b/<path>' header lines"
        )
    for path in targets:
        resolve_within(repo_root, path)
    return targets


@contextmanager
def worktree(repo_root: Path, base_ref: str = "HEAD"):
    """A disposable checkout of `base_ref`, removed on the way out.

    ``--detach`` because nothing here should move a branch, and the
    worktree is deleted either way. Removal is best-effort with
    ``--force``: a validation run that leaves a stale worktree behind would
    make the *next* run fail, so cleanup must not depend on the patch
    having behaved.
    """
    repo_root = repo_root.resolve()
    parent = tempfile.mkdtemp(prefix="diagnose-validate-")
    path = Path(parent) / "wt"
    created = _git(repo_root, ["worktree", "add", "--detach", str(path), base_ref])
    if not created.ok:
        shutil.rmtree(parent, ignore_errors=True)
        raise ValidationError(f"could not create a worktree at {base_ref}: {created.output}")
    try:
        yield path
    finally:
        _git(repo_root, ["worktree", "remove", "--force", str(path)])
        shutil.rmtree(parent, ignore_errors=True)
        # Drops the administrative entry if the directory vanished some
        # other way; harmless when removal already succeeded.
        _git(repo_root, ["worktree", "prune"])


def validate_in(
    tree: Path,
    diff: str,
    *,
    repo_root: Path | None = None,
    test_command: Sequence[str] = DEFAULT_TEST_COMMAND,
    test_cwd: str = DEFAULT_TEST_CWD,
) -> ValidationResult:
    """Applies `diff` inside an existing worktree and runs the tests.

    Separated from `validate` so the PR opener can commit and push from
    the very tree the tests just passed in, rather than applying a
    model-authored patch a second time and hoping for the same result.
    """
    if not diff.strip():
        return ValidationResult(ok=False, applied=False, reason="proposed diff is empty")

    try:
        files = check_paths(repo_root or tree, diff)
    except GuardrailError as exc:
        return ValidationResult(ok=False, applied=False, reason=str(exc))

    patch_file = tree.parent / "proposed.diff"
    # git apply is strict about a missing trailing newline in the last hunk.
    patch_file.write_text(diff if diff.endswith("\n") else diff + "\n", encoding="utf-8")

    before = _run_tests(tree, test_command, test_cwd)

    applied, recounted, apply_error = _apply(tree, patch_file)
    if not applied:
        return ValidationResult(
            ok=False,
            applied=False,
            reason=apply_error,
            tests_before=before[0],
            files=files,
        )

    after = _run_tests(tree, test_command, test_cwd)
    passed = after[0] == PASSED

    return ValidationResult(
        ok=passed,
        applied=True,
        reason="" if passed else _tail(after[1]),
        recounted=recounted,
        tests_before=before[0],
        tests_after=after[0],
        files=files,
        test_output=_tail(after[1]),
        # The pair that distinguishes a real fix from a diff that merely
        # doesn't break anything.
        proves_a_fix=passed and before[0] == FAILED,
    )


def validate(
    repo_root: Path,
    diff: str,
    *,
    base_ref: str = "HEAD",
    test_command: Sequence[str] = DEFAULT_TEST_COMMAND,
    test_cwd: str = DEFAULT_TEST_CWD,
) -> ValidationResult:
    """Full gate: throwaway worktree in, verdict out."""
    with worktree(repo_root, base_ref) as tree:
        return validate_in(
            tree,
            diff,
            repo_root=repo_root,
            test_command=test_command,
            test_cwd=test_cwd,
        )


def _apply(tree: Path, patch_file: Path) -> tuple[bool, bool, str]:
    """Applies the patch, retrying once with --recount.

    ``--recount`` rescues a diff whose hunk headers carry the wrong line
    counts — a common model slip, and a purely clerical one, since the
    content lines still say exactly what to change. It does *not* rescue a
    header with no line range at all (a bare ``@@``); git rejects that as
    "no valid patches in input" either way, which is why the fix for that
    belongs in the propose_fix contract and not here.
    """
    strict = _git(tree, ["apply", "--check", str(patch_file)])
    if strict.ok:
        applied = _git(tree, ["apply", str(patch_file)])
        return (applied.ok, False, "" if applied.ok else applied.output)

    lenient = _git(tree, ["apply", "--check", "--recount", str(patch_file)])
    if lenient.ok:
        applied = _git(tree, ["apply", "--recount", str(patch_file)])
        return (applied.ok, True, "" if applied.ok else applied.output)

    return (False, False, strict.output)


def _run_tests(tree: Path, command: Sequence[str], cwd: str) -> tuple[str, str]:
    """Runs the suite inside the worktree. Returns (status, output)."""
    if not command:
        return (SKIPPED, "no test command configured")

    working_dir = tree / cwd
    if not working_dir.is_dir():
        return (SKIPPED, f"{cwd} does not exist in the worktree")

    try:
        completed = subprocess.run(
            list(command),
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=_TEST_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        return (SKIPPED, f"test command not found: {exc}")
    except subprocess.TimeoutExpired:
        # A hung suite is a failure, not a skip: treating it as "couldn't
        # tell" would let a fix that deadlocks the app pass the gate.
        return (FAILED, f"test command timed out after {_TEST_TIMEOUT_SECONDS}s")

    output = (completed.stdout + "\n" + completed.stderr).strip()
    return (PASSED if completed.returncode == 0 else FAILED, output)


def _git(cwd: Path, argv: list[str]) -> _Run:
    try:
        completed = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _Run(returncode=1, stderr=f"git timed out: git {' '.join(argv)}")
    except FileNotFoundError:
        raise ValidationError("git is not installed or not on PATH") from None
    return _Run(completed.returncode, completed.stdout, completed.stderr)


def _tail(text: str, limit: int = 2_000) -> str:
    """Keeps the end of test output — the summary lines are what matter."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return "... [earlier output trimmed]\n" + text[-limit:]
