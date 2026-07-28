# Design Decisions

This document exists to prove the point the whole portfolio is built on:
the goal is to show Santiago working *with* Claude, not Claude working
alone. It records what Claude proposed and got accepted as-is, what got
corrected or rejected and why, what Santiago decided unilaterally, and
what the first design got wrong. Updated every phase, not just at the end.

## Phase 0 — the six up-front decisions

These were proposed by Claude during workmap planning, before any code
existed, and **accepted as-is** by Santiago.

### 1. Go for the monitor, not Rust
The spec allowed either. Go is the language of the observability ecosystem
this project imitates (Prometheus, Docker, Kubernetes) — using it signals
ecosystem awareness on top of "can write Go," and it's a fourth language
in the portfolio that isn't already covered elsewhere. Rust was kept as an
explicit fallback if Go's concurrency model fought the log-tailing +
metric-scraping design harder than expected. It didn't — Phase 0's stub
built clean on the first try, no `go vet` issues, so the fallback wasn't
needed.

### 2. Python/FastAPI for the demo app, not Go or TypeScript
The agent's auto-fix step (Phase 4) writes diffs against the demo app's
source. Claude patches Python more reliably than any other language in
this stack, and FastAPI is already proven twice in the portfolio
(Multimodal Biosignal, EDGE), so no new framework risk is introduced here.
This also keeps the Go surface scoped to what it's good at — the monitor —
rather than diluting it across app logic too.

### 3. Bugs injected as real commits, not config flags
Rejected alternative: a `BUG_MODE=b1` environment variable that branches
in code. That would make `git blame` point at a conditional, not at a
guilty change — the least interesting possible answer for the agent to
find. Real commits with innocent-looking messages (e.g. "perf: cache price
lookups") mean `git blame` surfaces an actual suspect and the fix PR
closes an actual regression. This is the single decision most likely to
come up in an interview, because it's the one that makes the diagnosis
step non-trivial rather than scripted.

### 4. SQLite as the shared incident store
Same choice that worked in Trading Microstructure and Polymarket PnL
Audit Log: one file, no broker, no service to keep alive across three
different language runtimes. The alternative (a small HTTP API in front
of the store) was considered and rejected — it would add a fourth
component with no design payoff, just surface area.

### 5. Offline demo mode from Phase 3
The Vulnerability Triage Agent's biggest adoption win was that anyone
could clone it and see the full pipeline run without an API key. Recorded
agent transcripts plus a heuristic fallback diagnoser replicate that here.
This is scoped work, not an afterthought — it ships with the agent code in
Phase 3, not bolted on in Phase 5.

### 6. Guardrails: read-only tools, gated write, validated diff, human-merged PRs
Every tool the agent gets is read-only except `propose_fix`, and even that
one doesn't write to disk or open a PR directly — its output (a diff) has
to apply cleanly and pass the demo app's own test suite before a PR gets
opened. PRs are never auto-merged, and the agent is hard-scoped to open
PRs only against this repo. This is a portfolio project demonstrating
agent tool-use discipline, not a real production auto-remediation system —
the guardrails are the part that shows the difference was understood.

## Phase 0 — one deviation from the original spec, and why

`PLAN.md`'s CI line originally implied a TypeScript job alongside Go and
Python in Phase 0. That got corrected during implementation: the
dashboard doesn't exist yet, and a CI job with no code to build or test
against is a checkbox with nothing behind it. The TypeScript job moves to
Phase 5, when the Next.js app is scaffolded and there's something real for
CI to check. Noted here instead of silently diverging from the plan.

## Phase 0 — one real bug caught by its own test

While writing the JSON-logging smoke test, the first version monkeypatched
the log file path and then called `importlib.reload()` on the logging
module "to be safe." That reload re-executes the module's top level, which
recomputes `LOG_DIR`/`LOG_FILE` from `__file__` again — silently undoing
the monkeypatch it followed. The test failed with the log file missing
from the temp directory it was supposed to be in. Fix: drop the reload;
patching the already-imported module's globals is sufficient on its own.

This one got caught and fixed *before* the commit landed, so it doesn't
show up as a separate `test:`/`fix:` pair in history the way
COMMIT-STRATEGY.md describes for Phase 2's detector work — worth flagging
here since the commit log alone won't show it. Recorded so the reasoning
isn't lost, and as a reminder to let genuine test failures land as their
own commit once Phase 2's iteration starts, instead of quietly fixing
forward.

## Phase 1 — decisions made during the vertical slice

### SQLite driver: `modernc.org/sqlite` over `mattn/go-sqlite3`
The mattn driver is the ecosystem default but needs cgo, which drags a C
toolchain into CI and cross-compilation. The modernc port is pure Go —
`go test ./...` works anywhere Go does. The trade-off (slower than cgo
sqlite under heavy write load) is irrelevant at a few incident rows per
demo run. Side effect worth recording: its module requirements bumped
`go.mod` to Go 1.25.

### CI Go version: `go-version-file` instead of a hardcoded string
The Phase 0 workflow pinned `go-version: "1.24"`. It drifted the moment
the sqlite driver bumped go.mod — the first hardcoded-copy-of-truth in
the repo to break, one phase after it was written. Corrected to
`go-version-file: monitor/go.mod` so there's a single source of truth.
Classic small lesson, kept here because it's the kind of thing
interviewers ask about CI hygiene.

### The B1 bug: validation that passes, lookup that explodes
The injected patch precomputes `PRICE_CENTS_BY_ID` keyed by `str(id)`
("copied from a JSON-keyed cache" is the implied backstory) while the
membership check uses a set of int ids. So validation approves the
request and the price lookup raises `KeyError` — an unhandled 500, not a
clean 404. That distinction is the point: a bug that fails validation
would produce polite 4xx responses and never page anyone; this one
produces exactly the error-spike signature the detector exists for. The
innocent commit message ("perf(checkout): precompute price lookup
table") is deliberate — real regressions don't announce themselves.

### Detector clocks off event timestamps, not wall time
`ErrorRate.Observe` prunes its window using the incoming event's
timestamp. Tests construct synthetic timelines and get fully
deterministic behavior — no sleeps, no clock mocking. The daemon's
behavior is identical since events arrive in near-real-time anyway.

### Empty `main` had to become "main = root commit"
Santiago picked "create an empty main" for the Phase 0 PR base. GitHub
refuses PRs between branches with no common ancestor, so a truly empty
orphan main can't receive one. Adjusted to main starting at the root
commit (the workmap doc, zero code) — closest workable version of the
intent, and all Phase 0 code still went through a reviewable PR (#1).

### Observed in the live run (feeding Phase 2)
- A sustained failure re-fires an incident every N errors — the
  post-fire reset is dedup theater, not real debounce. Phase 2's
  cooldown/dedup work is scoped for exactly this.
- The `request.unhandled_exception` log line carries no `status` field
  (the middleware logs before re-raising), so evidence samples show
  `status=0`. Detection is unaffected (`level=error` matches); cosmetic
  fix can ride along in Phase 2.

## Phase 2 onward

To be filled in as each phase closes.
