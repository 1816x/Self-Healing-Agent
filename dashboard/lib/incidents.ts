/**
 * Turns a raw `incidents` row into something a component can render without
 * defensive checks scattered through the JSX.
 *
 * Two properties of this schema make a naive mapper wrong:
 *
 * - **Nothing is SQL NULL.** Every column added after schema v1 is
 *   `NOT NULL DEFAULT ''`, so "this stage hasn't happened yet" arrives as the
 *   empty string. `JSON.parse("")` throws.
 * - **`diagnosis` and `validation` are not one shape each.** A diagnosis can be
 *   a model result, an error, or a refusal; a validation can be a gate record,
 *   a gate record plus a PR error, or a bare error. They are modelled here as
 *   discriminated unions so a component has to handle the arm it is given.
 *
 * Malformed JSON degrades to a `malformed` arm carrying the raw text rather
 * than throwing. Three languages write this file; a page that 500s on one bad
 * blob would hide every other incident.
 */

export const STATUSES = [
  "detected",
  "diagnosing",
  "diagnosed",
  "fix_proposed",
  "diagnosis_failed",
  "diagnosis_refused",
  "fix_validated",
  "fix_failed",
  "pr_opened",
] as const;

export type IncidentStatus = (typeof STATUSES)[number];

/** The happy path through the pipeline, in order. Used by the progress widget. */
export const PIPELINE: IncidentStatus[] = [
  "detected",
  "diagnosing",
  "fix_proposed",
  "fix_validated",
  "pr_opened",
];

/** Statuses that end the run without reaching `pr_opened`. */
export const TERMINAL_STATUSES: IncidentStatus[] = [
  "diagnosed",
  "diagnosis_failed",
  "diagnosis_refused",
  "fix_failed",
];

export type IncidentKind = "error_rate" | "latency_p95" | "memory_growth";

/**
 * Where a diagnosis came from. `replay-scripted` and `heuristic` are NOT model
 * output, and the repo treats never conflating them as a hard requirement —
 * see `agent/diagnose/transcripts/README.md`.
 */
export type DiagnosisSource = "live" | "replay" | "replay-scripted" | "heuristic";

export interface ToolCall {
  tool: string;
  input: Record<string, unknown>;
  error: string | null;
  outputChars: number | null;
}

export interface Evidence {
  summary: string;
  /** Per-kind: error_count | p95_ms+threshold_ms+sample_count | slope_per_sec+start_value+end_value. */
  metrics: Record<string, number>;
  /** Absent for memory_growth, which has no per-route dimension. */
  routes: Record<string, number> | null;
  /** Go marshals a nil slice as JSON null; memory_growth never sets it. */
  samples: string[];
}

export type Diagnosis =
  | { kind: "absent" }
  | { kind: "malformed"; raw: string }
  | { kind: "failed"; error: string }
  | { kind: "refused"; category: string; explanation: string | null }
  | {
      kind: "model";
      source: DiagnosisSource;
      turns: number | null;
      toolCalls: ToolCall[];
      reasoning: string[];
      rootCause: string | null;
      suspectCommit: string | null;
      /** Heuristic-only fields. */
      confidence: string | null;
      note: string | null;
      recentCommits: string[];
    };

export type TestOutcome = "passed" | "failed" | "skipped" | null;

export type Validation =
  | { kind: "absent" }
  | { kind: "malformed"; raw: string }
  | { kind: "error"; error: string }
  | {
      kind: "record";
      ok: boolean;
      applied: boolean;
      reason: string;
      recounted: boolean;
      testsBefore: TestOutcome;
      testsAfter: TestOutcome;
      files: string[];
      /** Red before AND green after — the only claim that proves a fix works. */
      provesAFix: boolean;
      /** Set when the gate passed but the PR call failed. */
      prError: string | null;
    };

export interface ProposedFix {
  diff: string;
  rationale: string;
}

export interface Incident {
  id: number;
  createdAt: string;
  kind: IncidentKind | string;
  status: IncidentStatus | string;
  windowStart: string;
  windowEnd: string;
  updatedAt: string;
  dedupKey: string;
  occurrences: number;
  evidence: Evidence;
  diagnosis: Diagnosis;
  diagnosedAt: string | null;
  proposedFix: ProposedFix | null;
  validation: Validation;
  prUrl: string | null;
}

/** The columns as SQLite hands them back. Every text column is non-null. */
export interface IncidentRow {
  id: number;
  created_at: string;
  kind: string;
  status: string;
  window_start: string;
  window_end: string;
  evidence: string;
  dedup_key: string;
  updated_at: string;
  occurrences: number;
  diagnosis: string;
  diagnosed_at: string;
  proposed_fix: string;
  validation: string;
  pr_url: string;
}

type Json = Record<string, unknown>;

const MALFORMED = Symbol("malformed");

/** Decodes a JSON column. `""` means "never written"; bad JSON is not fatal. */
function decode(raw: string): Json | null | typeof MALFORMED {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return MALFORMED;
    }
    return parsed as Json;
  } catch {
    return MALFORMED;
  }
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function optionalStr(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function strArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function numberMap(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out: Record<string, number> = {};
  for (const [key, raw] of Object.entries(value as Json)) {
    if (typeof raw === "number" && Number.isFinite(raw)) out[key] = raw;
  }
  return out;
}

export function parseEvidence(raw: string): Evidence {
  const blob = decode(raw);
  if (blob === null || blob === MALFORMED) {
    return { summary: "", metrics: {}, routes: null, samples: [] };
  }
  const routes = numberMap(blob.routes);
  return {
    summary: str(blob.summary),
    metrics: numberMap(blob.metrics),
    routes: blob.routes && Object.keys(routes).length > 0 ? routes : null,
    samples: strArray(blob.samples),
  };
}

function parseToolCalls(value: unknown): ToolCall[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): ToolCall[] => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const call = entry as Json;
    const input =
      call.input && typeof call.input === "object" && !Array.isArray(call.input)
        ? (call.input as Record<string, unknown>)
        : {};
    return [
      {
        tool: str(call.tool, "(unnamed)"),
        input,
        error: optionalStr(call.error),
        outputChars: num(call.output_chars),
      },
    ];
  });
}

const SOURCES: DiagnosisSource[] = ["live", "replay", "replay-scripted", "heuristic"];

export function parseDiagnosis(raw: string): Diagnosis {
  const blob = decode(raw);
  if (blob === null) return { kind: "absent" };
  if (blob === MALFORMED) return { kind: "malformed", raw };

  // `mark_refused` — the model declined. Distinct from an error on purpose.
  if (blob.refusal && typeof blob.refusal === "object") {
    const refusal = blob.refusal as Json;
    return {
      kind: "refused",
      category: str(refusal.category, "unspecified"),
      explanation: optionalStr(refusal.explanation),
    };
  }

  // `mark_failed` — a tool-loop error, an API error, or the turn cap. It has an
  // `error` key and none of the model fields.
  if (typeof blob.error === "string" && !blob.source) {
    return { kind: "failed", error: blob.error };
  }

  const rawSource = str(blob.source);
  const source: DiagnosisSource = SOURCES.includes(rawSource as DiagnosisSource)
    ? (rawSource as DiagnosisSource)
    : "heuristic";

  return {
    kind: "model",
    source,
    turns: num(blob.turns),
    toolCalls: parseToolCalls(blob.tool_calls),
    reasoning: strArray(blob.reasoning),
    rootCause: optionalStr(blob.root_cause),
    suspectCommit: optionalStr(blob.suspect_commit),
    confidence: optionalStr(blob.confidence),
    note: optionalStr(blob.note),
    recentCommits: strArray(blob.recent_commits),
  };
}

/**
 * True only for turns a model actually produced.
 *
 * `replay-scripted` is a hand-authored transcript and `heuristic` is rule-based
 * triage. Both exercise the real loop, neither is model output, and the CLI
 * shouts about it — the dashboard must too.
 */
export function isModelOutput(diagnosis: Diagnosis): boolean {
  return diagnosis.kind === "model" && (diagnosis.source === "live" || diagnosis.source === "replay");
}

function testOutcome(value: unknown): TestOutcome {
  return value === "passed" || value === "failed" || value === "skipped" ? value : null;
}

export function parseValidation(raw: string): Validation {
  const blob = decode(raw);
  if (blob === null) return { kind: "absent" };
  if (blob === MALFORMED) return { kind: "malformed", raw };

  // The catch-all path stores `{ok: false, error}` with no gate fields at all.
  if (typeof blob.error === "string" && blob.applied === undefined) {
    return { kind: "error", error: blob.error };
  }

  return {
    kind: "record",
    ok: blob.ok === true,
    applied: blob.applied === true,
    reason: str(blob.reason),
    recounted: blob.recounted === true,
    testsBefore: testOutcome(blob.tests_before),
    testsAfter: testOutcome(blob.tests_after),
    files: strArray(blob.files),
    provesAFix: blob.proves_a_fix === true,
    prError: optionalStr(blob.pr_error),
  };
}

export function parseProposedFix(raw: string): ProposedFix | null {
  const blob = decode(raw);
  if (blob === null || blob === MALFORMED) return null;
  const diff = str(blob.diff);
  if (!diff) return null;
  return { diff, rationale: str(blob.rationale) };
}

export function toIncident(row: IncidentRow): Incident {
  return {
    id: row.id,
    createdAt: row.created_at,
    kind: row.kind,
    status: row.status,
    windowStart: row.window_start,
    windowEnd: row.window_end,
    updatedAt: row.updated_at,
    dedupKey: row.dedup_key,
    occurrences: row.occurrences,
    evidence: parseEvidence(row.evidence),
    diagnosis: parseDiagnosis(row.diagnosis),
    diagnosedAt: optionalStr(row.diagnosed_at),
    proposedFix: parseProposedFix(row.proposed_fix),
    validation: parseValidation(row.validation),
    prUrl: optionalStr(row.pr_url),
  };
}

/**
 * One line describing where an incident actually stands.
 *
 * `fix_validated` is two different states: with `pr_error` the fix is verified
 * and the pull request failed; without it, no PR was attempted. Collapsing them
 * would undo a deliberate Phase 4 decision — a network error must not read as a
 * failed fix.
 */
export function statusDetail(incident: Incident): string {
  const { status, validation } = incident;
  if (status === "fix_validated" && validation.kind === "record" && validation.prError) {
    return "fix verified — the pull request could not be opened";
  }
  if (status === "fix_validated") {
    return "fix verified — no pull request attempted";
  }
  if (status === "fix_failed" && validation.kind === "record") {
    return validation.applied
      ? "the diff applied, but the tests did not pass with it"
      : "the diff did not apply";
  }
  if (status === "fix_proposed") {
    return "a diff is recorded but the gate has not run over it";
  }
  if (status === "diagnosis_refused" && incident.diagnosis.kind === "refused") {
    return `the model declined (${incident.diagnosis.category})`;
  }
  if (status === "diagnosis_failed" && incident.diagnosis.kind === "failed") {
    return incident.diagnosis.error;
  }
  return "";
}
