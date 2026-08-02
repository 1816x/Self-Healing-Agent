import Link from "next/link";
import { notFound } from "next/navigation";

import { DiffView } from "@/components/DiffView.tsx";
import { EvidencePanel } from "@/components/Evidence.tsx";
import { Pipeline } from "@/components/Pipeline.tsx";
import { Provenance } from "@/components/Provenance.tsx";
import { StatusBadge } from "@/components/StatusBadge.tsx";
import { StoreMissing } from "@/components/StoreMissing.tsx";
import { ToolTrace } from "@/components/ToolTrace.tsx";
import { statusDetail, type Incident, type Validation } from "@/lib/incidents.ts";
import { getIncident } from "@/lib/queries.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export default async function IncidentDetail({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const incidentId = Number(id);
  if (!Number.isInteger(incidentId)) notFound();

  let incident: Incident | null;
  try {
    incident = getIncident(incidentId);
  } catch (error) {
    return <StoreMissing error={error} />;
  }
  if (!incident) notFound();

  const detail = statusDetail(incident);

  return (
    <>
      <Link href="/" className="back">
        ← all incidents
      </Link>

      <header className="masthead">
        <div>
          <h1>
            <span className="incident-id">#{incident.id}</span> {incident.kind}
          </h1>
          <p>{incident.evidence.summary}</p>
        </div>
        <StatusBadge incident={incident} />
      </header>

      <section className="panel">
        <h2>pipeline</h2>
        <div className="body">
          <Pipeline incident={incident} />
          {detail && <p className="incident-detail" style={{ marginTop: 14 }}>{detail}</p>}
          <dl className="facts" style={{ marginTop: 14 }}>
            <dt>window</dt>
            <dd>
              {incident.windowStart} → {incident.windowEnd}
            </dd>
            <dt>dedup key</dt>
            <dd>{incident.dedupKey}</dd>
            <dt>occurrences</dt>
            <dd>
              {incident.occurrences}
              {incident.occurrences > 1 && " (re-fired and merged, not duplicated)"}
            </dd>
            {incident.diagnosedAt && (
              <>
                <dt>diagnosed</dt>
                <dd>{incident.diagnosedAt}</dd>
              </>
            )}
            {incident.prUrl && (
              <>
                <dt>pull request</dt>
                <dd>
                  <a href={incident.prUrl} target="_blank" rel="noreferrer">
                    {incident.prUrl}
                  </a>
                </dd>
              </>
            )}
          </dl>
        </div>
      </section>

      <section className="panel">
        <h2>evidence — what the detector saw</h2>
        <div className="body">
          <EvidencePanel kind={incident.kind} evidence={incident.evidence} />
        </div>
      </section>

      <DiagnosisSection incident={incident} />

      {incident.proposedFix && (
        <section className="panel">
          <h2>proposed fix</h2>
          <div className="body">
            {incident.proposedFix.rationale && (
              <p className="prose">{incident.proposedFix.rationale}</p>
            )}
            <DiffView diff={incident.proposedFix.diff} />
          </div>
        </section>
      )}

      <GateSection validation={incident.validation} />
    </>
  );
}

function DiagnosisSection({ incident }: { incident: Incident }) {
  const { diagnosis } = incident;

  if (diagnosis.kind === "absent") return null;

  if (diagnosis.kind === "malformed") {
    return (
      <section className="panel">
        <h2>diagnosis</h2>
        <div className="body">
          <p className="empty">
            The stored diagnosis is not valid JSON. Raw value:
          </p>
          <pre className="samples">{diagnosis.raw}</pre>
        </div>
      </section>
    );
  }

  if (diagnosis.kind === "failed") {
    return (
      <section className="panel">
        <h2>diagnosis</h2>
        <div className="body">
          <p className="verdict rejected" style={{ margin: 0 }}>
            The run failed: {diagnosis.error}
          </p>
        </div>
      </section>
    );
  }

  if (diagnosis.kind === "refused") {
    return (
      <section className="panel">
        <h2>diagnosis</h2>
        <div className="body">
          <p className="verdict unproven" style={{ marginBottom: 8 }}>
            The model declined this request ({diagnosis.category}).
          </p>
          <p className="empty" style={{ margin: 0 }}>
            {diagnosis.explanation ??
              "Recorded as its own outcome rather than an error — nothing is broken, and " +
                "re-sending the same prompt would not help."}
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="panel">
      <h2>diagnosis</h2>
      <Provenance diagnosis={diagnosis} />
      <div className="body">
        {diagnosis.rootCause && <p className="prose">{diagnosis.rootCause}</p>}

        <dl className="facts" style={{ marginBottom: 18 }}>
          {diagnosis.suspectCommit && (
            <>
              <dt>suspect commit</dt>
              <dd>{diagnosis.suspectCommit}</dd>
            </>
          )}
          {diagnosis.turns !== null && (
            <>
              <dt>turns</dt>
              <dd>{diagnosis.turns}</dd>
            </>
          )}
          {diagnosis.confidence && (
            <>
              <dt>confidence</dt>
              <dd>{diagnosis.confidence}</dd>
            </>
          )}
        </dl>

        {diagnosis.recentCommits.length > 0 && (
          <>
            <h3 style={{ fontSize: 13, margin: "0 0 8px" }}>Commits considered</h3>
            <pre className="samples" style={{ marginBottom: 18 }}>
              {diagnosis.recentCommits.join("\n")}
            </pre>
          </>
        )}

        <h3 style={{ fontSize: 13, margin: "0 0 4px" }}>Tool calls</h3>
        <ToolTrace calls={diagnosis.toolCalls} />

        {diagnosis.reasoning.length > 0 && (
          <details style={{ marginTop: 18 }}>
            <summary style={{ cursor: "pointer", fontSize: 13 }}>
              Summarized reasoning ({diagnosis.reasoning.length})
            </summary>
            <div style={{ marginTop: 10 }}>
              {diagnosis.reasoning.map((thought, index) => (
                <p className="prose" key={index}>
                  {thought}
                </p>
              ))}
            </div>
          </details>
        )}
      </div>
    </section>
  );
}

/**
 * What this machine checked, kept visually separate from what the model
 * claimed. `proves_a_fix` is the headline because it is the only assertion
 * here that means the fix works: red before, green after.
 */
function GateSection({ validation }: { validation: Validation }) {
  if (validation.kind === "absent") return null;

  if (validation.kind === "malformed") {
    return (
      <section className="panel">
        <h2>validation gate</h2>
        <div className="body">
          <pre className="samples">{validation.raw}</pre>
        </div>
      </section>
    );
  }

  if (validation.kind === "error") {
    return (
      <section className="panel">
        <h2>validation gate</h2>
        <div className="body">
          <p className="verdict rejected" style={{ margin: 0 }}>
            {validation.error}
          </p>
        </div>
      </section>
    );
  }

  const verdict = !validation.applied
    ? { tone: "rejected", text: "The diff did not apply." }
    : validation.provesAFix
      ? { tone: "proven", text: "Tests were red before this diff and green after it." }
      : validation.ok
        ? { tone: "unproven", text: "Tests pass — but they also passed before, so nothing is proven." }
        : { tone: "rejected", text: "The diff applied, but the tests did not pass with it." };

  return (
    <section className="panel">
      <h2>validation gate — what this machine checked</h2>
      <div className="body">
        <p className={`verdict ${verdict.tone}`}>{verdict.text}</p>

        <dl className="facts">
          <dt>applied</dt>
          <dd>{validation.applied ? "yes" : "no"}</dd>
          <dt>tests before</dt>
          <dd>{validation.testsBefore ?? "—"}</dd>
          <dt>tests after</dt>
          <dd>{validation.testsAfter ?? "—"}</dd>
          {validation.files.length > 0 && (
            <>
              <dt>files</dt>
              <dd>{validation.files.join(", ")}</dd>
            </>
          )}
          {validation.recounted && (
            <>
              <dt>recounted</dt>
              <dd>yes — line offsets were adjusted to make the patch apply</dd>
            </>
          )}
          {validation.reason && (
            <>
              <dt>reason</dt>
              <dd>{validation.reason}</dd>
            </>
          )}
        </dl>

        {validation.prError && (
          <p className="verdict unproven" style={{ marginTop: 16, marginBottom: 0 }}>
            The fix is verified; opening the pull request failed: {validation.prError}
          </p>
        )}
      </div>
    </section>
  );
}
