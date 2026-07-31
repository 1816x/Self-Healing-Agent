# v0.1.0 — the loop is closed

An incident-response agent that watches a demo app, detects failures,
diagnoses root cause with real tools, proposes a fix as a diff, proves the fix
works, and opens a pull request for a human to review.

Five phases, built in order over real sessions. No agent framework — every
layer is explicit and explainable.

## What's in it

**demo-app** — Python/FastAPI service with structured JSON logs and a
Prometheus `/metrics` endpoint. Its failures are injected as real commits with
innocent-looking messages, which is what makes `git blame` meaningful.

**monitor** — Go daemon. Three detectors, all verified live:

| Bug | Regression | Detector |
|-----|-----------|----------|
| `b1` | `/checkout` 500s on every request | `error_rate` (log-based sliding window) |
| `b2` | `/products` goes slow | `latency_p95` (metric-based, threshold + breach count) |
| `b3` | in-memory cache grows unbounded | `memory_growth` (least-squares slope) |

Debounce, cooldown and dedup turn a flapping signal into one incident row.

**agent** — Python, Claude function-calling loop, no SDK tool runner. Five
tools: `read_logs`, `get_metrics`, `git_log_recent`/`git_blame`, `read_source`,
and the gated terminal `propose_fix`. Every tool is read-only except the last,
and paths are confined to the repository. A proposed diff must apply cleanly
and turn the demo app's tests from red to green before a pull request opens.
Nothing is ever merged automatically.

**dashboard** — Next.js, reading the same SQLite file through a read-only
handle. Incident list with pipeline state; detail view with evidence, the
agent's tool-call trace, the proposed diff, and — kept deliberately separate —
what the validation gate independently checked.

One SQLite file is the contract between all four. Go owns every schema change.

## The MVP gate

1. **Two distinct failure types detected** — three, all verified live.
2. **Root cause diagnosed with at least two tools** — four, every run.
3. **At least one real pull request with a functional fix** —
   [PR #6](https://github.com/1816x/Self-Healing-Agent/pull/6): a one-line fix
   for B1, opened by the agent, CI green, **reviewed and merged by a human**.
   `test_checkout_success` was red before the diff and green after it.

The merge is the step the agent deliberately cannot take.

## New in this release (Phase 5)

- **Dashboard**, read-only by construction — SQLite rejects writes on its
  handle rather than the app promising not to.
- **`--resume`** picks up incidents a previous run left stranded: a stored fix
  that never reached the gate, or a claim held by a process that died. A
  resumed fix goes straight to the gate — it does not re-run the model,
  because the diff is already recorded.
- **`run_demo.sh` drives the whole loop.** One command in one terminal,
  `inject_bug.sh` in another, and detection *and* diagnosis both happen with
  no API key and no further commands.
- **Two defects fixed**, both latent until the agent ran automatically for the
  first time: a claimed incident no longer loses its dedup identity (one
  injected bug was producing 28 incidents), and the validation gate now runs
  the tests with the interpreter that launched the agent rather than whatever
  `python` PATH resolves to.

## Known limitations, stated plainly

**Neither outward-facing API call has ever been made for real.** The
environment this was built in has no model credentials, and its GitHub token
is proxied and returns 403 on direct API calls. So `LiveCompleter` has never
talked to the model API, and the pull request opener's own `POST /pulls` has
never been accepted by GitHub — PR #6's final API call came from the session's
GitHub tooling instead. Both were re-checked at the start of Phase 5 rather
than assumed carried forward.

Everything between those two edges is verified against the real thing: real
git, real worktrees, real tests, a real merged pull request, a real browser
against a real database. Closing the gap needs one live run with real
credentials — `--record` for the model half, `--open-pr` from an environment
whose token reaches GitHub for the other.

Smaller ones:

- The offline transcript is hand-authored and labelled `replay-scripted`
  everywhere it surfaces. `--record` on the first live run replaces it.
- The `incidents` table has no indexes. Fine at demo scale; a deliberate
  choice not to spend a schema migration on a dashboard's `ORDER BY`.
- `node:sqlite` is still an experimental Node API, which is why every
  reference to it lives in one file.
- Re-running an incident is a CLI flag, not a dashboard button.

See `docs/design-decisions.md` for why the architecture looks the way it does,
including what got rejected and what the first design got wrong.
