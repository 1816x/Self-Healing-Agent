# dashboard

Next.js incident dashboard — reads `incidents.db` and shows each incident's
pipeline state, the agent's tool-call trace, the diff it proposed, and what
the validation gate independently checked.

```bash
npm install
npm run dev                  # http://localhost:3000
```

The store defaults to `../incidents.db`. Point it elsewhere with
`INCIDENTS_DB=/path/to/incidents.db npm run dev`. If the file doesn't exist
yet, run `scripts/run_demo.sh` from the repo root and inject a bug.

## It cannot write

The database is opened `readOnly: true`, so a write is rejected by SQLite
itself rather than by convention. The dashboard displays the pipeline; it
never advances it. Re-running an incident is a CLI flag —
`python -m diagnose --resume` — which keeps the one process that can modify
incidents the same one that could always modify them.

Reads open and close around a single query. The store is in rollback-journal
mode, not WAL, and is written by a Go daemon in another process; a long-lived
reader handle is what killed the monitor's startup migration once and wedged
the agent's test suite once. Both are recorded in `docs/design-decisions.md`.

## Layout

| Path | What |
|------|------|
| `lib/db.ts` | The only file that opens SQLite. Read-only, short-lived, asserts schema v4 |
| `lib/incidents.ts` | Row mapper: turns empty strings and JSON blobs into discriminated unions |
| `lib/queries.ts` | The two queries the app makes |
| `app/page.tsx` | Incident list |
| `app/incidents/[id]/page.tsx` | Incident detail |
| `components/` | Pipeline widget, provenance banner, tool trace, diff renderer |

## Tests

```bash
npm test          # node --test, no test framework
npm run typecheck
npm run lint
```

The row mapper carries the weight, so that is what is tested: every
`diagnosis` and `validation` shape the Go and Python writers can produce,
including the ones a demo run never generates — a refusal, a rejected gate, a
verified fix whose pull request failed, and columns that were never written.

## Two things worth knowing before changing it

**Nothing in this schema is SQL `NULL`.** Every column added after schema v1
is `NOT NULL DEFAULT ''`, so "hasn't reached this stage" arrives as the empty
string and `JSON.parse("")` throws. Branch on truthiness, not `!= null`.

**`diagnosis.source` must always be visible.** `replay-scripted` and
`heuristic` drove the real tool loop against the real repository, but no model
produced those turns. The CLI says so loudly and this UI does too — that is a
requirement of the project, not a styling choice.

Go owns every schema change (`monitor/internal/store/store.go`). This app
reads; it never migrates.
