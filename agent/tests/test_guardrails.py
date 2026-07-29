"""Tests for path confinement and argument validation.

These are the tests that matter most in this phase: they define the
agent's security boundary. Each escape technique gets its own case,
because they fail differently — a substring check on ".." stops the
first and none of the rest.
"""

from __future__ import annotations

import pytest

from diagnose.guardrails import (
    MAX_READ_LINES,
    GuardrailError,
    positive_int,
    read_file_range,
    resolve_within,
    truncate,
)


@pytest.fixture
def repo(tmp_path):
    """A small fixture repository with a secret living outside it."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("\n".join(f"line {n}" for n in range(1, 51)))
    (tmp_path / "outside-secret.txt").write_text("SENSITIVE")
    return root


# --- containment ---


def test_allows_a_normal_relative_path(repo):
    resolved = resolve_within(repo, "src/main.py")
    assert resolved == (repo / "src" / "main.py").resolve()


def test_rejects_parent_traversal(repo):
    with pytest.raises(GuardrailError, match="outside the repository"):
        resolve_within(repo, "../outside-secret.txt")


def test_rejects_deep_parent_traversal(repo):
    with pytest.raises(GuardrailError, match="outside the repository"):
        resolve_within(repo, "src/../../outside-secret.txt")


def test_rejects_absolute_path(repo):
    """An absolute path is refused, not silently reinterpreted as relative.

    Quietly rebasing it would hide that the model has the wrong mental
    model of the tool.
    """
    with pytest.raises(GuardrailError, match="absolute"):
        resolve_within(repo, "/etc/passwd")


def test_rejects_absolute_path_inside_the_repo_too(repo):
    """Even a *valid* target is refused when given absolutely — the contract
    is 'repo-relative', and a tool that accepts both shapes invites confusion
    about which root an absolute path is relative to."""
    with pytest.raises(GuardrailError, match="absolute"):
        resolve_within(repo, str(repo / "src" / "main.py"))


def test_rejects_symlink_escaping_the_repo(repo, tmp_path):
    """The case a lexical ".." check cannot catch.

    The path contains no traversal at all; only resolving it reveals that
    it lands outside the repository.
    """
    (repo / "sneaky.txt").symlink_to(tmp_path / "outside-secret.txt")
    with pytest.raises(GuardrailError, match="outside the repository"):
        resolve_within(repo, "sneaky.txt")


def test_rejects_path_through_a_symlinked_directory(repo, tmp_path):
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("SENSITIVE")
    (repo / "link").symlink_to(outside_dir)
    with pytest.raises(GuardrailError, match="outside the repository"):
        resolve_within(repo, "link/secret.txt")


def test_rejects_empty_and_whitespace_paths(repo):
    for candidate in ("", "   "):
        with pytest.raises(GuardrailError, match="empty"):
            resolve_within(repo, candidate)


def test_encoded_traversal_is_not_decoded_into_an_escape(repo):
    """%2e%2e%2f is treated as a literal filename, never decoded.

    Decoding it would be the bug; the correct behavior is that no such
    file exists inside the repo, so it stays contained either way.
    """
    resolved = resolve_within(repo, "%2e%2e%2f%2e%2e%2foutside-secret.txt")
    assert repo.resolve() in resolved.parents


# --- bounded reads ---


def test_reads_a_line_range_with_numbers(repo):
    out = read_file_range(repo, "src/main.py", 3, 5)
    assert "3\tline 3" in out
    assert "5\tline 5" in out
    assert "line 6" not in out


def test_read_caps_the_number_of_lines(repo):
    big = repo / "big.txt"
    big.write_text("\n".join(f"row {n}" for n in range(1, MAX_READ_LINES + 200)))
    out = read_file_range(repo, "big.txt", 1, MAX_READ_LINES + 150)
    assert f"row {MAX_READ_LINES}" in out
    assert f"row {MAX_READ_LINES + 1}" not in out, "line cap must actually bound the read"


def test_read_rejects_a_directory(repo):
    with pytest.raises(GuardrailError, match="directory"):
        read_file_range(repo, "src")


def test_read_rejects_a_missing_file(repo):
    with pytest.raises(GuardrailError, match="not found"):
        read_file_range(repo, "src/nope.py")


def test_read_rejects_inverted_range(repo):
    with pytest.raises(GuardrailError, match="before"):
        read_file_range(repo, "src/main.py", 10, 4)


def test_read_rejects_start_past_end_of_file(repo):
    with pytest.raises(GuardrailError, match="past the end"):
        read_file_range(repo, "src/main.py", 5000)


def test_read_survives_invalid_utf8(repo):
    """A stray byte shouldn't take down a diagnosis."""
    (repo / "binary.txt").write_bytes(b"ok\n\xff\xfe bad bytes\n")
    out = read_file_range(repo, "binary.txt")
    assert "ok" in out


# --- output capping ---


def test_truncate_announces_itself(repo):
    out = truncate("x" * 100, limit=10)
    assert out.startswith("x" * 10)
    assert "truncated" in out, "silent truncation would let the model reason over a partial file"


def test_truncate_leaves_short_text_alone():
    assert truncate("short", limit=100) == "short"


# --- count coercion ---


def test_positive_int_defaults_and_clamps():
    assert positive_int(None, "limit", default=50, maximum=200) == 50
    assert positive_int(10, "limit", default=50, maximum=200) == 10
    assert positive_int(9999, "limit", default=50, maximum=200) == 200


def test_positive_int_rejects_junk_and_zero():
    with pytest.raises(GuardrailError, match="integer"):
        positive_int("abc", "limit", default=5, maximum=10)
    with pytest.raises(GuardrailError, match="at least 1"):
        positive_int(0, "limit", default=5, maximum=10)
