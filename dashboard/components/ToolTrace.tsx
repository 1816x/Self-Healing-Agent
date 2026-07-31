import type { ToolCall } from "@/lib/incidents.ts";

/**
 * The agent's tool calls, in order.
 *
 * `diagnosis.tool_calls` was shaped for this view specifically — the agent
 * records the tool name, its inputs, any error, and the size of what came back,
 * but never the output itself. That is the point: the trace shows what the
 * agent looked at without turning the incident store into a log archive.
 */
function formatArgs(input: Record<string, unknown>): string {
  const parts = Object.entries(input).map(([key, value]) => `${key}=${format(value)}`);
  return parts.join(" ");
}

function format(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

export function ToolTrace({ calls }: { calls: ToolCall[] }) {
  if (calls.length === 0) {
    return <p className="empty">No tool calls recorded.</p>;
  }

  return (
    <ol className="trace">
      {calls.map((call, index) => (
        <li key={index}>
          <span className="n">{index + 1}</span>
          <span className="tool">{call.tool}</span>
          <span className="args">
            {formatArgs(call.input)}
            {call.error && <div className="err">error: {call.error}</div>}
          </span>
          <span className="size">
            {call.error ? "—" : call.outputChars !== null ? `${call.outputChars} chars` : ""}
          </span>
        </li>
      ))}
    </ol>
  );
}
