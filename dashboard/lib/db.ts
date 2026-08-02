/**
 * The only module in this app that opens the incident store.
 *
 * Three constraints, all learned the hard way elsewhere in this repo:
 *
 * 1. **Read-only, enforced by SQLite.** The dashboard displays the pipeline; it
 *    never advances it. `readOnly: true` makes that a property of the handle
 *    rather than a convention a future edit could quietly break. Reruns are a
 *    CLI flag (`python -m diagnose --resume`), not a button.
 *
 * 2. **Open, query, close — never a module-level handle.** The database is in
 *    rollback-journal mode, not WAL, and is written by a Go daemon in another
 *    process. A long-lived reader is exactly what killed the monitor's startup
 *    migration once (`docs/design-decisions.md`) and wedged the agent's test
 *    suite once (`agent/tests/conftest.py`). `busy_timeout` matches the Go
 *    side's 5s.
 *
 * 3. **Assert the schema version.** Go owns every migration. This app reads
 *    columns v4 introduced, so it refuses an older database with the same
 *    message the Python agent uses rather than rendering blank columns.
 */
import { DatabaseSync } from "node:sqlite";
import path from "node:path";

/** Must match monitor/internal/store/store.go's schemaVersion. */
export const REQUIRED_SCHEMA_VERSION = 4;

const BUSY_TIMEOUT_MS = 5_000;

export class SchemaTooOldError extends Error {}
export class StoreUnavailableError extends Error {}

/** Where the store lives. Defaults to the repo root, one level up from here. */
export function databasePath(): string {
  const configured = process.env.INCIDENTS_DB;
  if (configured) return path.resolve(configured);
  return path.resolve(process.cwd(), "..", "incidents.db");
}

/**
 * Runs `read` against a short-lived read-only handle.
 *
 * Every caller goes through this: it is what guarantees the handle is closed
 * even when a query throws, and what keeps the read-only flag from being
 * something each call site has to remember.
 */
export function withStore<T>(read: (db: DatabaseSync) => T): T {
  const file = databasePath();
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(file, { readOnly: true, timeout: BUSY_TIMEOUT_MS });
  } catch (cause) {
    // Almost always "the demo hasn't been run yet". Say that, rather than
    // leaking a SQLite errno into a page.
    throw new StoreUnavailableError(
      `could not open the incident store at ${file}. Run scripts/run_demo.sh to ` +
        `create it, or set INCIDENTS_DB to point at an existing one.`,
      { cause },
    );
  }

  try {
    assertSchemaVersion(db, file);
    return read(db);
  } finally {
    db.close();
  }
}

function assertSchemaVersion(db: DatabaseSync, file: string): void {
  const row = db.prepare("PRAGMA user_version").get() as
    | { user_version?: number }
    | undefined;
  const version = row?.user_version ?? 0;
  if (version < REQUIRED_SCHEMA_VERSION) {
    throw new SchemaTooOldError(
      `incident store at ${file} is at schema v${version}, the dashboard needs ` +
        `v${REQUIRED_SCHEMA_VERSION}. The monitor owns migrations — run it once ` +
        `against this database to migrate it forward.`,
    );
  }
}
