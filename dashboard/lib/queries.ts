// Explicit .ts specifiers: Next's bundler resolves them, and so does plain
// `node`, which is what runs the tests and the screenshot script.
import { withStore } from "./db.ts";
import { toIncident, type Incident, type IncidentRow } from "./incidents.ts";

// Named rather than `SELECT *` so a v5 migration adding a column can't silently
// change the shape this app believes it is reading.
const COLUMNS = `id, created_at, kind, status, window_start, window_end,
                 evidence, dedup_key, updated_at, occurrences, diagnosis,
                 diagnosed_at, proposed_fix, validation, pr_url`;

/**
 * Newest first. There is no index on this table and deliberately so: at demo
 * scale a scan is instant, and the schema belongs to the Go monitor — adding a
 * migration to serve a sort here would spend that rule on nothing.
 */
export function listIncidents(): Incident[] {
  return withStore((db) => {
    const rows = db
      .prepare(`SELECT ${COLUMNS} FROM incidents ORDER BY id DESC`)
      .all() as unknown as IncidentRow[];
    return rows.map(toIncident);
  });
}

export function getIncident(id: number): Incident | null {
  return withStore((db) => {
    const row = db.prepare(`SELECT ${COLUMNS} FROM incidents WHERE id = ?`).get(id) as
      | unknown
      | undefined;
    return row ? toIncident(row as IncidentRow) : null;
  });
}
