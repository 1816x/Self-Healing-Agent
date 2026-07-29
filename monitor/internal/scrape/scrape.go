// Package scrape reads the demo app's /metrics endpoint and extracts the
// two signals the Phase 2 detectors need: the request-latency histogram
// and the cache-size gauge. It is not a general Prometheus client — the
// app and the monitor are written by the same hands, so the parser is
// scoped to exactly the exposition shape app/metrics.py produces, not
// the full text format (see docs/design-decisions.md).
package scrape

import (
	"fmt"
	"io"
	"net/http"
	"time"
)

// RouteKey identifies one route+method combination in the latency histogram.
type RouteKey struct {
	Route  string
	Method string
}

// Histogram holds one route's cumulative bucket counts as reported by a
// single scrape. Prometheus histograms never reset while the process
// runs, so a single Histogram on its own says nothing about *recent*
// latency — detectors need the delta between two consecutive snapshots.
type Histogram struct {
	// Buckets maps each "le" boundary, exactly as Prometheus renders it
	// (e.g. "0.005", "+Inf"), to the cumulative count at or below it.
	Buckets map[string]float64
	Sum     float64
	Count   float64
}

// Snapshot is one scrape's worth of the metrics this monitor cares about.
type Snapshot struct {
	Latency       map[RouteKey]Histogram
	CacheItems    float64
	HasCacheItems bool
}

const (
	latencyMetric = "demo_app_request_latency_seconds"
	cacheMetric   = "demo_app_cache_items"
)

var httpClient = &http.Client{Timeout: 3 * time.Second}

// Fetch scrapes url and parses the response. Any failure — the app isn't
// listening yet, a bad status code, a body that isn't valid exposition
// text — comes back as a plain error; scraping is inherently best-effort
// on a timer, so the caller is expected to skip the cycle rather than
// treat a single failed scrape as fatal (same tolerance as the log
// tailer's handling of a not-yet-created log file).
func Fetch(url string) (Snapshot, error) {
	resp, err := httpClient.Get(url)
	if err != nil {
		return Snapshot{}, fmt.Errorf("scrape %s: %w", url, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return Snapshot{}, fmt.Errorf("scrape %s: status %d", url, resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return Snapshot{}, fmt.Errorf("scrape %s: read body: %w", url, err)
	}
	return Parse(string(body)), nil
}
