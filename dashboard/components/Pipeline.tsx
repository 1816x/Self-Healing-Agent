import { PIPELINE, type Incident, type IncidentStatus } from "@/lib/incidents.ts";

/**
 * Where an incident stopped, along the happy path.
 *
 * The store has nine statuses but only five of them are stages; the other four
 * are ways of stopping. So a terminal status is drawn *at the stage it stopped
 * at* rather than as a sixth box — `fix_failed` is the gate rejecting a fix, and
 * showing it anywhere other than at the validation step would misdescribe it.
 */
const STOPS_AT: Record<string, IncidentStatus> = {
  diagnosed: "diagnosing",
  diagnosis_failed: "diagnosing",
  diagnosis_refused: "diagnosing",
  fix_failed: "fix_validated",
};

const LABELS: Record<IncidentStatus, string> = {
  detected: "detected",
  diagnosing: "diagnosing",
  fix_proposed: "fix proposed",
  fix_validated: "validated",
  pr_opened: "PR opened",
  diagnosed: "diagnosed",
  diagnosis_failed: "failed",
  diagnosis_refused: "refused",
  fix_failed: "gate rejected",
};

export function Pipeline({ incident }: { incident: Incident }) {
  const stopped = STOPS_AT[incident.status];
  const marker = stopped ?? (incident.status as IncidentStatus);
  const reachedIndex = PIPELINE.indexOf(marker);

  return (
    <div className="pipeline">
      {PIPELINE.map((stage, index) => {
        const isMarker = index === reachedIndex;
        // The last stage is the end of the road, not a step in progress — an
        // amber "in flight" dot on `pr_opened` would read as unfinished work.
        const state = isMarker
          ? stopped
            ? "stopped"
            : index === PIPELINE.length - 1
              ? "shipped"
              : "current"
          : index < reachedIndex
            ? "done"
            : "";
        return (
          <span key={stage} style={{ display: "flex", alignItems: "center" }}>
            {index > 0 && <span className="pipeline-link" />}
            <span className={`pipeline-step ${state}`}>
              <span className="pipeline-dot" />
              {isMarker && stopped ? LABELS[incident.status as IncidentStatus] : LABELS[stage]}
            </span>
          </span>
        );
      })}
    </div>
  );
}
