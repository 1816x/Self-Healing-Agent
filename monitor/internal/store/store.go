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
	"sync"
	"time"

	_ "modernc.org/sqlite"

	"github.com/1816x/self-healing-agent/monitor/internal/detect"
)

// schemaVersion is the current PRAGMA user_version. Open() walks a
// hand-built DB from any earlier version up to this one — the honest
// answer to "how does a file-based store evolve its schema without a
// migration framework" for a project this size.
const schemaVersion = 3

const schemaV1 = `
CREATE TABLE IF NOT EXISTS incidents (
	id           INTEGER PRIMARY KEY AUTOINCREMENT,
	created_at   TEXT NOT NULL,
	kind         TEXT NOT NULL,
	status       TEXT NOT NULL DEFAULT 'detected',
	window_start TEXT NOT NULL,
	window_end   TEXT NOT NULL,
	evidence     TEXT NOT NULL
);`

// v2 adds what Phase 2's dedup/cooldown needs: a stable identity to merge
// repeat firings against (dedup_key), when an open incident was last
// touched (updated_at), and how many times it's re-fired (occurrences).
// F1 shipped without these, so a real DB from that phase has to migrate
// forward, not just get re-created.
var schemaV2Statements = []string{
	`ALTER TABLE incidents ADD COLUMN dedup_key TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE incidents ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE incidents ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1`,
	`UPDATE incidents SET dedup_key = kind WHERE dedup_key = ''`,
	`UPDATE incidents SET updated_at = window_end WHERE updated_at = ''`,
}

// v3 adds the columns the Phase 3 diagnosis agent writes back. The Go
// monitor never writes them — it only ever inserts and merges incidents —
// but the DDL lives here anyway: this package already owns the migration
// ladder, and one language owning all schema beats two languages agreeing
// on it. The Python agent asserts user_version >= 3 instead of carrying a
// duplicate copy of these statements (see docs/design-decisions.md).
var schemaV3Statements = []string{
	`ALTER TABLE incidents ADD COLUMN diagnosis TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE incidents ADD COLUMN diagnosed_at TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE incidents ADD COLUMN proposed_fix TEXT NOT NULL DEFAULT ''`,
}

// Incident lifecycle. The monitor only ever produces StatusDetected; every
// later transition is written by the Phase 3 agent (and Phase 4's PR step).
// They're declared here so both sides share one vocabulary rather than
// trading bare strings.
const (
	StatusDetected = "detected"
	// StatusDiagnosing is claimed atomically, so two agent processes can't
	// pick up the same incident.
	StatusDiagnosing = "diagnosing"
	StatusDiagnosed  = "diagnosed"
	// StatusFixProposed means a diff was recorded. Phase 4 adds the
	// validation gate and the PR; Phase 3 only records.
	StatusFixProposed = "fix_proposed"
	// StatusDiagnosisFailed covers a tool-loop error or a blown iteration
	// cap; StatusDiagnosisRefused covers stop_reason == "refusal" from the
	// model, which is a distinct outcome worth not conflating with a bug.
	StatusDiagnosisFailed  = "diagnosis_failed"
	StatusDiagnosisRefused = "diagnosis_refused"
)

type Store struct {
	db *sql.DB
	// SQLite allows one writer at a time. Phase 2 adds a second goroutine
	// (the metrics scrape loop) writing incidents alongside the existing
	// log-tailer loop; a mutex around every write is simpler and more
	// certain than relying on busy_timeout retries to paper over
	// "database is locked" under concurrent access.
	mu sync.Mutex
}

// evidence is the JSON blob stored per incident: everything the Phase 3
// agent needs to start a diagnosis without re-reading the whole log.
type evidence struct {
	Summary string             `json:"summary"`
	Metrics map[string]float64 `json:"metrics"`
	Routes  map[string]int     `json:"routes,omitempty"`
	Samples []string           `json:"samples"`
}

// maxStoredSamples caps evidence growth across repeated merges of a
// still-ongoing incident — distinct from each detector's own per-firing
// sample cap (see detect.maxSamples), which bounds a single Incident.
const maxStoredSamples = 10

func Open(path string) (*Store, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	if err := migrate(db); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate schema: %w", err)
	}
	return &Store{db: db}, nil
}

func migrate(db *sql.DB) error {
	var version int
	if err := db.QueryRow(`PRAGMA user_version`).Scan(&version); err != nil {
		return fmt.Errorf("read user_version: %w", err)
	}

	if version < 1 {
		if _, err := db.Exec(schemaV1); err != nil {
			return fmt.Errorf("apply v1 schema: %w", err)
		}
		version = 1
	}
	if version < 2 {
		for _, stmt := range schemaV2Statements {
			if _, err := db.Exec(stmt); err != nil {
				return fmt.Errorf("apply v2 migration (%s): %w", stmt, err)
			}
		}
		version = 2
	}
	if version < 3 {
		for _, stmt := range schemaV3Statements {
			if _, err := db.Exec(stmt); err != nil {
				return fmt.Errorf("apply v3 migration (%s): %w", stmt, err)
			}
		}
		version = 3
	}

	// PRAGMA doesn't support bound parameters; version is our own int,
	// never user input, so this is safe to format directly.
	if _, err := db.Exec(fmt.Sprintf(`PRAGMA user_version = %d`, version)); err != nil {
		return fmt.Errorf("set user_version: %w", err)
	}
	return nil
}

func (s *Store) Close() error {
	return s.db.Close()
}

// execer is satisfied by both *sql.DB and *sql.Tx, so insert() works
// whether or not the caller is inside a transaction.
type execer interface {
	Exec(query string, args ...any) (sql.Result, error)
}

// InsertIncident stores a fired incident with status 'detected' and
// returns its row id, unconditionally — no dedup against any existing
// open incident. Kept for callers (and tests) that want an unconditional
// insert; the daemon itself uses UpsertIncident.
func (s *Store) InsertIncident(incident *detect.Incident) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return insert(s.db, incident, 1)
}

func insert(x execer, incident *detect.Incident, occurrences int) (int64, error) {
	blob, err := json.Marshal(evidence{
		Summary: incident.Summary,
		Metrics: incident.Metrics,
		Routes:  incident.Routes,
		Samples: incident.Samples,
	})
	if err != nil {
		return 0, fmt.Errorf("marshal evidence: %w", err)
	}

	windowEnd := incident.WindowEnd.UTC().Format(time.RFC3339)
	result, err := x.Exec(
		`INSERT INTO incidents (created_at, kind, dedup_key, window_start, window_end, evidence, updated_at, occurrences)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		// created_at is real wall-clock time (operational metadata: when
		// the monitor actually wrote the row). updated_at tracks the
		// incident's own event-time (WindowEnd), not wall-clock — dedup
		// compares it against a later incident's WindowStart, and mixing
		// event-time with wall-clock there breaks both production
		// correctness under log-delivery lag and deterministic testing.
		time.Now().UTC().Format(time.RFC3339),
		incident.Kind,
		incident.DedupKey,
		incident.WindowStart.UTC().Format(time.RFC3339),
		windowEnd,
		string(blob),
		windowEnd,
		occurrences,
	)
	if err != nil {
		return 0, fmt.Errorf("insert incident: %w", err)
	}
	return result.LastInsertId()
}

// UpsertIncident merges a fresh firing into the still-open incident with
// the same DedupKey if one was updated within dedupWindow of this
// firing's start; otherwise it inserts a new row. Merging replaces the
// evidence with the incoming (latest) snapshot of the condition, extends
// window_end, appends samples up to maxStoredSamples, and increments
// occurrences — it does not attempt to numerically combine old and new
// Metrics, which would need per-metric semantics (sum vs. max vs.
// latest) this store has no way to know. "Latest state plus an
// occurrence count" is how most real incident/alerting systems show an
// ongoing condition, so the simplification is a reasonable one, not a
// missing feature.
//
// This is what turns a flapping signal into one incident row instead of
// one per detector reset — the gap the Phase 1 live run surfaced.
func (s *Store) UpsertIncident(incident *detect.Incident, dedupWindow time.Duration) (id int64, merged bool, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	tx, err := s.db.Begin()
	if err != nil {
		return 0, false, fmt.Errorf("begin: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op once committed

	var existingID int64
	var existingUpdatedAt, existingEvidence string
	var existingOccurrences int
	row := tx.QueryRow(
		`SELECT id, updated_at, evidence, occurrences FROM incidents
		 WHERE dedup_key = ? AND status = ?
		 ORDER BY updated_at DESC LIMIT 1`,
		incident.DedupKey, StatusDetected,
	)
	scanErr := row.Scan(&existingID, &existingUpdatedAt, &existingEvidence, &existingOccurrences)

	switch {
	case scanErr == sql.ErrNoRows:
		newID, insertErr := insert(tx, incident, 1)
		if insertErr != nil {
			return 0, false, insertErr
		}
		if err := tx.Commit(); err != nil {
			return 0, false, fmt.Errorf("commit: %w", err)
		}
		return newID, false, nil

	case scanErr != nil:
		return 0, false, fmt.Errorf("query existing incident: %w", scanErr)
	}

	lastUpdate, parseErr := time.Parse(time.RFC3339, existingUpdatedAt)
	if parseErr != nil || incident.WindowStart.Sub(lastUpdate) > dedupWindow {
		newID, insertErr := insert(tx, incident, 1)
		if insertErr != nil {
			return 0, false, insertErr
		}
		if err := tx.Commit(); err != nil {
			return 0, false, fmt.Errorf("commit: %w", err)
		}
		return newID, false, nil
	}

	var prev evidence
	if err := json.Unmarshal([]byte(existingEvidence), &prev); err != nil {
		return 0, false, fmt.Errorf("unmarshal existing evidence: %w", err)
	}
	mergedEvidence := evidence{
		Summary: incident.Summary,
		Metrics: incident.Metrics,
		Routes:  incident.Routes,
		Samples: append(append([]string{}, prev.Samples...), incident.Samples...),
	}
	if len(mergedEvidence.Samples) > maxStoredSamples {
		mergedEvidence.Samples = mergedEvidence.Samples[len(mergedEvidence.Samples)-maxStoredSamples:]
	}
	blob, err := json.Marshal(mergedEvidence)
	if err != nil {
		return 0, false, fmt.Errorf("marshal merged evidence: %w", err)
	}

	windowEnd := incident.WindowEnd.UTC().Format(time.RFC3339)
	_, err = tx.Exec(
		`UPDATE incidents SET window_end = ?, updated_at = ?, evidence = ?, occurrences = ?
		 WHERE id = ?`,
		windowEnd,
		windowEnd, // event-time, not wall-clock — see the note in insert()
		string(blob),
		existingOccurrences+1,
		existingID,
	)
	if err != nil {
		return 0, false, fmt.Errorf("update incident: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return 0, false, fmt.Errorf("commit: %w", err)
	}
	return existingID, true, nil
}

// CountByStatus is a small helper for tests and the daemon's own logging.
func (s *Store) CountByStatus(status string) (int, error) {
	var n int
	err := s.db.QueryRow(`SELECT COUNT(*) FROM incidents WHERE status = ?`, status).Scan(&n)
	return n, err
}
