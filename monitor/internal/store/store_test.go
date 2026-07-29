package store

import (
	"database/sql"
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	_ "modernc.org/sqlite"

	"github.com/1816x/self-healing-agent/monitor/internal/detect"
)

func testIncident() *detect.Incident {
	start := time.Date(2026, 7, 28, 12, 0, 0, 0, time.UTC)
	return &detect.Incident{
		Kind:        "error_rate",
		DedupKey:    "error_rate",
		WindowStart: start,
		WindowEnd:   start.Add(3 * time.Second),
		Summary:     "5 errors across 1 route(s)",
		Metrics:     map[string]float64{"error_count": 5},
		Routes:      map[string]int{"/checkout": 5},
		Samples:     []string{"2026-07-28T12:00:00Z error request.completed /checkout status=500"},
	}
}

func TestInsertAndReadBack(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	id, err := s.InsertIncident(testIncident())
	if err != nil {
		t.Fatal(err)
	}
	if id != 1 {
		t.Errorf("first incident id = %d, want 1", id)
	}

	var kind, status, blob string
	err = s.db.QueryRow(
		`SELECT kind, status, evidence FROM incidents WHERE id = ?`, id,
	).Scan(&kind, &status, &blob)
	if err != nil {
		t.Fatal(err)
	}
	if kind != "error_rate" || status != "detected" {
		t.Errorf("kind=%q status=%q", kind, status)
	}

	var ev evidence
	if err := json.Unmarshal([]byte(blob), &ev); err != nil {
		t.Fatalf("evidence is not valid JSON: %v", err)
	}
	if ev.Metrics["error_count"] != 5 || ev.Routes["/checkout"] != 5 || len(ev.Samples) != 1 {
		t.Errorf("evidence round-trip mismatch: %+v", ev)
	}
}

func TestCountByStatus(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	for i := 0; i < 3; i++ {
		if _, err := s.InsertIncident(testIncident()); err != nil {
			t.Fatal(err)
		}
	}
	n, err := s.CountByStatus("detected")
	if err != nil {
		t.Fatal(err)
	}
	if n != 3 {
		t.Errorf("detected count = %d, want 3", n)
	}
	n, err = s.CountByStatus("diagnosed")
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Errorf("diagnosed count = %d, want 0", n)
	}
}

func TestOpenIsIdempotent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")
	s1, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s1.InsertIncident(testIncident()); err != nil {
		t.Fatal(err)
	}
	s1.Close()

	// Reopening an existing DB must not wipe or fail on the schema.
	s2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	n, err := s2.CountByStatus("detected")
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Errorf("incident lost across reopen: count = %d", n)
	}
}

// --- Schema migration ---

func TestOpenMigratesAV1DatabaseAndPreservesData(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")

	// Hand-build exactly what F1 shipped: no dedup_key/updated_at/occurrences,
	// and insert a row the old, 5-column way — a real DB from that phase,
	// not a fixture written to match the new code.
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec(schemaV1); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec(
		`INSERT INTO incidents (created_at, kind, window_start, window_end, evidence)
		 VALUES (?, ?, ?, ?, ?)`,
		"2026-07-28T12:00:00Z", "error_rate", "2026-07-28T12:00:00Z", "2026-07-28T12:00:03Z",
		`{"error_count":5,"routes":{"/checkout":5},"samples":["old evidence"]}`,
	); err != nil {
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}

	s, err := Open(path)
	if err != nil {
		t.Fatalf("Open must migrate a v1 database, got: %v", err)
	}
	defer s.Close()

	var version int
	if err := s.db.QueryRow(`PRAGMA user_version`).Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != schemaVersion {
		t.Errorf("user_version = %d, want %d", version, schemaVersion)
	}

	var dedupKey, updatedAt string
	var occurrences int
	err = s.db.QueryRow(`SELECT dedup_key, updated_at, occurrences FROM incidents WHERE id = 1`).
		Scan(&dedupKey, &updatedAt, &occurrences)
	if err != nil {
		t.Fatal(err)
	}
	if dedupKey != "error_rate" {
		t.Errorf("migrated dedup_key = %q, want backfilled from kind", dedupKey)
	}
	if updatedAt != "2026-07-28T12:00:03Z" {
		t.Errorf("migrated updated_at = %q, want backfilled from window_end", updatedAt)
	}
	if occurrences != 1 {
		t.Errorf("migrated occurrences = %d, want 1", occurrences)
	}

	// The pre-existing row must still be readable through the normal path.
	n, err := s.CountByStatus(StatusDetected)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Errorf("pre-migration row lost: count = %d", n)
	}
}

func TestMigrateIsIdempotent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")
	s1, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s1.InsertIncident(testIncident()); err != nil {
		t.Fatal(err)
	}
	s1.Close()

	// Re-opening an already-current-version DB must not error or duplicate columns.
	s2, err := Open(path)
	if err != nil {
		t.Fatalf("re-opening a current-version database must not error: %v", err)
	}
	defer s2.Close()
	n, err := s2.CountByStatus(StatusDetected)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Errorf("count = %d, want 1", n)
	}
}

// --- UpsertIncident dedup/cooldown ---

func TestUpsertInsertsWhenNoOpenIncidentExists(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	id, merged, err := s.UpsertIncident(testIncident(), 2*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if merged {
		t.Error("first firing must insert, not merge")
	}
	if id != 1 {
		t.Errorf("id = %d, want 1", id)
	}
}

func TestUpsertMergesAFlappingSignalIntoOneRow(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	base := testIncident()
	firstID, _, err := s.UpsertIncident(base, 2*time.Minute)
	if err != nil {
		t.Fatal(err)
	}

	// The condition re-fires 5 times, 10s apart, well inside the 2-minute
	// dedup window — this is exactly the F1 live-run finding: the same
	// ongoing failure resetting and re-triggering the detector.
	for i := 1; i <= 5; i++ {
		refire := testIncident()
		refire.WindowStart = base.WindowStart.Add(time.Duration(i) * 10 * time.Second)
		refire.WindowEnd = refire.WindowStart.Add(3 * time.Second)
		refire.Samples = []string{"refire evidence"}
		id, merged, err := s.UpsertIncident(refire, 2*time.Minute)
		if err != nil {
			t.Fatal(err)
		}
		if !merged {
			t.Fatalf("refire %d: expected a merge, got a new row", i)
		}
		if id != firstID {
			t.Fatalf("refire %d: id = %d, want the original %d", i, id, firstID)
		}
	}

	n, err := s.CountByStatus(StatusDetected)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("flapping signal produced %d incident rows, want 1", n)
	}

	var occurrences int
	var blob string
	if err := s.db.QueryRow(`SELECT occurrences, evidence FROM incidents WHERE id = ?`, firstID).
		Scan(&occurrences, &blob); err != nil {
		t.Fatal(err)
	}
	if occurrences != 6 {
		t.Errorf("occurrences = %d, want 6 (1 initial + 5 merges)", occurrences)
	}
	var ev evidence
	if err := json.Unmarshal([]byte(blob), &ev); err != nil {
		t.Fatal(err)
	}
	if len(ev.Samples) == 0 || ev.Samples[len(ev.Samples)-1] != "refire evidence" {
		t.Errorf("merged evidence should carry the latest samples, got %v", ev.Samples)
	}
}

func TestUpsertOpensANewIncidentAfterDedupWindowElapses(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	base := testIncident()
	firstID, _, err := s.UpsertIncident(base, time.Minute)
	if err != nil {
		t.Fatal(err)
	}

	later := testIncident()
	later.WindowStart = base.WindowStart.Add(5 * time.Minute) // well past the 1-minute window
	later.WindowEnd = later.WindowStart.Add(3 * time.Second)
	secondID, merged, err := s.UpsertIncident(later, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if merged {
		t.Error("a firing after the dedup window elapsed must open a new incident")
	}
	if secondID == firstID {
		t.Error("expected a distinct row for the new occurrence")
	}

	n, err := s.CountByStatus(StatusDetected)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Errorf("count = %d, want 2 separate incidents", n)
	}
}

func TestUpsertKeepsDifferentDedupKeysSeparate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "incidents.db")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	errRate := testIncident()
	latency := &detect.Incident{
		Kind: "latency_p95", DedupKey: "latency_p95:/products",
		WindowStart: errRate.WindowStart, WindowEnd: errRate.WindowEnd,
		Summary: "p95 250ms", Metrics: map[string]float64{"p95_ms": 250},
	}

	if _, _, err := s.UpsertIncident(errRate, 2*time.Minute); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.UpsertIncident(latency, 2*time.Minute); err != nil {
		t.Fatal(err)
	}

	n, err := s.CountByStatus(StatusDetected)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Errorf("different dedup keys must not merge: count = %d, want 2", n)
	}
}
