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

## Phase 3 — the diagnosis agent

### The thing I got wrong before writing a line: the API had moved

I was about to write `thinking: {"type": "enabled", "budget_tokens": N}`.
That form is **removed** on the current Opus family and returns a 400. I
also would have put `effort` at the top level of the request (it belongs
inside `output_config`) and might well have passed a `temperature` (all
sampling parameters are now rejected outright).

None of that came from carelessness — it came from a model's training
data being a snapshot. The cutoff was January 2026; this was built at the
end of July. Six months is enough for three breaking changes in one
request shape.

So the first action of this phase was reading the current API reference,
not writing code. **The transferable lesson, and the reason this is the
first entry rather than a footnote: for a fast-moving dependency, "I know
this API" is a claim with a shelf life.** Verify the request shape against
current docs at the start of the work, not when a 400 arrives in
production. Everything in `client.py` that looks like trivia — the
`adaptive` thinking form, `output_config.effort`, `display: "summarized"`
— is there because it was checked.

### Manual loop instead of the SDK's tool runner

The Anthropic SDK ships a `tool_runner` helper that drives the
call → execute → feed-back cycle, and **its own guidance is to default to
it**. This project doesn't, which means the burden is on me to justify it
rather than to assume a hand-written loop looks more impressive.

Two reasons specific to this repo:

1. **Offline replay wants exactly one seam.** The demo has to run without
   an API key, which means substituting the model turn. `loop.run()` takes
   a `completer` callable and knows nothing else about where turns come
   from — live, recorded, or scripted in a test all satisfy the same
   protocol. Bending the tool runner's per-turn hooks into that shape
   would have been more code, not less.
2. **The repo's stated purpose is being explainable.** A loop you read top
   to bottom is worth more here than one you configure.

**When this would be the wrong call:** a production agent with no replay
requirement. Then the tool runner is better — it is maintained, it
handles `pause_turn` resumption and compaction, and hand-rolling that is
a liability. Recording the alternative honestly matters more than
defending the choice.

### One protocol, so offline mode can't rot

`Completer` is a `Protocol`, and the replay types (`replay.Block`,
`replay.Response`) use the same attribute names as the SDK's response
objects. So the live path passes SDK objects through untouched — no
adapter layer — and offline mode exercises *the same code path*, not a
parallel one guarded by an `if offline:` branch that only runs in the
demo and quietly rots. The loop genuinely cannot tell which it's talking
to; the tests rely on that, and use the replay types rather than bespoke
fakes for the same reason.

### Go owns all DDL; Python asserts the version

The v2→v3 migration (diagnosis columns) landed in the Go monitor even
though only the Python agent writes those columns. The monitor already
has the `PRAGMA user_version` ladder, and the alternative — both
languages carrying migration statements for one table — is the
arrangement that rots the first time someone edits only one side.

`Store.__init__` asserts `user_version >= 3` and, on failure, names the
monitor as the thing to run. The store tests build the real monitor
binary and let it create the database, so the cross-language schema
contract is a tested property rather than a comment; CI installs Go in
the agent job for that reason, since without it those tests would skip
and a drift would ship green.

### Path confinement is the actual security work

The tool surface is the agent's entire security boundary: whatever those
five functions permit is exactly what a confused or prompt-injected model
can reach. And the threat isn't hypothetical here — the agent reads the
demo app's own log lines by design, which is a channel an instruction
could arrive through.

Blocklisting `..` would have felt like the fix and covered one of five
escape techniques. `resolve_within()` resolves first and *then* checks
containment, which is what catches the other four: a symlink whose path
contains no traversal at all, a symlinked parent directory, an absolute
path, and (trivially) encoded traversal. Each has its own test, because
they fail differently.

Absolute paths are **refused rather than silently rebased**. Quietly
reinterpreting `/etc/passwd` as repo-relative would hide that the model
has the wrong mental model of the tool.

### Five tools, and why not a bash tool

Each maps to a question an on-call engineer actually asks, in the order
they ask it: what broke (`read_logs`), how badly (`get_metrics`), what
changed (`git_log_recent`), who touched this line (`git_blame`), what does
the code say (`read_source`).

A single `bash` tool would cover all five and more, with less code. It was
rejected because it would be **unauditable**: one opaque command string
instead of five typed calls that can each be individually bounded, logged,
and refused. The call trace stored on the incident — which the Phase 5
dashboard renders — only means something because the calls are typed.

### `propose_fix` is terminal, gated, and records nothing to disk

The one tool that produces an artifact neither writes a file nor opens a
pull request. It ends the loop and stores `{root_cause, suspect_commit,
diff, rationale}` on the incident. Phase 4 adds the gate that actually
matters (does the diff apply? do the demo app's tests pass?) before any
PR exists. It's also the only tool declared `strict` with
`additionalProperties: false`, since its output is parsed and stored.

Guardrail rejections and unknown tools come back as `is_error`
tool_results rather than exceptions, so a model that asks for a bad path
gets told and gets another turn. A test asserts the loop survives a
refused path and still reaches a conclusion — the recovery behavior is
the point, not the rejection.

### Refusal is a first-class outcome, not an error

Opus 5 ships elevated cybersecurity safeguards, and this project is
security-adjacent by construction: it analyzes failures, reads source,
and proposes patches. A refusal arrives as **HTTP 200** with
`stop_reason: "refusal"` and empty or partial content — so `content[0]`
on a refused response is exactly how this would break in production
rather than in a test. The loop checks `stop_reason` before touching
content, and the incident gets its own `diagnosis_refused` status:
nothing is broken and retrying the same prompt won't help, so filing it
as a failure would hide *why* a diagnosis is missing.

**Deliberately deferred:** the server-side `fallbacks` beta, which
re-runs a refused request on another model. This project already has a
fallback path (offline heuristics), and putting a beta endpoint in the
demo's default path costs more than it buys here. Worth revisiting if
refusals show up in practice.

### Prompt caching, because the loop re-sends the same prefix every turn

The system prompt and tool definitions are byte-identical on every
iteration and render ahead of the messages, so top-level
`cache_control: {"type": "ephemeral"}` makes turns 2..N read that prefix
instead of reprocessing it. `LiveCompleter` accumulates
`cache_read_input_tokens` and the CLI prints it, so this is a claim the
first live run will either confirm or refute rather than one I get to
assert.

### Offline mode has three tiers, and the labeling is the design

1. **Recorded replay** — turns a real model produced (`--record`).
   Stored as `source: "replay"`.
2. **Hand-authored replay** — turns a human wrote to demonstrate the
   loop. Stored as `source: "replay-scripted"`, warned about on the CLI,
   `origin: hand_authored` in the file.
3. **Heuristic** — rule-based triage when no transcript matches. Says
   `NOT A MODEL DIAGNOSIS` in the root-cause field, carries
   `confidence: low`, and returns `suspect_commit: None` rather than
   inventing one when git history is unreadable.

An absent `origin` field defaults to `hand_authored` — the conservative
direction, so a transcript can only claim to be a real recording by
explicitly saying so.

This tiering exists because the honest failure mode of a keyless demo is
"admits it is guessing," and the dishonest one is "produces
agent-shaped output indistinguishable from a real run." A portfolio
project that fabricates model output and calls it a diagnosis is worse
than one with a smaller demo. Tier 2 exists at all only because the
labeling makes it safe: it drives the real loop over real tools, and
nothing downstream can present it as a model run.

### What Phase 3 did not verify, stated plainly

**No live API call has ever been made through `LiveCompleter`.** The
environment this was built in has no credentials — no key, no token, no
`ant` profile. The request shape was written against current docs and
every loop invariant around it is unit-tested with scripted turns, but
tests passing is not the API accepting the request. The F3 gate says
"online and offline"; only offline is met. That's why PLAN.md marks F3
code-complete rather than closed.

### Two bugs the tests caught, and one process mistake I made

**Bug 1 — leaked SQLite connections under concurrency.** The store tests
started failing intermittently, but only the first one, and only in the
full suite. Cause: `with sqlite3.connect(...)` commits the transaction
but does **not** close the connection. The schema-wait poll leaked one
handle per iteration onto a database the monitor was concurrently
writing, and the lock contention wedged it. Fixed with
`contextlib.closing`; verified across three consecutive full-suite runs
rather than declared fixed after one pass.

**Bug 2 — dead code in `git_blame`.** A leftover `"--line-porcelain" if
False else "-s"` from an edit. Harmless, and exactly the kind of thing
that survives into a portfolio repo and gets asked about in an
interview.

**Process mistake — `git reset --hard` ate uncommitted work.** While
cleaning up an injected bug commit, I ran `git reset --hard HEAD~1` with
uncommitted provenance changes to two tracked files in the working tree.
The reset destroyed them (the new transcript files survived only because
untracked files are left alone). I had to redo all three edits. The habit
that would have prevented it: commit or stash *before* any `reset --hard`,
and never chain it after a compound command that also did real work.
Recorded because the interesting failures in a project like this are
rarely the algorithms.

## Phase 4 — Auto-fix and the pull request

### The bug had to be published, or the MVP gate was unreachable

The spec's third criterion is "at least one real PR with a proposed,
functional fix." Phase 4 started by discovering that the repo as built
could not satisfy it. The injected bug commit was strictly local and
`inject_bug.sh` never pushed, so a fix PR against `main` would revert
code `main` has never contained — it could not apply, and CI could not
judge it. The choice was between a PR that documents a fix and a PR that
*is* one.

`inject_bug.sh --push` publishes the bug to `demo/<bug>-<sha>` and the fix
PR targets that branch. The PR is then genuinely mergeable, CI-verifiable,
and reviewable, while `main` still never carries an injected bug — the
script refuses to publish from `main` at all, and refuses before it
commits anything, so a refusal leaves the tree untouched.

This narrows a stated policy rather than quietly contradicting it: "never
push the injected commit" became "never push it to `main`". Both README
and PLAN.md say the new thing.

### Validate in a worktree, and validate against the *right* tree

The gate applies the model's diff to a throwaway `git worktree` and runs
the demo app's suite there. Two things this buys: the operator's checkout
is never touched (design-decisions already records a session where a
`git reset --hard` ate uncommitted work — a validator that mutates the
tree would be that mistake institutionalised), and the tested tree is a
complete, real checkout rather than a simulation.

The first version cut that worktree from `HEAD`. That was wrong in a way
only the finished artifact revealed: the PR targets a base branch, so
validating against `HEAD` tests one tree while proposing a change to
another, and every commit made in between rides along into the PR. The
first real pull request this pipeline produced carried an unrelated
refactor beside the one-line fix. Now both are the base ref, preferring
`origin/<base>` because that is what GitHub actually diffs against.

The lesson is narrow and worth keeping: the unit tests were green for
this bug the whole time, because they asserted the gate's verdict rather
than the shape of the thing it produced.

### Red before, green after — and admitting when you only have half of it

After-green alone proves the diff didn't break anything; it cannot tell a
real fix from a no-op. Red-before plus green-after can. So the suite runs
twice.

But red-before is recorded, not required. B2 and B3 are latency and memory
regressions that no unit test turns red, and requiring the pair would make
them permanently unfixable. When the suite was already green the gate
still passes and records `proves_a_fix: false` — and the PR body says
plainly that the tests do not witness the regression and reviewer
judgement carries more weight. A body that read identically in both cases
would be overstating what was checked.

### A failed pull request is not a failed fix

The first end-to-end run recorded `fix_failed` on a GitHub 403 —
overwriting a validation record that said "applied cleanly, tests red
before and green after". A network error had erased the one thing the run
had actually established.

Validation failure and PR failure are now separate paths. A PR that can't
be opened leaves the incident `fix_validated` with the error attached,
because that is what happened. `validation` is also its own column rather
than more keys inside `diagnosis`: one is what the model claimed, the
other is what this machine checked, and a dashboard has to be able to tell
a proposal from a verified fix.

### The PR opener's guardrails are about blast radius, not correctness

Everything upstream of it is confined to a checkout nobody else sees; this
is the one step the outside world observes. So: the destination repository
is pinned in code rather than derived from whatever `origin` says, because
the agent reads content it did not write — log lines, commit subjects,
source comments — and the step that writes somewhere public is exactly
what a prompt injection would aim at. Host and repository are checked
independently, so a remote borrowing the local proxy's path layout
(`https://evil.com/git/1816x/...`) is still refused. There is no merge
call in the module and a test asserts its absence.

Loopback hosts are accepted alongside `github.com`, which is a real
concession: this repo is developed in sandboxes whose git goes through a
local proxy, and the first version of the check simply refused to run
here. The slug allowlist is what carries the guarantee; the host check
narrows it.

### What Phase 4 did not verify, stated plainly

**The PR opener's own HTTP request has never been accepted by GitHub.**
This sandbox's `GITHUB_TOKEN` is proxied and returns 403 on direct API
calls, so the agent pushed the branch and built the request, but the final
`POST /pulls` for PR #6 came from the session's GitHub tooling instead.
This is the same shape of gap as Phase 3's unproven `LiveCompleter`, and
it is recorded the same way rather than glossed. The failure path *is*
verified — the agent classified the 403, kept its validation evidence, and
exited non-zero.

### Four defects this phase surfaced, none in the new code's happy path

**1 — The monitor died at startup under a concurrent reader.** It opened
SQLite with no busy timeout, so any other process holding a read lock for
a few milliseconds made the migration's `PRAGMA user_version` fail with
`SQLITE_BUSY`, which `Open()` treats as fatal. The existing mutex never
covered this; it serializes one process's goroutines, and the whole
architecture is one file shared by three languages. It presented as a
flaky test fixture failing about one run in three after the v4 migration
widened the window — and "flaky fixture" is exactly the diagnosis that
would have shipped it. The monitor's own stderr had the answer, in a pipe
nothing was reading.

**2 — The Phase 3 transcript proposed a diff that could never apply.** A
bare `@@` hunk header with no line ranges. It looked fine in the store and
in CLI output for an entire phase, because no code path had ever tried to
apply it. Fixed at the source — the `propose_fix` contract and the prompt
now specify the format — plus a test that applies every shipped
transcript's diff against a B1-injected checkout.

**3 — The validation gate used the wrong base ref.** Covered above.

**4 — `ruff>=0.7` let CI and this machine enforce different rules.** CI
installed 0.16.1, which promoted `ISC004` out of preview; local had
0.15.8, where the rule requires `--preview`. The build went red on code
that had been green in every local run. Both packages now pin
`>=0.16,<0.17`. An unbounded linter is a build that fails on someone
else's schedule.

### The `propose_fix` description had to stop being true, so it was rewritten

It told the model "the diff is NOT applied and no pull request is opened
by this call". Accurate in Phase 3; false the moment the gate existed.
Leaving it would have been the cheap option and a lie to the one reader
who cannot check. It now says the call itself changes nothing, the diff is
then applied to a throwaway checkout and tested, and a PR may follow —
and that nothing is ever merged automatically.

### Known gap

An incident that crashes between `fix_proposed` and the gate cannot be
re-driven: `claim()` only takes incidents in `detected`. This is inherited
from Phase 3, where `fix_proposed` was terminal, and it is not a
regression — but with a Phase 4 pipeline behind it, a resume path is worth
having. Left for Phase 5, where the dashboard will want to trigger reruns
anyway.

## Phase 5 — the dashboard, the resume path, and v0.1.0

### The resume path, and the one thing it must not do

Phase 4 left this gap open above. Closing it was mostly obvious — widen
the claim set, add a lease for claims whose owner died — but one decision
in it was not.

A resumed `fix_proposed` incident **must not re-run the model.** The diff
is already in the row. Re-deriving it would spend a real API call
reproducing work that is sitting in front of us, and would overwrite the
diagnosis that produced the diff with a second, possibly different one —
so the incident's stored reasoning would no longer be the reasoning
behind its stored fix. The resumed run goes straight to the gate. The
test asserts it by making any model or replay turn raise.

Terminal statuses stay terminal under `--resume`. A diff the gate
rejected does not become valid on a second reading, and `mark_refused`
already argues that re-sending the same prompt is pointless. Only
`fix_proposed` — a resting state, not a verdict — is resumable.

`reclaim_stale_diagnosing` is the first code in the project to read
`updated_at` back, which surfaced that the column has two meanings: Go
writes event time, Python writes wall clock. Reading it as wall clock is
sound *only* because Go never writes `diagnosing` — it produces
`detected` and nothing else. That reasoning is now in the docstring,
because the next person to read that column will not get the same warning.

The lease defaults to 15 minutes. The property that matters is not
reclaiming quickly but not reclaiming too early: two agents diagnosing
one incident is exactly what the conditional UPDATE was written to
prevent, and a lease that expires mid-run reintroduces it by the back
door.

### `node:sqlite`, not a native module

The dashboard reads SQLite through Node's standard library rather than
`better-sqlite3`. No native module means no build step in CI and no
prebuilt-binary lottery, and — the deciding reason — its `readOnly: true`
is a real SQLite mode. "The dashboard cannot write to the store" is
enforced by the database instead of by reviewer attention.

The cost is an experimental API and a Node ≥ 22.6 floor. Both are
contained: every `node:sqlite` reference lives in `lib/db.ts`, and the
Node version is pinned in `.nvmrc` the same way Go's comes from `go.mod`.

### The dashboard is read-only, and reruns stayed a CLI flag

The Phase 4 note above guessed the dashboard would want to trigger
reruns. It doesn't. A rerun button needs a write handle, which forfeits
the guarantee above, and a "run the agent" endpoint would put process
spawning in a web app that renders model-authored content — the precise
thing the PR opener's guardrails were written against. `--resume` covers
the same need from the CLI, where the blast radius is a terminal.

Reads open and close around one query. The store is rollback-journal, not
WAL, and this project has twice been bitten by long-lived reader handles:
one killed the monitor's startup migration, one wedged the agent's test
suite. Verified rather than assumed — the monitor ran throughout a
hammering read loop and never logged `SQLITE_BUSY`.

### The row mapper is where this schema's sharp edges get paid for

Two properties make a naive TypeScript mapper wrong. **Nothing is SQL
`NULL`** — every column after v1 is `NOT NULL DEFAULT ''`, so "not
reached this stage" is the empty string and `JSON.parse("")` throws. And
**`diagnosis` has four shapes, `validation` three**: a model result, an
error, a refusal, a heuristic fallback; a gate record, a gate record plus
a PR error, a bare error. Both are discriminated unions, so a component
cannot destructure the wrong arm. Malformed JSON degrades to a
`malformed` arm carrying the raw text rather than throwing — three
languages write this file, and one bad blob must not 500 a page that
would otherwise show six good incidents.

`fix_validated` renders as two different states, because it is two:
with `validation.pr_error` the fix is verified and the pull request
failed; without it, no pull request was attempted. Collapsing them would
undo the Phase 4 decision that a network error is not a failed fix.

### Provenance is a banner, not a badge

`replay-scripted` and `heuristic` drove the real tool loop against the
real repository, but no model produced those turns. The CLI shouts about
it. A badge in the corner of a card would technically disclose the same
fact while letting a scripted transcript read as a model diagnosis on the
way past, so it renders above the root cause where the eye hits it first.
`isModelOutput()` fails closed on an unrecognised source.

### No index migration, and the diff parser is hand-written

The `incidents` table still has zero indexes. Adding a v5 migration to
serve a dashboard's `ORDER BY` at three-digit row counts would spend the
Go-owns-all-DDL rule on nothing. Recorded here instead of implemented.

The diff renderer is about fifteen lines: a unified diff is line-oriented
and classifying a line is a switch on its first character — with
`---`/`+++` checked before `-`/`+`, or every diff opens with a phantom
deleted line. A dependency would have been larger than the parser and
harder to explain.

### Two defects the one-command demo surfaced

Phase 5's gate — a stranger runs one command and watches the whole loop —
meant running the agent automatically for the first time. Both of these
had been latent since Phase 2 and Phase 4 respectively; nothing had ever
exercised the path that revealed them.

**1 — A claimed incident stopped absorbing its own re-firings.** The
monitor's dedup query matched only rows in `detected`. While nothing
moved incidents out of that status, every re-firing merged correctly and
Phase 2's "one incident, not fifty" held. The moment the agent started
claiming incidents within seconds, a still-firing condition found no
`detected` row for its key and opened a *new* incident every detector
cycle — 28 incidents from one injected bug in two minutes. The lookup no
longer filters by status, and a firing whose incident is already claimed
is `Suppressed`: not merged, because rewriting evidence under an agent
mid-diagnosis is why the filter existed; not inserted, because the
condition is already on record and being worked. Staleness moved from
`updated_at` to `window_end`, which only this package writes and which is
always event time — the old code avoided that timebase mix only by never
reading a row the agent had touched.

**2 — The validation gate ran the tests with the wrong interpreter.**
`DEFAULT_TEST_COMMAND` began with a bare `"python"`, resolved from PATH.
In the scripted demo the app and agent live in `demo-app/.venv` while
PATH still points at a system interpreter with no fastapi, so the gate
applied a perfectly good diff and reported `ModuleNotFoundError` — a
verdict about the environment wearing the costume of a verdict about the
diff. It would have marked a working fix `fix_failed`. Now
`sys.executable`: the tests run under the same interpreter as the agent.

Both are the same shape as Phase 4's four: not in the new code's happy
path, and invisible until something actually ran the combination.

### What Phase 5 did not verify, stated plainly

**The two outward-facing calls are still unproven, for the third phase
running.** They were re-checked at the start of this phase, not assumed:
`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are unset in this
environment, and `GET https://api.github.com/repos/1816x/Self-Healing-Agent`
returns 403 through the sandbox's proxy while `/user` returns 200 — so it
is scoping, not a dead token. `LiveCompleter` has still never talked to
the model API and the PR opener's own `POST /pulls` has still never been
accepted by GitHub.

This is recorded the same way Phases 3 and 4 recorded it, and v0.1.0
ships with it stated rather than quietly dropped because the project
reached its last phase. Everything between those two edges is verified
against the real thing: real git, real tests, a real pull request, a real
browser against a real database.


To be filled in as each phase closes.
