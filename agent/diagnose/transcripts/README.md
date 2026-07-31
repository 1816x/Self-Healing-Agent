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

## The diff has to be real too

A hand-authored transcript is still a demo artifact people read as
representative, so its `propose_fix` diff must be one `git apply` accepts —
`test_every_shipped_transcript_proposes_a_diff_that_actually_applies` in
`agent/tests/test_patch.py` enforces that against a B1-injected checkout.

The first hand-authored transcript failed this: its hunk header was a bare
`@@` with no line ranges. Nothing noticed until Phase 4 tried to apply it,
because until then no code path ever did.

## Recording a real one

```bash
python -m diagnose --db incidents.db --record
```

Overwrites the transcript for that incident's kind with `origin:
"recorded"`. Requires credentials and costs API tokens.
