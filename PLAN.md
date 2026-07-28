# Self-Healing Incident Agent — Workmap

Project #4 of the portfolio plan. Started 2026-07-28.

This file governs the repo. Phases run in order, no skipping. When a phase closes, its checkbox gets marked and `docs/design-decisions.md` gets updated with what actually happened vs. what was planned.

## What this is

An incident-response agent that watches a demo app's logs and metrics, detects failures, diagnoses root cause by giving Claude real tools (read logs, query metrics, `git blame`, read source), proposes a concrete fix as a diff, and opens a pull request with the change and the reasoning behind it. A dashboard shows each incident moving through the pipeline: detected → diagnosing → diagnosed → fix proposed → PR opened.

The category is real and active: HolmesGPT (CNCF Sandbox, ~2.8k★) and automatron (~390★) ship the same idea commercially. This repo is a scoped, from-scratch version of that — no agent framework, every layer explicit and explainable in an interview.

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

One SQLite file (`incidents.db`) is the contract between the three layers. No message broker, no services to babysit.

## Key design decisions (made up front — challenge in review, log in design-decisions.md)

1. **Go for the monitor.** The spec allows Rust, but the observability ecosystem (Prometheus, Docker, Kubernetes) is Go — using it signals ecosystem awareness, and it adds a fourth language to the portfolio. Rust stays the fallback if Go fights back hard in the first week.
2. **Python/FastAPI for the demo app.** The agent's auto-fix writes diffs against this code; Claude patches Python more reliably than any other language, and FastAPI is already proven in the portfolio (Biosignal, EDGE). Keeps the Go surface focused on the monitor, where it earns its place.
3. **Bugs are injected as real commits.** `scripts/inject_bug.sh <id>` applies a prepared patch and commits it with an innocent-looking message (real bugs hide in commits titled "perf: cache price lookups"). This is what makes `git blame` meaningful — the agent finds a genuinely guilty commit, and the fix PR fixes a real change. Most interviewable decision in the repo.
4. **SQLite as the incident store.** Single file shared by Go, Python, and TypeScript. Zero infrastructure. Same choice that worked in Trading and Polymarket.
5. **Offline demo mode.** The full loop runs without API keys using recorded agent transcripts and a heuristic fallback diagnoser — the same move that made VTA demoable by anyone. `--offline` flag from Phase 3 on.
6. **Agent guardrails.** Every tool is read-only except `propose_fix`. A proposed diff must apply cleanly and pass the demo app's tests before a PR opens. Iteration cap on the tool loop. PRs are never auto-merged — a human reviews. Only ever opens PRs against this repo, never third-party repos.

## Bug catalog (demo app)

| ID | Bug | Detected via | Phase |
|----|-----|-------------|-------|
| B1 | Unhandled exception in `/checkout` on a specific input class | Log-based: error-rate spike in sliding window | F1 |
| B2 | Latency regression in hot path (N+1 lookup) | Metric-based: p95 latency threshold | F2 |
| B3 | Unbounded in-memory cache growth | Metric-based: memory slope | F2 (stretch) |

MVP needs two failure types detected; B3 is the stretch third.

## Agent tools (the interviewable core)

| Tool | Access | Purpose |
|------|--------|---------|
| `read_logs(window, filter)` | read | Log excerpts around the anomaly window |
| `get_metrics(name, range)` | read | Time-series slices from the monitor's store |
| `git_log_recent(n)` / `git_blame(file, lines)` | read | Find the suspect commit |
| `read_source(file, range)` | read | Read the implicated code |
| `propose_fix(diff, rationale)` | write (gated) | Terminal tool — unified diff + explanation |

Why these five and not more is a `docs/design-decisions.md` section, not just a code comment.

## Repo layout

```
demo-app/    Python/FastAPI service, structured logs, /metrics
monitor/     Go daemon: detectors, incident writer
agent/       Python: Claude tool loop, diff validation, PR opener
dashboard/   Next.js incident dashboard
scripts/     inject_bug.sh, run_demo.sh
docs/        design-decisions.md, architecture.md
```

## Phases

### F0 — Scaffolding (3-5 commits)
- [ ] Repo layout, `.gitattributes` with linguist-vendored for lockfiles
- [ ] demo-app skeleton: 2 endpoints, structured JSON logs, `/metrics`
- [ ] CI: build + test jobs for Go, Python, TypeScript
- [ ] README skeleton: pitch, architecture diagram, injected-bugs disclaimer
- [ ] `docs/design-decisions.md` created with the six decisions above

**Done when:** demo app runs and emits logs/metrics; CI is green on an empty test suite.

### F1 — Vertical slice, no AI (2-4 commits)
- [ ] Monitor tails demo-app logs, sliding-window error-rate detector
- [ ] `inject_bug.sh b1` → incident row appears in SQLite with alert logged

**Done when:** injecting B1 produces an incident end-to-end with zero AI involved.

### F2 — Detection iteration (8-15 commits, the long block)
- [ ] Metrics scraper + p95 latency detector (B2)
- [ ] Memory-slope detector (B3, stretch)
- [ ] Detector tests on synthetic log/metric fixtures — `test:` then `fix:` commits as fixtures expose real bugs
- [ ] False-positive handling: debounce, cooldown, incident dedup
- [ ] Incident lifecycle states in SQLite

**Done when:** two detectors (three with B3) pass fixture tests; a flapping signal produces one incident, not fifty.

### F3 — Diagnosis agent (3-6 commits)
- [ ] Python agent picks up new incidents, runs Claude function-calling loop
- [ ] The five tools implemented with guardrails; diagnosis written back to incident
- [ ] Offline mode: recorded transcripts + heuristic fallback
- [ ] `docs:` commit explaining tool design and what got rejected

**Done when:** on B1, the agent names the guilty commit and the root cause using at least logs + git blame, online and offline.

### F4 — Auto-fix + PR (3-6 commits)
- [ ] `propose_fix` produces a diff; validation gate: applies cleanly + demo-app tests pass
- [ ] Branch + PR opened via GitHub API with diagnosis narrative in the body
- [ ] Iteration cap and failure handling (diff doesn't apply / tests fail → incident marked `fix_failed`, no PR)

**Done when:** at least one real PR exists on this repo with a functional fix for an injected bug. (MVP gate from the spec.)

### F5 — Dashboard + release (4-6 commits)
- [ ] Next.js dashboard: incident list with pipeline states, detail view with tool-call trace and diff
- [ ] README polish: demo GIF, quickstart, design-decisions summary
- [ ] `chore(release): tag v0.1.0` + GitHub Release with notes

**Done when:** v0.1.0 tagged; a stranger can clone, run `run_demo.sh`, and watch the loop happen offline.

## Definition of done (MVP, from the spec)

1. Monitor detects at least 2 distinct failure types in the demo app.
2. Agent diagnoses root cause using at least 2 tools (logs + git blame minimum).
3. At least one real PR opened with a proposed, functional fix.

## Working agreements (from COMMIT-STRATEGY.md)

- Conventional commits, atomic — one logical change per commit.
- One feature branch + self-PR per phase (F2 may split into two), merged with merge commits, not squash. PR bodies follow the auto-review template.
- No backdating, ever. Real sessions across real days; irregular gaps are fine and expected.
- `docs/design-decisions.md` is a living document: what Claude proposed and was accepted, what was corrected or rejected and why, what Santiago decided alone, and what the first design got wrong.
- GitHub topics on day one: `observability`, `llm-agent`, `sre`, `golang`, `incident-response`.

## Cadence

Spec estimates 4-5 weeks. With AI leverage the build fits in 2-3 weeks of real sessions — but the history must show iteration, not a one-day dump. Target: F0-F1 in week one, F2 across week one/two (it's the credibility block), F3-F4 in week two/three, F5 closes it.
