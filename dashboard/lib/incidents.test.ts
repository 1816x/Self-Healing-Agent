/**
 * Tests for the row mapper, run by `node --test` with no test framework.
 *
 * The fixtures here are copied from what the Go monitor and the Python agent
 * actually write — `monitor/internal/store/store.go`, `agent/diagnose/store.py`
 * and `agent/diagnose/patch.py`. The arms most worth testing are the ones a
 * demo run rarely produces: a refusal, a failed gate, a validated fix whose
 * pull request failed, and a column that was never written.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  isModelOutput,
  parseDiagnosis,
  parseEvidence,
  parseProposedFix,
  parseValidation,
  statusDetail,
  toIncident,
  type IncidentRow,
} from "./incidents.ts";

function row(overrides: Partial<IncidentRow> = {}): IncidentRow {
  return {
    id: 1,
    created_at: "2026-07-29T12:00:00Z",
    kind: "error_rate",
    status: "detected",
    window_start: "2026-07-29T12:00:00Z",
    window_end: "2026-07-29T12:00:03Z",
    evidence: JSON.stringify({
      summary: "5 errors across 1 route(s)",
      metrics: { error_count: 5 },
      routes: { "/checkout": 5 },
      samples: ["2026-07-29T12:00:00Z error request.completed /checkout status=500"],
    }),
    dedup_key: "error_rate",
    updated_at: "2026-07-29T12:00:03Z",
    occurrences: 1,
    // Every column below is NOT NULL DEFAULT '' — this is what "unset" is.
    diagnosis: "",
    diagnosed_at: "",
    proposed_fix: "",
    validation: "",
    pr_url: "",
    ...overrides,
  };
}

// --- the empty-string contract ---

test("a freshly detected incident maps without any JSON parse throwing", () => {
  const incident = toIncident(row());
  assert.equal(incident.diagnosis.kind, "absent");
  assert.equal(incident.validation.kind, "absent");
  assert.equal(incident.proposedFix, null);
  assert.equal(incident.prUrl, null);
  assert.equal(incident.diagnosedAt, null);
});

test("malformed JSON degrades to a malformed arm instead of throwing", () => {
  const incident = toIncident(row({ diagnosis: "{oops", validation: "not json{" }));
  assert.equal(incident.diagnosis.kind, "malformed");
  assert.equal(incident.validation.kind, "malformed");
  // Corrupt evidence must not take the incident out of the list entirely.
  assert.deepEqual(parseEvidence("{oops").metrics, {});
});

// --- evidence, per kind ---

test("error_rate evidence keeps its route breakdown", () => {
  const evidence = parseEvidence(row().evidence);
  assert.equal(evidence.metrics.error_count, 5);
  assert.deepEqual(evidence.routes, { "/checkout": 5 });
  assert.equal(evidence.samples.length, 1);
});

test("latency evidence carries the threshold it breached", () => {
  const evidence = parseEvidence(
    JSON.stringify({
      summary: "p95 243ms on GET /products",
      metrics: { p95_ms: 243, threshold_ms: 100, sample_count: 7 },
      routes: { "/products": 7 },
      samples: [],
    }),
  );
  assert.equal(evidence.metrics.p95_ms, 243);
  assert.equal(evidence.metrics.threshold_ms, 100);
});

test("memory_growth evidence has no routes and null samples", () => {
  // Go omits `routes` entirely here and marshals the nil sample slice as null.
  const evidence = parseEvidence(
    JSON.stringify({
      summary: "cache grew 0 -> 13 items",
      metrics: { slope_per_sec: 0.67, start_value: 0, end_value: 13 },
      samples: null,
    }),
  );
  assert.equal(evidence.routes, null);
  assert.deepEqual(evidence.samples, []);
  assert.equal(evidence.metrics.slope_per_sec, 0.67);
});

// --- the four diagnosis shapes ---

test("a model diagnosis exposes its tool-call trace", () => {
  const diagnosis = parseDiagnosis(
    JSON.stringify({
      source: "live",
      turns: 5,
      tool_calls: [
        { tool: "read_logs", input: { level: "error" }, error: null, output_chars: 4210 },
        { tool: "git_blame", input: { file: "demo-app/app/main.py" }, error: "no such file" },
      ],
      reasoning: ["checked the logs first"],
      root_cause: "keys are strings, lookups are ints",
      suspect_commit: "8f2e079",
    }),
  );
  assert.equal(diagnosis.kind, "model");
  if (diagnosis.kind !== "model") return;
  assert.equal(diagnosis.toolCalls.length, 2);
  assert.equal(diagnosis.toolCalls[0]?.outputChars, 4210);
  // Unknown-tool entries omit output_chars entirely.
  assert.equal(diagnosis.toolCalls[1]?.outputChars, null);
  assert.equal(diagnosis.toolCalls[1]?.error, "no such file");
  assert.equal(diagnosis.suspectCommit, "8f2e079");
  assert.equal(isModelOutput(diagnosis), true);
});

test("mark_failed's error shape is not mistaken for a model result", () => {
  const diagnosis = parseDiagnosis(JSON.stringify({ error: "iteration cap reached" }));
  assert.equal(diagnosis.kind, "failed");
  if (diagnosis.kind !== "failed") return;
  assert.equal(diagnosis.error, "iteration cap reached");
});

test("a refusal is its own arm, not an error", () => {
  const diagnosis = parseDiagnosis(
    JSON.stringify({ refusal: { category: "cyber", explanation: null } }),
  );
  assert.equal(diagnosis.kind, "refused");
  if (diagnosis.kind !== "refused") return;
  assert.equal(diagnosis.category, "cyber");
  assert.equal(diagnosis.explanation, null);
});

test("the heuristic shape keeps its confidence and commit list", () => {
  const diagnosis = parseDiagnosis(
    JSON.stringify({
      source: "heuristic",
      confidence: "low",
      root_cause: "NOT A MODEL DIAGNOSIS. Recent commits touched /checkout.",
      suspect_commit: "abc1234",
      recent_commits: ["abc1234 perf(checkout): precompute price lookup table"],
      note: "rule-based triage",
    }),
  );
  assert.equal(diagnosis.kind, "model");
  if (diagnosis.kind !== "model") return;
  assert.equal(diagnosis.confidence, "low");
  assert.equal(diagnosis.recentCommits.length, 1);
});

// --- provenance: the requirement, not a nicety ---

test("scripted and heuristic diagnoses are never reported as model output", () => {
  for (const source of ["replay-scripted", "heuristic"]) {
    const diagnosis = parseDiagnosis(JSON.stringify({ source, turns: 4 }));
    assert.equal(isModelOutput(diagnosis), false, `${source} must not read as model output`);
  }
  for (const source of ["live", "replay"]) {
    const diagnosis = parseDiagnosis(JSON.stringify({ source, turns: 4 }));
    assert.equal(isModelOutput(diagnosis), true);
  }
});

test("an unrecognised source is treated as not-model-output", () => {
  // Failing closed matters more here than rendering the label accurately.
  const diagnosis = parseDiagnosis(JSON.stringify({ source: "something-new" }));
  assert.equal(isModelOutput(diagnosis), false);
});

// --- the three validation shapes ---

test("a passing gate record reports proves_a_fix", () => {
  const validation = parseValidation(
    JSON.stringify({
      ok: true,
      applied: true,
      reason: "",
      recounted: false,
      tests_before: "failed",
      tests_after: "passed",
      files: ["demo-app/app/main.py"],
      proves_a_fix: true,
    }),
  );
  assert.equal(validation.kind, "record");
  if (validation.kind !== "record") return;
  assert.equal(validation.provesAFix, true);
  assert.deepEqual(validation.files, ["demo-app/app/main.py"]);
  assert.equal(validation.prError, null);
});

test("a bare error record has no gate fields and must not claim one applied", () => {
  const validation = parseValidation(
    JSON.stringify({ ok: false, error: "error: patch failed: demo-app/app/main.py:29" }),
  );
  assert.equal(validation.kind, "error");
});

// --- fix_validated is two states ---

test("a verified fix whose PR failed reads differently from one never attempted", () => {
  const gate = {
    ok: true,
    applied: true,
    reason: "",
    recounted: false,
    tests_before: "failed",
    tests_after: "passed",
    files: ["demo-app/app/main.py"],
    proves_a_fix: true,
  };

  const failed = toIncident(
    row({
      status: "fix_validated",
      validation: JSON.stringify({ ...gate, pr_error: "GitHub returned 403" }),
    }),
  );
  const notAttempted = toIncident(
    row({ status: "fix_validated", validation: JSON.stringify(gate) }),
  );

  assert.match(statusDetail(failed), /pull request could not be opened/);
  assert.match(statusDetail(notAttempted), /no pull request attempted/);
  assert.notEqual(statusDetail(failed), statusDetail(notAttempted));
});

test("fix_failed distinguishes a diff that did not apply from one that did not work", () => {
  const didNotApply = toIncident(
    row({
      status: "fix_failed",
      validation: JSON.stringify({ ok: false, applied: false, reason: "corrupt patch" }),
    }),
  );
  const didNotWork = toIncident(
    row({
      status: "fix_failed",
      validation: JSON.stringify({
        ok: false,
        applied: true,
        tests_before: "failed",
        tests_after: "failed",
      }),
    }),
  );

  assert.match(statusDetail(didNotApply), /did not apply/);
  assert.match(statusDetail(didNotWork), /tests did not pass/);
});

// --- the proposed fix ---

test("proposed_fix yields the diff and rationale, and nothing when unset", () => {
  const diff = '--- a/demo-app/app/main.py\n+++ b/demo-app/app/main.py\n@@ -28,7 +28,7 @@\n-old\n+new\n';
  const fix = parseProposedFix(JSON.stringify({ diff, rationale: "key by int" }));
  assert.equal(fix?.rationale, "key by int");
  assert.equal(fix?.diff, diff);

  assert.equal(parseProposedFix(""), null);
  assert.equal(parseProposedFix(JSON.stringify({ diff: "", rationale: "r" })), null);
});
