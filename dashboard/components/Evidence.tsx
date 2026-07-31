import type { Evidence } from "@/lib/incidents.ts";

/**
 * What the detector saw, rendered with the units it actually measured in.
 *
 * The three detectors report different metric keys, so a generic
 * key/value dump would show `p95_ms: 243` next to `slope_per_sec: 0.67` with no
 * indication that one breached a threshold and the other is a rate. The
 * per-kind shaping is the whole value of this panel.
 */
interface Metric {
  label: string;
  value: string;
  over?: boolean;
}

function metricsFor(kind: string, metrics: Record<string, number>): Metric[] {
  const get = (key: string): number | undefined => metrics[key];

  if (kind === "latency_p95") {
    const p95 = get("p95_ms");
    const threshold = get("threshold_ms");
    return [
      {
        label: "p95",
        value: p95 !== undefined ? `${round(p95)} ms` : "—",
        over: p95 !== undefined && threshold !== undefined && p95 > threshold,
      },
      { label: "threshold", value: threshold !== undefined ? `${round(threshold)} ms` : "—" },
      { label: "samples", value: String(get("sample_count") ?? "—") },
    ];
  }

  if (kind === "memory_growth") {
    const start = get("start_value");
    const end = get("end_value");
    return [
      { label: "growth", value: get("slope_per_sec") !== undefined ? `${round(get("slope_per_sec")!, 2)}/s` : "—", over: true },
      {
        label: "cache size",
        value: start !== undefined && end !== undefined ? `${round(start)} → ${round(end)}` : "—",
      },
    ];
  }

  if (kind === "error_rate") {
    return [{ label: "errors in window", value: String(get("error_count") ?? "—"), over: true }];
  }

  // An unknown kind still renders — a v5 detector shouldn't blank this panel.
  return Object.entries(metrics).map(([label, value]) => ({ label, value: String(value) }));
}

function round(value: number, places = 0): string {
  return value.toFixed(places);
}

export function EvidencePanel({ kind, evidence }: { kind: string; evidence: Evidence }) {
  const metrics = metricsFor(kind, evidence.metrics);

  return (
    <>
      {metrics.length > 0 && (
        <div className="metrics">
          {metrics.map((metric) => (
            <div className="metric" key={metric.label}>
              <div className="label">{metric.label}</div>
              <div className={`value ${metric.over ? "over" : ""}`}>{metric.value}</div>
            </div>
          ))}
        </div>
      )}

      {evidence.routes && (
        <table className="routes">
          <thead>
            <tr>
              <th>route</th>
              <th>count</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(evidence.routes).map(([route, count]) => (
              <tr key={route}>
                <td>{route}</td>
                <td>{count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {evidence.samples.length > 0 && (
        <pre className="samples" style={{ marginTop: 16 }}>
          {evidence.samples.join("\n")}
        </pre>
      )}
    </>
  );
}
