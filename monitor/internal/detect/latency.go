package detect

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"time"

	"github.com/1816x/self-healing-agent/monitor/internal/scrape"
)

// Latency fires when a route's p95 latency — computed from the delta
// between two consecutive histogram scrapes — sustains above Threshold
// for MinBreaches consecutive scrapes.
//
// This is scrape-driven, not event-driven like ErrorRate: there's no
// discrete "latency event" to accumulate within a time.Duration window,
// only periodic cumulative snapshots. MinBreaches (a count of consecutive
// over-threshold scrapes) plays the debounce role that Window/Threshold
// play for ErrorRate — a genuinely different shape for a genuinely
// different kind of signal, not an inconsistency.
type Latency struct {
	Threshold   time.Duration
	MinBreaches int
	// MinSamples guards against trusting a p95 estimate computed from
	// almost no traffic — 1 slow request out of 1 is a "100% over
	// threshold" reading that means nothing.
	MinSamples float64

	prev        map[scrape.RouteKey]scrape.Histogram
	havePrev    bool
	breaches    map[scrape.RouteKey]int
	windowStart map[scrape.RouteKey]time.Time
}

// Observe consumes one scrape's per-route histograms and returns any
// incidents that just crossed MinBreaches. t is the scrape's own
// timestamp, not wall-clock time, so tests stay deterministic.
func (d *Latency) Observe(t time.Time, cur map[scrape.RouteKey]scrape.Histogram) []*Incident {
	if d.breaches == nil {
		d.breaches = map[scrape.RouteKey]int{}
		d.windowStart = map[scrape.RouteKey]time.Time{}
	}
	if !d.havePrev {
		d.prev = cur
		d.havePrev = true
		return nil
	}

	var incidents []*Incident
	for key, curHist := range cur {
		prevHist, existed := d.prev[key]
		if !existed {
			continue // route appeared this cycle; no baseline to diff against yet
		}
		deltaBuckets, ok := bucketDelta(prevHist, curHist)
		if !ok {
			delete(d.breaches, key) // counter reset (app restart) — forget any partial streak
			continue
		}
		p95, count := quantile(deltaBuckets, 0.95)
		if count < d.MinSamples || p95 <= d.Threshold {
			delete(d.breaches, key)
			continue
		}

		if d.breaches[key] == 0 {
			d.windowStart[key] = t
		}
		d.breaches[key]++
		if d.breaches[key] < d.MinBreaches {
			continue
		}

		incidents = append(incidents, &Incident{
			Kind:        "latency_p95",
			DedupKey:    "latency_p95:" + key.Route,
			WindowStart: d.windowStart[key],
			WindowEnd:   t,
			Summary: fmt.Sprintf("p95 %s on %s %s (threshold %s)",
				p95.Round(time.Millisecond), key.Method, key.Route, d.Threshold),
			Metrics: map[string]float64{
				"p95_ms":       float64(p95.Milliseconds()),
				"threshold_ms": float64(d.Threshold.Milliseconds()),
				"sample_count": count,
			},
			Routes: map[string]int{key.Route: int(count)},
		})
		d.breaches[key] = 0 // needs a fresh run of MinBreaches before refiring
	}

	d.prev = cur
	return incidents
}

// bucketDelta computes per-interval bucket counts from two cumulative
// histograms. A bucket whose count decreased means the process restarted
// (Prometheus counters reset to zero) — that invalidates the whole
// interval rather than producing a nonsensical negative count.
func bucketDelta(prev, cur scrape.Histogram) (map[string]float64, bool) {
	d := make(map[string]float64, len(cur.Buckets))
	for le, curCount := range cur.Buckets {
		prevCount, existed := prev.Buckets[le]
		if !existed || curCount < prevCount {
			return nil, false
		}
		d[le] = curCount - prevCount
	}
	return d, true
}

type bucketBound struct {
	le    string
	upper float64
}

// sortedBounds orders bucket boundaries ascending by numeric value,
// treating "+Inf" as +infinity. Boundaries are derived from whatever
// buckets are present in the data rather than hardcoded to
// app/metrics.py's current bucket list, so the detector doesn't silently
// drift out of sync if that list ever changes.
func sortedBounds(buckets map[string]float64) []bucketBound {
	bounds := make([]bucketBound, 0, len(buckets))
	for le := range buckets {
		upper := math.Inf(1)
		if le != "+Inf" {
			if v, err := strconv.ParseFloat(le, 64); err == nil {
				upper = v
			}
		}
		bounds = append(bounds, bucketBound{le: le, upper: upper})
	}
	sort.Slice(bounds, func(i, j int) bool { return bounds[i].upper < bounds[j].upper })
	return bounds
}

// quantile estimates the qth percentile from delta bucket counts using
// the same linear interpolation Prometheus's histogram_quantile applies
// within a bucket, and returns the total observation count alongside it.
func quantile(deltaBuckets map[string]float64, q float64) (time.Duration, float64) {
	bounds := sortedBounds(deltaBuckets)
	if len(bounds) == 0 {
		return 0, 0
	}
	total := deltaBuckets[bounds[len(bounds)-1].le] // count at +Inf == total observations
	if total <= 0 {
		return 0, 0
	}
	target := q * total

	lowerBound, lowerCount := 0.0, 0.0
	for _, b := range bounds {
		count := deltaBuckets[b.le]
		if count >= target {
			if math.IsInf(b.upper, 1) {
				// All mass sits beyond the last finite bucket. Real
				// Prometheus returns +Inf here; we report the last
				// finite boundary as a conservative floor instead —
				// "detected, latency underestimated" beats "detected,
				// latency unknown" for an operator deciding whether to
				// care.
				return secondsToDuration(lowerBound), total
			}
			if count == lowerCount {
				return secondsToDuration(b.upper), total
			}
			frac := (target - lowerCount) / (count - lowerCount)
			return secondsToDuration(lowerBound + frac*(b.upper-lowerBound)), total
		}
		lowerBound, lowerCount = b.upper, count
	}
	return secondsToDuration(lowerBound), total
}

func secondsToDuration(s float64) time.Duration {
	return time.Duration(s * float64(time.Second))
}
