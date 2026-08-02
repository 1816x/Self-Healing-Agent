import { SchemaTooOldError, StoreUnavailableError } from "@/lib/db.ts";

/**
 * The two ways reading the store fails for a reason the reader can fix:
 * the demo hasn't been run yet, or the database predates schema v4. Anything
 * else is a real bug and is rethrown so it reaches the error overlay.
 */
export function StoreMissing({ error }: { error: unknown }) {
  if (!(error instanceof StoreUnavailableError) && !(error instanceof SchemaTooOldError)) {
    throw error;
  }

  return (
    <div className="error-box">
      <strong>Cannot read the incident store</strong>
      <p>{error.message}</p>
    </div>
  );
}
