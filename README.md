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
(`scripts/inject_bug.sh`, landing in Phase 1) — not accidents. That's what
makes the whole loop reproducible: anyone who clones this repo can trigger
the same incident, watch the same detection, and see the same agent
diagnosis, every time. See the bug catalog in `PLAN.md`.

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

Phases 0-2 done — the demo app, the monitor daemon, and all three detectors
(error-rate, p95 latency, memory growth) work end to end against real
injected bugs. Phase 3 (Claude diagnosis agent) is next. See `PLAN.md` for
the phase plan and `docs/design-decisions.md` for why the architecture looks
the way it does.

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

Undo an injection before trying another: `git reset --hard HEAD~1` (the
injected commit is local-only — `inject_bug.sh` never pushes it).
