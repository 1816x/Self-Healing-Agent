import type { Incident } from "@/lib/incidents.ts";

type Tone = "idle" | "active" | "good" | "bad" | "warn" | "shipped";

const TONES: Record<string, Tone> = {
  detected: "idle",
  diagnosing: "active",
  diagnosed: "good",
  fix_proposed: "active",
  diagnosis_failed: "bad",
  diagnosis_refused: "warn",
  fix_validated: "good",
  fix_failed: "bad",
  pr_opened: "shipped",
};

/**
 * `fix_validated` is the one status whose tone depends on more than the status.
 * A verified fix whose pull request failed is not the same green as one waiting
 * to be shipped, and the store keeps that distinction in `validation.pr_error`.
 */
export function toneFor(incident: Incident): Tone {
  if (
    incident.status === "fix_validated" &&
    incident.validation.kind === "record" &&
    incident.validation.prError
  ) {
    return "warn";
  }
  return TONES[incident.status] ?? "idle";
}

export function StatusBadge({ incident }: { incident: Incident }) {
  return <span className={`badge ${toneFor(incident)}`}>{incident.status}</span>;
}
