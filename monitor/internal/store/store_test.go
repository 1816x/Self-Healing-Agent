package store

import (
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

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
