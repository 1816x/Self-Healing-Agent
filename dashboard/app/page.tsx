import Link from "next/link";

import { Pipeline } from "@/components/Pipeline.tsx";
import { StatusBadge } from "@/components/StatusBadge.tsx";
import { StoreMissing } from "@/components/StoreMissing.tsx";
import { statusDetail, type Incident } from "@/lib/incidents.ts";
import { listIncidents } from "@/lib/queries.ts";

// The store is written by a Go daemon in another process while this page is
// being looked at. A cached render would show a stale pipeline, which is the
// one thing this page exists to avoid.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export default function IncidentList() {
  let incidents: Incident[];
  try {
    incidents = listIncidents();
  } catch (error) {
    return <StoreMissing error={error} />;
  }

  return (
    <>
      <header className="masthead">
        <div>
          <h1>Self-Healing Incident Agent</h1>
          <p>
            Incidents detected by the monitor, diagnosed by the agent, and the fixes it
            proposed.
          </p>
        </div>
        <div className="counts">
          {incidents.length} incident{incidents.length === 1 ? "" : "s"}
        </div>
      </header>

      {incidents.length === 0 ? (
        <div className="card">
          <p className="empty" style={{ margin: 0 }}>
            No incidents yet. Run <code>scripts/run_demo.sh</code>, then{" "}
            <code>scripts/inject_bug.sh b1</code> in another terminal.
          </p>
        </div>
      ) : (
        <div className="stack">
          {incidents.map((incident) => (
            <IncidentCard key={incident.id} incident={incident} />
          ))}
        </div>
      )}
    </>
  );
}

function IncidentCard({ incident }: { incident: Incident }) {
  const detail = statusDetail(incident);

  return (
    <Link href={`/incidents/${incident.id}`} className="card incident-card">
      <div className="incident-head">
        <span className="incident-id">#{incident.id}</span>
        <span className="incident-kind">{incident.kind}</span>
        <StatusBadge incident={incident} />
        {incident.occurrences > 1 && (
          <span className="repeat" title="times this ongoing condition re-fired and merged">
            ×{incident.occurrences}
          </span>
        )}
      </div>

      <p className="incident-summary">{incident.evidence.summary || "(no summary recorded)"}</p>
      {detail && <p className="incident-detail">{detail}</p>}

      <Pipeline incident={incident} />

      <div className="meta-row" style={{ marginTop: 12 }}>
        <span>detected {incident.createdAt}</span>
        {incident.diagnosedAt && <span>diagnosed {incident.diagnosedAt}</span>}
      </div>
    </Link>
  );
}
