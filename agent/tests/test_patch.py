"""Tests for the validation gate.

These run against a real throwaway git repository rather than a mocked
subprocess layer. The gate's whole job is to find out what git and pytest
actually do with a model-authored patch, so a test that stubs them out
would assert the opposite of the thing worth knowing.

The fixture repo carries a trivial pytest suite of its own, so these tests
never need the demo app's dependencies installed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from diagnose import patch
from diagnose.guardrails import GuardrailError

# A suite whose pass/fail is driven by one value in the source under test,
# so a patch can flip it deterministically.
_SOURCE_BROKEN = 'VALUE = "broken"\n'
_SOURCE_FIXED = 'VALUE = "fixed"\n'
_TEST_FILE = '''from src import VALUE


def test_value():
    assert VALUE == "fixed"
'''

# The fix, as a well-formed unified diff.
GOOD_DIFF = """--- a/pkg/src.py
+++ b/pkg/src.py
@@ -1 +1 @@
-VALUE = "broken"
+VALUE = "fixed"
"""

# Same change, hunk header carrying counts that don't match reality — the
# clerical slip --recount exists for.
WRONG_COUNT_DIFF = """--- a/pkg/src.py
+++ b/pkg/src.py
@@ -1,99 +1,99 @@
-VALUE = "broken"
+VALUE = "fixed"
"""

# Same change, hunk header with no line range at all. git rejects this
# outright, with or without --recount.
BARE_HUNK_DIFF = """--- a/pkg/src.py
+++ b/pkg/src.py
@@
-VALUE = "broken"
+VALUE = "fixed"
"""

# Applies cleanly, but doesn't fix anything.
NO_OP_FIX_DIFF = """--- a/pkg/src.py
+++ b/pkg/src.py
@@ -1 +1 @@
-VALUE = "broken"
+VALUE = "still broken"
"""

TEST_COMMAND = ("python", "-m", "pytest", "-q")


def _git(cwd: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=test", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo whose test suite is red until the patch lands."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "src.py").write_text(_SOURCE_BROKEN)
    (root / "pkg" / "test_src.py").write_text(_TEST_FILE)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


def _validate(repo: Path, diff: str) -> patch.ValidationResult:
    return patch.validate(repo, diff, test_command=TEST_COMMAND, test_cwd="pkg")


# --- the happy path ---


def test_a_real_fix_applies_and_turns_the_suite_green(repo):
    result = _validate(repo, GOOD_DIFF)

    assert result.ok
    assert result.applied
    assert result.tests_before == patch.FAILED
    assert result.tests_after == patch.PASSED
    assert result.proves_a_fix, "red-before + green-after is what distinguishes a fix from a no-op"
    assert result.files == ("pkg/src.py",)


def test_validation_never_touches_the_real_working_tree(repo):
    """The patch is applied in a worktree; the operator's checkout is untouched."""
    before = (repo / "pkg" / "src.py").read_text()

    result = _validate(repo, GOOD_DIFF)

    assert result.ok
    assert (repo / "pkg" / "src.py").read_text() == before
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert status.stdout == "", "validation must leave no trace in the real tree"


def test_worktree_is_cleaned_up_even_when_the_patch_fails(repo):
    _validate(repo, BARE_HUNK_DIFF)

    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True
    )
    # Only the main checkout remains.
    assert len(listed.stdout.strip().splitlines()) == 1


# --- diffs that don't apply ---


def test_a_bare_hunk_header_is_rejected_with_gits_own_message(repo):
    """The exact defect the shipped Phase 3 transcript had.

    --recount does not rescue this: a header with no line range gives git
    nothing to recount. The fix belongs in the propose_fix contract, so
    this test pins the behaviour rather than working around it.
    """
    result = _validate(repo, BARE_HUNK_DIFF)

    assert not result.ok
    assert not result.applied
    assert "no valid patches" in result.reason.lower()


def test_wrong_hunk_counts_are_rescued_by_recount(repo):
    result = _validate(repo, WRONG_COUNT_DIFF)

    assert result.ok
    assert result.recounted, "a clerical miscount should not sink an otherwise correct fix"


def test_an_empty_diff_fails_without_running_anything(repo):
    result = _validate(repo, "   \n")

    assert not result.ok
    assert not result.applied
    assert "empty" in result.reason


def test_a_diff_naming_no_files_is_rejected(repo):
    result = _validate(repo, "just some prose, not a diff at all\n")

    assert not result.ok
    assert not result.applied
    assert "names no files" in result.reason


def test_a_diff_that_escapes_the_repository_is_refused(repo):
    """Containment is the same rule the read-only tools use.

    A patch is the one model-authored artifact that writes, so a path
    outside the repo must be refused before anything is applied — not
    caught afterwards.
    """
    escaping = """--- a/../../etc/passwd
+++ b/../../etc/passwd
@@ -1 +1 @@
-x
+y
"""
    result = _validate(repo, escaping)

    assert not result.ok
    assert not result.applied
    assert "outside the repository" in result.reason


def test_check_paths_raises_for_an_absolute_path():
    with pytest.raises(GuardrailError):
        patch.check_paths(Path("/tmp"), "--- a/etc/passwd\n+++ /etc/passwd\n@@ -1 +1 @@\n-x\n+y\n")


# --- diffs that apply but don't fix ---


def test_a_diff_that_applies_but_leaves_tests_red_fails_the_gate(repo):
    result = _validate(repo, NO_OP_FIX_DIFF)

    assert result.applied, "it applied cleanly — that alone is not enough"
    assert not result.ok
    assert result.tests_after == patch.FAILED
    assert result.reason, "pytest's own output is kept so the failure is diagnosable"


def test_green_before_and_after_passes_but_does_not_claim_to_prove_a_fix(repo):
    """The B2/B3 case: no unit test catches a latency or memory regression.

    Requiring red-before would make those permanently unfixable, so the
    gate passes — but records that it did not witness a behaviour change,
    rather than overstating what it verified.
    """
    # Make the suite green first, so the diff lands on an already-passing tree.
    (repo / "pkg" / "src.py").write_text(_SOURCE_FIXED)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "green")

    cosmetic = """--- a/pkg/src.py
+++ b/pkg/src.py
@@ -1 +1 @@
-VALUE = "fixed"
+VALUE = "fixed"  # clarify intent
"""
    result = _validate(repo, cosmetic)

    assert result.ok
    assert result.tests_before == patch.PASSED
    assert result.tests_after == patch.PASSED
    assert not result.proves_a_fix
    assert "also passed before" in result.summary()


# --- plumbing ---


def test_diff_targets_ignores_dev_null_and_deduplicates():
    created = """--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1 @@
+x = 1
"""
    assert patch.diff_targets(created) == ("pkg/new.py",)


def test_as_record_is_json_ready_and_keeps_the_evidence(repo):
    record = _validate(repo, GOOD_DIFF).as_record()

    assert record["ok"] is True
    assert record["tests_before"] == patch.FAILED
    assert record["tests_after"] == patch.PASSED
    assert record["files"] == ["pkg/src.py"]
    assert isinstance(record["proves_a_fix"], bool)


def test_missing_test_command_skips_rather_than_silently_passing(repo):
    result = patch.validate(
        repo, GOOD_DIFF, test_command=("definitely-not-a-real-command",), test_cwd="pkg"
    )

    assert result.tests_after == patch.SKIPPED
    assert not result.ok, "a suite that never ran must not count as a pass"


def test_a_missing_test_directory_skips(repo):
    result = patch.validate(repo, GOOD_DIFF, test_command=TEST_COMMAND, test_cwd="nope")

    assert result.tests_after == patch.SKIPPED
    assert not result.ok


def test_validate_in_reuses_a_worktree_the_caller_owns(repo):
    """The PR opener commits from the tree the tests just passed in.

    Applying the patch a second time to push it would be a second chance
    to diverge from what was validated.
    """
    with patch.worktree(repo) as tree:
        result = patch.validate_in(
            tree, GOOD_DIFF, repo_root=repo, test_command=TEST_COMMAND, test_cwd="pkg"
        )
        assert result.ok
        # The fix is present in the tree, ready to be committed and pushed.
        assert (tree / "pkg" / "src.py").read_text() == _SOURCE_FIXED


# --- the shipped transcripts ---


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _shipped_fix_diffs() -> list[tuple[str, str]]:
    """(kind, diff) for every propose_fix call in a shipped transcript."""
    import json

    from diagnose.replay import TRANSCRIPT_DIR

    found = []
    for path in sorted(TRANSCRIPT_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        for turn in data.get("turns", []):
            for block in turn.get("content", []):
                if block.get("name") == "propose_fix":
                    found.append((data.get("kind", path.stem), block["input"]["diff"]))
    return found


def test_every_shipped_transcript_proposes_a_diff_that_actually_applies():
    """The defect this gate was written to catch, pinned as a test.

    The Phase 3 transcript shipped a diff with a bare `@@` hunk header. It
    looked fine in the store and in CLI output, and would have failed the
    moment anything tried to apply it. A hand-authored transcript is still
    a demo artifact people will read as representative, so it has to clear
    the same bar a live diff does.

    Applies against a B1-injected checkout, since that is the state the
    transcript diagnoses. Tests are deliberately not run here — that would
    need the demo app's dependencies in the agent CI job, and `applied` is
    the property this test is about.
    """
    diffs = _shipped_fix_diffs()
    assert diffs, "expected at least one shipped transcript with a propose_fix"

    root = _repo_root()
    if not (root / "scripts" / "bugs" / "b1.patch").exists():
        pytest.skip("running outside a full checkout")

    b1 = str(root / "scripts" / "bugs" / "b1.patch")
    for kind, diff in diffs:
        with patch.worktree(root) as tree:
            if subprocess.run(
                ["git", "apply", "--check", b1], cwd=tree, capture_output=True, check=False
            ).returncode == 0:
                subprocess.run(["git", "apply", b1], cwd=tree, check=True, capture_output=True)
            elif subprocess.run(
                ["git", "apply", "--reverse", "--check", b1], cwd=tree, capture_output=True, check=False
            ).returncode != 0:
                pytest.skip("HEAD is neither clean nor B1-injected; cannot stage the transcript's state")
            # else: HEAD already carries B1 — running the demo leaves it
            # committed, and a developer who then runs the tests should not
            # see a spurious failure.

            result = patch.validate_in(tree, diff, repo_root=root, test_command=())
            assert result.applied, f"transcript {kind!r} proposes a diff git cannot apply: {result.reason}"
