# Self-Healing Incident Agent

An incident-response agent that watches a demo app's logs and metrics, detects
failures, diagnoses root cause by giving Claude real tools (read logs, query
metrics, `git blame`, read source), proposes a concrete fix as a diff, and
opens a pull request with the change and the reasoning behind it. A dashboard
shows each incident move through the pipeline: detected → diagnosing →
diagnosed → fix proposed → PR opened.

The category is real and active — [HolmesGPT](https://github.com/robusta-dev/holmesgpt)
(CNCF Sandbox, SRE agent) and [automatron](https://github.com/creasty/automatron)
ship the same idea commercially. This repo is a scoped, from-scratch version
of it: no agent framework, every layer explicit and explainable end to end.

> **CI:** ![CI](https://github.com/1816x/Self-Healing-Agent/actions/workflows/ci.yml/badge.svg)

## Important: the bugs are on purpose

The demo app's failures are **injected as real, deliberate commits**
(`scripts/inject_bug.sh`) — not accidents. That's what makes the whole loop
reproducible: anyone who clones this repo can trigger the same incident,
watch the same detection, and see the same agent diagnosis, every time.
See the bug catalog in `PLAN.md`.

If you see a branch named `demo/b1-<sha>`, or a pull request whose base is
one, that is an injected bug and its fix — deliberate, and never merged
into `main`.

## Architecture

```
demo-app (Python/FastAPI, bugs injected as real commits)
        │  structured JSON logs + /metrics (Prometheus format)
        ▼
monitor (Go daemon)
        │  tails logs, scrapes metrics, sliding-window detectors,
        │  debounce/dedup → writes incident to SQLite
        ▼
agent (Python + Claude function calling)
        │  tools: read_logs · get_metrics · git_log/git_blame ·
        │  read_source · propose_fix(diff)
        │  validates diff (applies + tests pass) → opens PR via GitHub API
        ▼
dashboard (Next.js)
           reads SQLite → incident timeline, tool-call trace, diff view
```

One SQLite file (`incidents.db`) is the contract between the three layers —
no message broker, no services to babysit.

## Repo layout

| Path | What |
|------|------|
| `demo-app/` | Python/FastAPI service with structured logs and a `/metrics` endpoint |
| `monitor/` | Go daemon: log/metric detectors, incident writer |
| `agent/` | Python: Claude tool loop, diff validation, PR opener |
| `dashboard/` | Next.js incident dashboard |
| `scripts/` | `inject_bug.sh`, `run_demo.sh` |
| `docs/` | `design-decisions.md`, architecture notes |
| `PLAN.md` | The full phase-by-phase workmap this project follows |

## Status

**The MVP loop is closed.** Phases 0-4 are done: the demo app, the monitor
and all three detectors, the diagnosis agent, and the auto-fix step that
validates a proposed diff and opens a pull request.

[**PR #6**](https://github.com/1816x/Self-Healing-Agent/pull/6) is the
proof — a one-line fix for the B1 bug, opened by the agent, with CI green.
The incident behind it walked `detected → diagnosing → fix_proposed →
fix_validated → pr_opened`, and the demo app's `test_checkout_success` was
red before the diff and green after it.

Two honest caveats, both the same shape: **neither outward-facing API call
has ever been made for real.** The environment this was built in has no
model credentials and a proxied GitHub token that rejects direct API
calls, so `LiveCompleter` has never talked to the model API and the PR
opener's own HTTP request has never been accepted by GitHub. Everything
between those two edges is verified against the real thing — real git,
real tests, a real pull request. Details in `PLAN.md`.

See `docs/design-decisions.md` for why the architecture looks the way it
does, including what got rejected.

## Quickstart

```bash
scripts/run_demo.sh          # terminal 1: demo app + monitor + steady load
scripts/inject_bug.sh b1     # terminal 2: pick one — b1, b2, or b3
```

Within seconds the corresponding detector fires and an incident lands in
`incidents.db`:

| Bug | Regression | Detector |
|-----|-----------|----------|
| `b1` | `/checkout` 500s on every request | `error_rate` (log-based) |
| `b2` | `/products` goes slow (simulated N+1) | `latency_p95` (metric-based) |
| `b3` | in-memory cache grows unbounded | `memory_growth` (metric-based) |

```bash
sqlite3 incidents.db 'SELECT id, kind, status, occurrences, evidence FROM incidents;'
```

Then diagnose it. No API key needed:

```bash
cd agent && pip install -e ".[dev]" && cd ..
python -m diagnose --db incidents.db --offline --repo-root .
```

The agent claims the incident, drives its read-only tools (`read_logs` →
`git_log_recent` → `git_blame` → `read_source`) against this repository,
and records a root cause plus a proposed diff on the incident. Drop
`--offline` to call the API for real.

Offline mode tells you what it is: with no recorded model session it either
replays a hand-authored transcript (labeled `source: "replay-scripted"`) or
falls back to rule-based triage that says `NOT A MODEL DIAGNOSIS`. It never
presents scripted turns as model output — see
`agent/diagnose/transcripts/README.md`.

Then ship the fix. The agent applies the diff to a throwaway `git
worktree`, runs the demo app's tests there, and only then offers to open a
pull request:

```bash
scripts/inject_bug.sh b1 --push          # publish the bug to demo/b1-<sha>
python -m diagnose --db incidents.db --offline --dry-run-pr \
    --base-branch demo/b1-<sha>          # see the PR without sending it
python -m diagnose --db incidents.db --offline --open-pr \
    --base-branch demo/b1-<sha>          # actually open it
```

The gate runs every time; `--open-pr` is opt-in, and nothing is ever
merged automatically. A diff that doesn't apply, or that leaves the tests
red, marks the incident `fix_failed` and opens nothing.

**Why the bug gets its own branch.** A fix PR needs a base that actually
contains the bug — against `main` it would revert code `main` has never
had, so it could neither apply nor pass CI. `--push` publishes the
injected commit to `demo/<bug>-<sha>` and the fix PR targets that.
**`main` never carries an injected bug**, and `inject_bug.sh` refuses to
publish from it.

Undo an injection before trying another: `git reset --hard HEAD~1`, and
`git push origin --delete demo/<bug>-<sha>` if you published it.
