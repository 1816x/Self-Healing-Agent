"""CLI entry point: diagnose one incident.

    python -m diagnose --offline                 # replay/heuristic, no API key
    python -m diagnose --incident-id 3           # a specific incident, live
    python -m diagnose --record                  # live, and save the transcript
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import client as client_mod
from . import heuristic, loop, replay
from .prompts import incident_briefing
from .store import Incident, SchemaTooOldError, Store
from .tools import Toolbox

REPO_ROOT = Path(__file__).resolve().parents[2]


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
            return _run_offline(store, incident, toolbox, args)
        return _run_live(store, incident, toolbox, args)


def _run_offline(store: Store, incident: Incident, toolbox: Toolbox, args) -> int:
    completer = replay.ReplayCompleter.for_kind(incident.kind)
    if completer is None:
        print(
            f"no recorded transcript for kind '{incident.kind}' — "
            f"falling back to rule-based triage (not a model diagnosis)"
        )
        diagnosis = heuristic.diagnose(incident, toolbox)
        store.write_diagnosis(incident.id, diagnosis)
        _print_summary(diagnosis, status="diagnosed (heuristic)")
        return 0

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
        return 1
    return _record_outcome(store, incident, outcome, source=source)


def _run_live(store: Store, incident: Incident, toolbox: Toolbox, args) -> int:
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
        return 1

    if completer.usage:
        cached = completer.cache_reads()
        total_out = sum(entry["output_tokens"] for entry in completer.usage)
        print(f"usage: {total_out} output tokens, {cached} tokens read from cache")

    exit_code = _record_outcome(store, incident, outcome, source="live")

    if args.record and outcome.status == "fix_proposed":
        path = replay.save_transcript(incident.kind, completer.transcript)
        print(f"recorded transcript -> {path}")

    return exit_code


def _record_outcome(store: Store, incident: Incident, outcome: loop.Outcome, *, source: str) -> int:
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
        store.write_proposed_fix(
            incident.id,
            diagnosis,
            {"diff": fix.get("diff", ""), "rationale": fix.get("rationale", "")},
        )
        _print_summary(diagnosis, status="fix_proposed")
        return 0

    if outcome.status == "refused":
        store.mark_refused(incident.id, outcome.refusal or {})
        print("the model declined to diagnose this incident", file=sys.stderr)
        return 1

    if outcome.status == "iteration_cap":
        store.mark_failed(incident.id, f"hit the {outcome.turns}-turn cap without concluding")
        print(f"gave up after {outcome.turns} turns without a conclusion", file=sys.stderr)
        return 1

    # Stopped with prose instead of a proposal — a legitimate outcome when the
    # evidence genuinely doesn't support a conclusion, so it's recorded as a
    # diagnosis rather than a failure.
    diagnosis["root_cause"] = outcome.text
    store.write_diagnosis(incident.id, diagnosis)
    _print_summary(diagnosis, status="diagnosed (no fix proposed)")
    return 0


def _print_summary(diagnosis: dict, *, status: str) -> None:
    print(f"\nstatus: {status}")
    if suspect := diagnosis.get("suspect_commit"):
        print(f"suspect commit: {suspect}")
    if root_cause := diagnosis.get("root_cause"):
        print(f"root cause: {root_cause}")
    if calls := diagnosis.get("tool_calls"):
        print(f"tools used: {', '.join(call['tool'] for call in calls)}")


if __name__ == "__main__":
    raise SystemExit(main())
