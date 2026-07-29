# Replay transcripts

Each `<incident-kind>.json` lets `python -m diagnose --offline` drive the
real tool loop without an API key.

The `origin` field is the only thing that matters when reading one:

| `origin` | What it is | Stored as |
|---|---|---|
| `recorded` | Turns a real model actually produced, written by `--record` after a live run. | `source: "replay"` |
| `hand_authored` | Turns written by a human to demonstrate the loop. **Not model output.** | `source: "replay-scripted"` |

A transcript with no `origin` field is treated as `hand_authored` — a
transcript can only claim to be a real recording by explicitly saying so.

The CLI prints a warning when replaying a hand-authored transcript, and
the label reaches the stored diagnosis, so nothing downstream (including
the Phase 5 dashboard) can present scripted turns as a model run.

## Recording a real one

```bash
python -m diagnose --db incidents.db --record
```

Overwrites the transcript for that incident's kind with `origin:
"recorded"`. Requires credentials and costs API tokens.
