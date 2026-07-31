import type { Diagnosis } from "@/lib/incidents.ts";

/**
 * Says where a diagnosis came from, above the diagnosis itself.
 *
 * This is a requirement, not decoration. `replay-scripted` is a hand-authored
 * transcript and `heuristic` is rule-based triage — both exercise the real tool
 * loop against the real repository, neither is model output, and the CLI prints
 * a loud warning for exactly this reason. A badge tucked in a corner would not
 * carry that, so it renders as a banner the eye hits before the root cause.
 */
const NOTES: Record<string, { title: string; body: string; tone: string }> = {
  live: {
    title: "model diagnosis",
    body: "These turns came from a live model call against the real repository.",
    tone: "model",
  },
  replay: {
    title: "recorded model session",
    body: "A replay of a real model call, recorded with --record. The turns are model output.",
    tone: "model",
  },
  "replay-scripted": {
    title: "hand-authored transcript — not model output",
    body:
      "These turns are scripted. They drove the real tool loop against the real " +
      "repository, but no model produced them. Run live with --record to replace this.",
    tone: "scripted",
  },
  heuristic: {
    title: "rule-based triage — not a model diagnosis",
    body:
      "No transcript existed for this incident kind, so the agent fell back to " +
      "rules over recent commits. Nothing here was reasoned about.",
    tone: "scripted",
  },
};

export function Provenance({ diagnosis }: { diagnosis: Diagnosis }) {
  if (diagnosis.kind !== "model") return null;
  const note = NOTES[diagnosis.source];
  if (!note) return null;

  return (
    <div className={`provenance ${note.tone}`}>
      <span aria-hidden="true">{note.tone === "scripted" ? "▲" : "●"}</span>
      <span>
        <strong>{note.title}</strong>
        <p>{note.body}</p>
      </span>
    </div>
  );
}
