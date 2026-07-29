// Package detect holds the monitor's anomaly detectors. Phase 1 shipped a
// single error-rate detector with an Incident shape built just for it;
// Phase 2 adds two more detector kinds (latency, memory growth), so the
// shape generalizes here first — see docs/design-decisions.md.
package detect

import (
	"fmt"
	"time"

	"github.com/1816x/self-healing-agent/monitor/internal/logtail"
)

// Incident is what a detector hands to the store when it fires.
//
// Metrics is a flat, kind-specific bag (error_rate uses "error_count";
// latency uses "p95_ms"/"threshold_ms"; memory_growth uses
// "slope_per_sec") rather than one struct per kind — it's what lets the
// store treat every kind identically for dedup/merge (Phase 2) without a
// type switch.
//
// DedupKey identifies "the same ongoing condition" for the store's
// dedup/cooldown logic. Kind alone isn't specific enough once a detector
// can fire for more than one route at a time (latency): DedupKey carries
// that distinction, e.g. "latency_p95:/products" vs "latency_p95:/checkout".
type Incident struct {
	Kind        string
	DedupKey    string
	WindowStart time.Time
	WindowEnd   time.Time
	Summary     string
	Metrics     map[string]float64
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
// After firing, the buffer resets so the detector needs a fresh run of
// Threshold errors before firing again; the store's dedup window (Phase 2)
// is what collapses repeated firings of a still-ongoing failure into one
// incident row instead of one per reset.
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

	routes := map[string]int{}
	var samples []string
	for _, e := range d.errors {
		routes[e.Route]++
		if len(samples) < maxSamples {
			samples = append(samples, fmt.Sprintf(
				"%s %s %s %s status=%d",
				e.TS.Format(time.RFC3339), e.Level, e.Event, e.Route, e.Status,
			))
		}
	}

	incident := &Incident{
		Kind:        "error_rate",
		DedupKey:    "error_rate",
		WindowStart: d.errors[0].TS,
		WindowEnd:   event.TS,
		Summary:     fmt.Sprintf("%d errors across %d route(s)", len(d.errors), len(routes)),
		Metrics:     map[string]float64{"error_count": float64(len(d.errors))},
		Routes:      routes,
		Samples:     samples,
	}
	d.errors = nil
	return incident
}
