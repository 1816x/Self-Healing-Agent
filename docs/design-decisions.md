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

## Phase 2 — detection iteration

### Own Prometheus text-format parser, not `prometheus/common/expfmt`
We control both ends — our own `/metrics` output from Phase 0 — and only
need two metrics (the latency histogram, the cache gauge) out of the
dozen-plus families `prometheus_client` emits (Python GC stats, process
memory, our own counters already covered by the log-based error-rate
detector). Pulling in the official protobuf-based client for that is
heavy; a ~100-line scoped parser, tested against a real captured scrape
including the noise lines to prove filtering rather than assume it, is
lighter and is itself a legitimate parsing-skill talking point — the
same shape of choice as VTA's own tree-sitter parser.

### Histograms are cumulative — detection needs deltas
`demo_app_request_latency_seconds` never resets while the process runs,
so p95 has to come from the *difference* between two consecutive
scrapes' bucket counts, not raw counts. A bucket whose count decreased
between scrapes means the app restarted (Prometheus counters reset to
zero) — that invalidates the interval instead of producing a nonsensical
negative delta, the same "shrink means something structural happened"
pattern as the Phase 1 log tailer's rotation handling.

### Dedup/cooldown lives in the store, not the detector — and needed a real refactor
Each detector still decides *when* a condition is worth firing (its own
window/threshold/breach-count, unchanged in spirit from Phase 1).
`UpsertIncident` merges a fresh firing into the still-open incident with
the same `DedupKey` if it landed within a dedup window of the last
update, instead of inserting a new row. Getting there required
generalizing `detect.Incident` first (`refactor(monitor): generalize
Incident shape`, its own commit) — F1's struct was built entirely around
`ErrorCount`, and Phase 2 needed a shape three different detector kinds
could share. A flat `Metrics map[string]float64` replaced the bespoke
field; `DedupKey` was added separately from `Kind` because `Kind` alone
isn't specific enough once a detector fires per-route (two different
routes breaching latency at once must not collapse into one incident).

Merging **replaces** evidence with the incoming (latest) snapshot rather
than numerically combining old and new `Metrics` — summing makes sense
for `error_count`, not for `p95_ms` (which should reflect the latest
reading, not a running total), and the store has no way to know which
rule applies to which metric. "Latest state plus an occurrence count" is
how most real incident/alerting tools show an ongoing condition anyway,
so this is a reasonable simplification, not a missing feature.

### Found by its own test: `updated_at` mixed wall-clock and event time
The dedup decision compares an incoming incident's `WindowStart`
(event-time, deterministic) against the existing row's `updated_at`. The
first implementation wrote `updated_at` from `time.Now()` — wall clock —
which only happened to "work" in earlier manual testing because the
fake event-time in test fixtures was close to the real clock. A test
that set the incident's event-time days apart from the real system
clock caught it immediately: the merge decision came out wrong in both
directions depending on which way the fake and real clocks diverged.
Fixed by writing `updated_at` from the incident's own `WindowEnd`
instead, consistent with the "detectors run on event-time" rule already
used everywhere else (Phase 1's ErrorRate, this phase's Latency). This
is the same category of bug as F1's `importlib.reload` test failure —
found and fixed before the commit landed, not preserved as a `test:`/
`fix:` pair, but real and worth recording for the same reason.

### Schema evolves via `PRAGMA user_version`
F1 shipped a schema with no `dedup_key`/`updated_at`/`occurrences`.
Rather than assume every `incidents.db` on disk is fresh, `Store.Open`
reads `user_version` and applies whatever migrations are missing, in
order — tested by hand-building a real v1-shaped database (the exact
`CREATE TABLE` and 5-column `INSERT` F1 used) and confirming both the
migration and the pre-existing row survive it. Small, but it's the
honest answer to "how does a file-based store evolve without a
migration framework," and it's the kind of thing that comes up in a
systems-design interview question.

### Latency and memory detectors are scrape-driven, not event-driven — and that's fine
`ErrorRate` accumulates discrete log events within a `time.Duration`
window. `Latency` and `MemorySlope` have no discrete "event" — only
periodic snapshots — so their debounce is a count of consecutive
over-threshold *scrapes* (`MinBreaches`) or a least-squares slope over a
lookback window of gauge readings, not an event-count-within-a-window.
Forcing both shapes to look identical would have meant inventing fake
events; the asymmetry is a consequence of the signal, not an
inconsistency, and is exactly what a debounce mechanism should look
like for a continuously-sampled metric.

### Least squares, not (last − first) ÷ duration, for the memory-slope
A naive two-point comparison is fooled by noise at either endpoint — one
low reading at the start or one high reading at the end can suggest
growth (or hide it) that isn't really there. `TestMemorySlopeNoisyButFlatDoesNotFire`
constructs oscillating data where the raw endpoints (50 → 52) would read
as growth to a two-point check but the actual least-squares trend across
all six points is slightly negative — proving the regression isn't fooled
by exactly that failure mode, not just asserting it in a comment.

### B2 and B3 are realistic regressions, not sleep-for-effect hacks
B2: `/products` starts calling a per-item "live pricing" lookup
(`time.sleep` standing in for a downstream round trip) instead of
returning the static catalog — the textbook N+1 shape (one query became
N) without a real database to N+1 against. B3: `/checkout` starts
caching every result under a fresh UUID "for idempotent retries" and
nothing ever evicts it — a genuinely common leak shape (cache added, TTL
forgotten), not a contrived one. Both ship via the same
write-the-real-change → diff → revert → `scripts/bugs/bN.patch` flow as
B1, with the same innocent-sounding commit messages once injected.

### Live-run margins, chosen deliberately
Detector thresholds are sized with real headroom against the demo's
actual traffic, not hand-tuned after the fact: B2's ~120ms per
`/products` call (3 items × 40ms simulated round trip) clears a 100ms
p95 threshold comfortably; B3's ~2 cache items/sec (one per checkout at
`run_demo.sh`'s 0.5s loop cadence) clears a 0.5/sec growth threshold by
4×. Verified against the real numbers the live gate run produced, not
assumed.

## Phase 3 onward

To be filled in as each phase closes.
