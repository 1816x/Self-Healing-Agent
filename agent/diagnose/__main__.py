"""CLI entry point: diagnose one incident, and optionally ship the fix.

    python -m diagnose --offline                 # replay/heuristic, no API key
    python -m diagnose --incident-id 3           # a specific incident, live
    python -m diagnose --record                  # live, and save the transcript
    python -m diagnose --offline --dry-run-pr    # validate, print the PR, send nothing
    python -m diagnose --offline --open-pr       # validate and open a real PR
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from . import client as client_mod
from . import heuristic, loop, patch, pr, replay
from .prompts import incident_briefing
from .store import Incident, SchemaTooOldError, Store
from .tools import Toolbox

REPO_ROOT = Path(__file__).resolve().parents[2]

# (exit code, diagnosis, proposed fix or None). The diagnosis and the
# proposal travel back to main() rather than being re-read from the store,
# so the validation gate operates on exactly what was just recorded.
_Outcome = tuple[int, dict, dict | None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m diagnose",
        description="Diagnose an incident detected by the monitor.",
    )
    parser.add_argument("--db", default="incidents.db", help="SQLite incident store")
    parser.add_argument(
        "--incident-id",
        type=int,
        help="Diagnose this incident. Default: the oldest one still 'detected'.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Never call the API: replay a recorded transcript, else fall back to heuristics.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Save this live session as the replay transcript for its incident kind.",
    )
    parser.add_argument("--model", default=client_mod.DEFAULT_MODEL)
    parser.add_argument(
        "--effort",
        default=client_mod.DEFAULT_EFFORT,
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=12,
        help="Hard cap on model turns before giving up (default 12).",
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository the read-only tools are confined to.",
    )
    parser.add_argument(
        "--metrics-url",
        default="http://127.0.0.1:8000/metrics",
        help="Demo app metrics endpoint.",
    )

    # --- Phase 4: validation and the pull request ---
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the validation gate and stop at 'fix_proposed' (Phase 3 behaviour).",
    )
    parser.add_argument(
        "--open-pr",
        action="store_true",
        help="After a passing validation, push a branch and open a real pull request.",
    )
    parser.add_argument(
        "--dry-run-pr",
        action="store_true",
        help="Print the pull request that --open-pr would create, without pushing or calling the API.",
    )
    parser.add_argument(
        "--base-branch",
        help=(
            "Branch the fix PR targets. Defaults to the branch the injected bug was "
            "published to; see scripts/inject_bug.sh --push."
        ),
    )
    parser.add_argument(
        "--test-command",
        default=" ".join(patch.DEFAULT_TEST_COMMAND),
        help="Command the validation gate runs to decide whether the fix works.",
    )
    parser.add_argument(
        "--test-cwd",
        default=patch.DEFAULT_TEST_CWD,
        help="Directory inside the worktree to run --test-command in.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        store = Store(args.db)
    except SchemaTooOldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError) as exc:
        print(f"error: could not open {args.db}: {exc}", file=sys.stderr)
        return 2

    with store:
        incident = (
            store.claim(args.incident_id) if args.incident_id else store.claim_next_detected()
        )
        if incident is None:
            target = f"incident #{args.incident_id}" if args.incident_id else "any incident"
            print(f"nothing to do: no {target} in 'detected' state.")
            return 0

        print(f"diagnosing {incident.summary_line()}")
        repo_root = Path(args.repo_root)
        toolbox = Toolbox(
            repo_root,
            log_file=repo_root / "demo-app" / "logs" / "demo-app.jsonl",
            metrics_url=args.metrics_url,
        )

        if args.offline:
            exit_code, diagnosis, proposed_fix = _run_offline(store, incident, toolbox, args)
        else:
            exit_code, diagnosis, proposed_fix = _run_live(store, incident, toolbox, args)

        if proposed_fix is None or args.no_validate:
            return exit_code
        return _ship(store, incident, diagnosis, proposed_fix, args)


def _run_offline(store: Store, incident: Incident, toolbox: Toolbox, args) -> _Outcome:
    completer = replay.ReplayCompleter.for_kind(incident.kind)
    if completer is None:
        print(
            f"no recorded transcript for kind '{incident.kind}' — "
            f"falling back to rule-based triage (not a model diagnosis)"
        )
        diagnosis = heuristic.diagnose(incident, toolbox)
        store.write_diagnosis(incident.id, diagnosis)
        _print_summary(diagnosis, status="diagnosed (heuristic)")
        return (0, diagnosis, None)

    if completer.is_recorded:
        print("replaying a recorded model session (offline)")
        source = "replay"
    else:
        # Loud on purpose. A hand-authored transcript exercises the real
        # loop and real tools, but the turns are not model output and must
        # never read as though they were.
        print(
            "replaying a HAND-AUTHORED transcript (offline) — these turns are "
            "scripted, not model output. Run live with --record for a real one."
        )
        source = "replay-scripted"

    try:
        outcome = loop.run(completer, toolbox, incident_briefing(incident), args.max_turns)
    except replay.TranscriptExhaustedError as exc:
        store.mark_failed(incident.id, str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return (1, {}, None)
    return _record_outcome(store, incident, outcome, source=source)


def _run_live(store: Store, incident: Incident, toolbox: Toolbox, args) -> _Outcome:
    completer = client_mod.LiveCompleter(model=args.model, effort=args.effort)
    print(f"calling {args.model} (effort={args.effort})")

    try:
        outcome = loop.run(completer, toolbox, incident_briefing(incident), args.max_turns)
    except Exception as exc:  # noqa: BLE001 — see below
        # Deliberately broad. This incident is already claimed as
        # 'diagnosing'; if anything at all goes wrong we must record a
        # terminal status, or it sits claimed forever and no later run will
        # pick it up. describe_api_error() classifies what it recognizes and
        # labels the rest as unexpected. (Exception, not BaseException, so
        # Ctrl-C still propagates.)
        message = client_mod.describe_api_error(exc)
        store.mark_failed(incident.id, message)
        print(f"error: {message}", file=sys.stderr)
        return (1, {}, None)

    if completer.usage:
        cached = completer.cache_reads()
        total_out = sum(entry["output_tokens"] for entry in completer.usage)
        print(f"usage: {total_out} output tokens, {cached} tokens read from cache")

    recorded = _record_outcome(store, incident, outcome, source="live")

    if args.record and outcome.status == "fix_proposed":
        path = replay.save_transcript(incident.kind, completer.transcript)
        print(f"recorded transcript -> {path}")

    return recorded


def _record_outcome(
    store: Store, incident: Incident, outcome: loop.Outcome, *, source: str
) -> _Outcome:
    """Writes the loop's result to the incident and reports it."""
    diagnosis = {
        "source": source,
        "turns": outcome.turns,
        "tool_calls": outcome.tool_calls,
        "reasoning": outcome.reasoning,
    }

    if outcome.status == "fix_proposed":
        fix = outcome.proposed_fix or {}
        diagnosis["root_cause"] = fix.get("root_cause", "")
        diagnosis["suspect_commit"] = fix.get("suspect_commit", "")
        proposed = {"diff": fix.get("diff", ""), "rationale": fix.get("rationale", "")}
        store.write_proposed_fix(incident.id, diagnosis, proposed)
        _print_summary(diagnosis, status="fix_proposed")
        return (0, diagnosis, proposed)

    if outcome.status == "refused":
        store.mark_refused(incident.id, outcome.refusal or {})
        print("the model declined to diagnose this incident", file=sys.stderr)
        return (1, diagnosis, None)

    if outcome.status == "iteration_cap":
        store.mark_failed(incident.id, f"hit the {outcome.turns}-turn cap without concluding")
        print(f"gave up after {outcome.turns} turns without a conclusion", file=sys.stderr)
        return (1, diagnosis, None)

    # Stopped with prose instead of a proposal — a legitimate outcome when the
    # evidence genuinely doesn't support a conclusion, so it's recorded as a
    # diagnosis rather than a failure.
    diagnosis["root_cause"] = outcome.text
    store.write_diagnosis(incident.id, diagnosis)
    _print_summary(diagnosis, status="diagnosed (no fix proposed)")
    return (0, diagnosis, None)


def _print_summary(diagnosis: dict, *, status: str) -> None:
    print(f"\nstatus: {status}")
    if suspect := diagnosis.get("suspect_commit"):
        print(f"suspect commit: {suspect}")
    if root_cause := diagnosis.get("root_cause"):
        print(f"root cause: {root_cause}")
    if calls := diagnosis.get("tool_calls"):
        print(f"tools used: {', '.join(call['tool'] for call in calls)}")




def _ship(store: Store, incident: Incident, diagnosis: dict, proposed_fix: dict, args) -> int:
    """Validate the proposed diff, and if asked, open the pull request.

    Wrapped in a broad except for the same reason the live loop is: the
    incident is already past 'detected', and an unhandled error here would
    leave it parked in a status no later run picks up. Every path through
    this function ends with the incident in a terminal state.
    """
    repo_root = Path(args.repo_root).resolve()
    base = args.base_branch or _current_branch(repo_root)

    try:
        return _validate_and_open(store, incident, diagnosis, proposed_fix, args, repo_root, base)
    except Exception as exc:  # noqa: BLE001 — an incident must never be left mid-flight
        store.write_validation_failed(incident.id, {"ok": False, "error": str(exc)})
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _validate_and_open(
    store: Store,
    incident: Incident,
    diagnosis: dict,
    proposed_fix: dict,
    args,
    repo_root: Path,
    base: str,
) -> int:
    print(f"\nvalidating the proposed diff against {base} in a throwaway worktree")

    with patch.worktree(repo_root, base_ref="HEAD") as tree:
        result = patch.validate_in(
            tree,
            proposed_fix.get("diff", ""),
            repo_root=repo_root,
            test_command=tuple(args.test_command.split()) if args.test_command else (),
            test_cwd=args.test_cwd,
        )
        print(f"validation: {result.summary()}")

        if not result.ok:
            store.write_validation_failed(incident.id, result.as_record())
            print("status: fix_failed — no pull request opened", file=sys.stderr)
            return 1

        store.write_validated(incident.id, result.as_record())
        print("status: fix_validated")

        if not (args.open_pr or args.dry_run_pr):
            print("(pass --open-pr to open a pull request, or --dry-run-pr to preview one)")
            return 0

        return _open_pull_request(store, incident, diagnosis, proposed_fix, result, args, repo_root, tree, base)


def _open_pull_request(
    store: Store,
    incident: Incident,
    diagnosis: dict,
    proposed_fix: dict,
    validation,
    args,
    repo_root: Path,
    tree: Path,
    base: str,
) -> int:
    repository = pr.resolve_repository(repo_root)
    branch = pr.branch_name(incident.id)
    title = f"fix: {incident.kind} in incident #{incident.id}"
    body = pr.build_body(incident, diagnosis, proposed_fix, validation, base=base)

    if args.dry_run_pr:
        print("\n--- dry run: this pull request would be created ---")
        print(f"repository: {repository}")
        print(f"head:       {branch}")
        print(f"base:       {base}")
        print(f"title:      {title}")
        print(f"\n{body}")
        print("--- nothing was pushed and no API call was made ---")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise pr.PROpenError(
            "no GITHUB_TOKEN in the environment — cannot open a pull request. "
            "Use --dry-run-pr to preview it instead."
        )

    print(f"pushing {branch} and opening a pull request against {base}")
    pr.push_branch(tree, branch, f"{title}\n\n{proposed_fix.get('rationale', '')}".strip())
    opened = pr.create_pull_request(
        repository, title=title, body=body, head=branch, base=base, token=token
    )

    store.write_pr_opened(incident.id, opened.url)
    print(f"status: pr_opened -> {opened.url}")
    return 0


def _current_branch(repo_root: Path) -> str:
    """The branch the bug actually lives on, when no base was given.

    Defaulting to the current branch rather than 'main' is deliberate: the
    injected bug commit is local, and a PR against a base that does not
    contain the bug could not apply, let alone pass CI.
    """
    import subprocess

    completed = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    return completed.stdout.strip() or "main"


if __name__ == "__main__":
    raise SystemExit(main())
