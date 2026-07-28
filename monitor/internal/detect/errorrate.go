// Package detect holds the monitor's anomaly detectors. Phase 1 ships a
// single sliding-window error-rate detector; more signal types plus
// debounce/cooldown handling arrive in Phase 2 (see PLAN.md).
package detect

import (
	"fmt"
	"time"

	"github.com/1816x/self-healing-agent/monitor/internal/logtail"
)

// Incident is what a detector hands to the store when it fires.
type Incident struct {
	Kind        string
	WindowStart time.Time
	WindowEnd   time.Time
	ErrorCount  int
	Routes      map[string]int
	Samples     []string
}

// ErrorRate fires when at least Threshold error events land within Window.
//
// Time comes from event timestamps, not the wall clock — that makes the
// detector fully deterministic under test and immune to log-delivery lag.
type ErrorRate struct {
	Window    time.Duration
	Threshold int

	errors []logtail.LogEvent
}

const maxSamples = 5

// Observe feeds one event through the detector. It returns a non-nil
// Incident at the moment the threshold is crossed, and nil otherwise.
// After firing, the buffer resets so one sustained failure produces one
// incident, not one per subsequent error line (crude dedup — Phase 2
// replaces this with proper debounce/cooldown).
func (d *ErrorRate) Observe(event logtail.LogEvent) *Incident {
	cutoff := event.TS.Add(-d.Window)
	kept := d.errors[:0]
	for _, e := range d.errors {
		if e.TS.After(cutoff) {
			kept = append(kept, e)
		}
	}
	d.errors = kept

	if !event.IsError() {
		return nil
	}
	d.errors = append(d.errors, event)

	if len(d.errors) < d.Threshold {
		return nil
	}

	incident := &Incident{
		Kind:        "error_rate",
		WindowStart: d.errors[0].TS,
		WindowEnd:   event.TS,
		ErrorCount:  len(d.errors),
		Routes:      map[string]int{},
	}
	for _, e := range d.errors {
		incident.Routes[e.Route]++
		if len(incident.Samples) < maxSamples {
			incident.Samples = append(incident.Samples, fmt.Sprintf(
				"%s %s %s %s status=%d",
				e.TS.Format(time.RFC3339), e.Level, e.Event, e.Route, e.Status,
			))
		}
	}
	d.errors = nil
	return incident
}
