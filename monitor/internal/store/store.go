// Package store persists incidents to SQLite — the single file that acts
// as the contract between the monitor (writer), the diagnosis agent
// (Phase 3, reader/updater), and the dashboard (Phase 5, reader).
//
// Driver: modernc.org/sqlite (pure Go). See docs/design-decisions.md for
// why it was picked over the cgo-based alternative.
package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"github.com/1816x/self-healing-agent/monitor/internal/detect"
)

const schema = `
CREATE TABLE IF NOT EXISTS incidents (
	id           INTEGER PRIMARY KEY AUTOINCREMENT,
	created_at   TEXT NOT NULL,
	kind         TEXT NOT NULL,
	status       TEXT NOT NULL DEFAULT 'detected',
	window_start TEXT NOT NULL,
	window_end   TEXT NOT NULL,
	evidence     TEXT NOT NULL
);`

type Store struct {
	db *sql.DB
}

// evidence is the JSON blob stored per incident: everything the Phase 3
// agent needs to start a diagnosis without re-reading the whole log.
type evidence struct {
	ErrorCount int            `json:"error_count"`
	Routes     map[string]int `json:"routes"`
	Samples    []string       `json:"samples"`
}

func Open(path string) (*Store, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("create schema: %w", err)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error {
	return s.db.Close()
}

// InsertIncident stores a fired incident with status 'detected' and
// returns its row id.
func (s *Store) InsertIncident(incident *detect.Incident) (int64, error) {
	blob, err := json.Marshal(evidence{
		ErrorCount: incident.ErrorCount,
		Routes:     incident.Routes,
		Samples:    incident.Samples,
	})
	if err != nil {
		return 0, fmt.Errorf("marshal evidence: %w", err)
	}

	result, err := s.db.Exec(
		`INSERT INTO incidents (created_at, kind, window_start, window_end, evidence)
		 VALUES (?, ?, ?, ?, ?)`,
		time.Now().UTC().Format(time.RFC3339),
		incident.Kind,
		incident.WindowStart.UTC().Format(time.RFC3339),
		incident.WindowEnd.UTC().Format(time.RFC3339),
		string(blob),
	)
	if err != nil {
		return 0, fmt.Errorf("insert incident: %w", err)
	}
	return result.LastInsertId()
}

// CountByStatus is a small helper for tests and the daemon's own logging.
func (s *Store) CountByStatus(status string) (int, error) {
	var n int
	err := s.db.QueryRow(`SELECT COUNT(*) FROM incidents WHERE status = ?`, status).Scan(&n)
	return n, err
}
