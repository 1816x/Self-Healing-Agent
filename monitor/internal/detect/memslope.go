package detect

import (
	"fmt"
	"time"
)

// MemorySlope fires when a gauge (the demo app's cache-item count) grows
// at a sustained rate across its window — the scrape-driven analog of
// ErrorRate's event-driven sliding window, applied to a continuous value
// instead of discrete events.
type MemorySlope struct {
	Window     time.Duration
	RatePerSec float64 // minimum sustained growth rate to count as "leak-like"
	MinSamples int     // minimum points in the window before trusting the slope

	samples []gaugeSample
}

type gaugeSample struct {
	t time.Time
	v float64
}

// Observe feeds one gauge reading through the detector. Old samples age
// out of Window the same way ErrorRate expires old error events — a
// value that stopped climbing naturally stops looking like a leak once
// its growth period scrolls out of the window, with no separate
// "resolved" logic needed.
func (d *MemorySlope) Observe(t time.Time, value float64) *Incident {
	cutoff := t.Add(-d.Window)
	kept := d.samples[:0]
	for _, s := range d.samples {
		if s.t.After(cutoff) {
			kept = append(kept, s)
		}
	}
	d.samples = append(kept, gaugeSample{t: t, v: value})

	if len(d.samples) < d.MinSamples {
		return nil
	}

	slope := leastSquaresSlope(d.samples)
	if slope < d.RatePerSec {
		return nil
	}

	first, last := d.samples[0], d.samples[len(d.samples)-1]
	incident := &Incident{
		Kind:        "memory_growth",
		DedupKey:    "memory_growth",
		WindowStart: first.t,
		WindowEnd:   last.t,
		Summary: fmt.Sprintf("cache grew from %.0f to %.0f items (%.2f/s) over %s",
			first.v, last.v, slope, last.t.Sub(first.t).Round(time.Second)),
		Metrics: map[string]float64{
			"slope_per_sec": slope,
			"start_value":   first.v,
			"end_value":     last.v,
		},
	}
	d.samples = nil // needs a fresh sustained run before firing again, same reset-based debounce as ErrorRate
	return incident
}

// leastSquaresSlope fits a line to (time-since-first-sample, value) and
// returns its slope in units-per-second. Least squares over the whole
// window is used instead of a naive (last-first)/duration comparison
// because two noisy endpoint samples can suggest growth (or hide it) even
// when the overall trend is flat — the regression is robust to exactly
// that.
func leastSquaresSlope(samples []gaugeSample) float64 {
	n := float64(len(samples))
	t0 := samples[0].t
	var sumT, sumV, sumTT, sumTV float64
	for _, s := range samples {
		t := s.t.Sub(t0).Seconds()
		sumT += t
		sumV += s.v
		sumTT += t * t
		sumTV += t * s.v
	}
	denom := n*sumTT - sumT*sumT
	if denom == 0 {
		return 0
	}
	return (n*sumTV - sumT*sumV) / denom
}
