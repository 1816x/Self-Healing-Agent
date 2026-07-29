package detect

import (
	"testing"
	"time"

	"github.com/1816x/self-healing-agent/monitor/internal/scrape"
)

var products = scrape.RouteKey{Route: "/products", Method: "GET"}

func cumulative(buckets map[string]float64) scrape.Histogram {
	total := buckets["+Inf"]
	return scrape.Histogram{Buckets: buckets, Count: total}
}

// --- quantile()/bucketDelta() direct unit tests ---

func TestQuantileInterpolatesWithinBucket(t *testing.T) {
	// 20 total observations; 19 land at or under 0.1s, none under 0.1s
	// specifically fall in (0.1, 0.25]... construct so p95 target=19 lands
	// exactly at the 0.25 bucket boundary transition from a 0-count 0.1
	// bucket, matching the worked example in the design doc.
	buckets := map[string]float64{
		"0.005": 0, "0.01": 0, "0.025": 0, "0.05": 0, "0.1": 0,
		"0.25": 20, "1.0": 20, "+Inf": 20,
	}
	p95, total := quantile(buckets, 0.95)
	if total != 20 {
		t.Fatalf("total = %v, want 20", total)
	}
	want := 242500 * time.Microsecond // 0.1 + 0.95*(0.25-0.1) = 0.2425s
	if diff := p95 - want; diff > time.Microsecond || diff < -time.Microsecond {
		t.Errorf("p95 = %v, want ~%v", p95, want)
	}
}

func TestQuantileEmptyInterval(t *testing.T) {
	p95, total := quantile(map[string]float64{"0.1": 0, "+Inf": 0}, 0.95)
	if total != 0 || p95 != 0 {
		t.Errorf("expected zero result for an empty interval, got p95=%v total=%v", p95, total)
	}
}

func TestQuantileAllMassBeyondLastFiniteBucket(t *testing.T) {
	// Every observation exceeded the largest bucket boundary.
	p95, total := quantile(map[string]float64{"1.0": 0, "+Inf": 10}, 0.95)
	if total != 10 {
		t.Fatalf("total = %v, want 10", total)
	}
	if p95 != time.Second {
		t.Errorf("p95 = %v, want the last finite boundary (1s) as a floor", p95)
	}
}

func TestBucketDeltaDetectsCounterReset(t *testing.T) {
	prev := cumulative(map[string]float64{"0.1": 50, "+Inf": 50})
	restarted := cumulative(map[string]float64{"0.1": 3, "+Inf": 3})
	if _, ok := bucketDelta(prev, restarted); ok {
		t.Error("a decreasing bucket count must be reported as a counter reset, not a negative delta")
	}
}

// --- Latency.Observe() behavioral tests ---

func healthySnapshot(cumCount float64) map[scrape.RouteKey]scrape.Histogram {
	return map[scrape.RouteKey]scrape.Histogram{
		products: cumulative(map[string]float64{
			"0.005": cumCount, "0.01": cumCount, "0.1": cumCount, "0.25": cumCount, "+Inf": cumCount,
		}),
	}
}

// slowSnapshot adds slowAdds observations to the running total, all
// landing beyond 0.1s (between the 0.1 and 0.25 boundaries).
func slowSnapshot(base map[scrape.RouteKey]scrape.Histogram, slowAdds float64) map[scrape.RouteKey]scrape.Histogram {
	prevFast := base[products].Buckets["0.1"]
	prevTotal := base[products].Buckets["+Inf"]
	return map[scrape.RouteKey]scrape.Histogram{
		products: cumulative(map[string]float64{
			"0.005": prevFast, "0.01": prevFast, "0.1": prevFast,
			"0.25": prevTotal + slowAdds, "+Inf": prevTotal + slowAdds,
		}),
	}
}

func TestFirstObserveOnlyEstablishesBaseline(t *testing.T) {
	d := &Latency{Threshold: 100 * time.Millisecond, MinBreaches: 1, MinSamples: 1}
	if inc := d.Observe(t0, healthySnapshot(10)); inc != nil {
		t.Errorf("first Observe must not fire (no baseline yet), got %v", inc)
	}
}

func TestFiresAfterConsecutiveBreaches(t *testing.T) {
	d := &Latency{Threshold: 100 * time.Millisecond, MinBreaches: 2, MinSamples: 5}
	base := healthySnapshot(10)
	d.Observe(t0, base) // baseline

	s1 := slowSnapshot(base, 20)
	if inc := d.Observe(t0.Add(time.Second), s1); inc != nil {
		t.Fatalf("breach 1/2 must not fire yet, got %v", inc)
	}

	s2 := slowSnapshot(s1, 20)
	incidents := d.Observe(t0.Add(2*time.Second), s2)
	if len(incidents) != 1 {
		t.Fatalf("expected 1 incident at breach 2/2, got %d", len(incidents))
	}
	inc := incidents[0]
	if inc.Kind != "latency_p95" || inc.DedupKey != "latency_p95:/products" {
		t.Errorf("kind/dedup key = %q/%q", inc.Kind, inc.DedupKey)
	}
	if inc.Metrics["p95_ms"] <= 100 {
		t.Errorf("p95_ms = %v, expected > threshold", inc.Metrics["p95_ms"])
	}
}

func TestTransientSpikeDoesNotFire(t *testing.T) {
	d := &Latency{Threshold: 100 * time.Millisecond, MinBreaches: 2, MinSamples: 5}
	base := healthySnapshot(10)
	d.Observe(t0, base)

	s1 := slowSnapshot(base, 20)
	d.Observe(t0.Add(time.Second), s1) // breach 1

	// Recovers before reaching MinBreaches.
	s2 := healthySnapshot(s1[products].Buckets["+Inf"])
	if inc := d.Observe(t0.Add(2*time.Second), s2); inc != nil {
		t.Fatalf("a healthy interval must reset the streak, got %v", inc)
	}

	s3 := slowSnapshot(s2, 20)
	if inc := d.Observe(t0.Add(3*time.Second), s3); inc != nil {
		t.Fatalf("streak must restart from zero after the reset, not fire on breach 1 again, got %v", inc)
	}
}

func TestCounterResetDoesNotCrashOrFalselyFire(t *testing.T) {
	d := &Latency{Threshold: 100 * time.Millisecond, MinBreaches: 1, MinSamples: 1}
	base := slowSnapshot(healthySnapshot(10), 20)
	d.Observe(t0, base)

	restarted := healthySnapshot(2) // cumulative counts dropped: app restarted
	if inc := d.Observe(t0.Add(time.Second), restarted); inc != nil {
		t.Fatalf("a counter reset interval must not produce an incident, got %v", inc)
	}
}

func TestBelowMinSamplesDoesNotFire(t *testing.T) {
	d := &Latency{Threshold: 100 * time.Millisecond, MinBreaches: 1, MinSamples: 5}
	base := healthySnapshot(10)
	d.Observe(t0, base)

	// Only 1 slow request this interval — p95 would exceed threshold,
	// but there's not enough traffic to trust that.
	tiny := slowSnapshot(base, 1)
	if inc := d.Observe(t0.Add(time.Second), tiny); inc != nil {
		t.Fatalf("must not fire below MinSamples, got %v", inc)
	}
}

func TestNewRouteWithNoBaselineIsSkipped(t *testing.T) {
	d := &Latency{Threshold: 100 * time.Millisecond, MinBreaches: 1, MinSamples: 1}
	d.Observe(t0, healthySnapshot(10))

	checkout := scrape.RouteKey{Route: "/checkout", Method: "POST"}
	withNewRoute := healthySnapshot(11)
	withNewRoute[checkout] = cumulative(map[string]float64{"0.005": 0, "1.0": 5, "+Inf": 5})
	if inc := d.Observe(t0.Add(time.Second), withNewRoute); inc != nil {
		t.Fatalf("a route with no prior baseline must not fire on its first sighting, got %v", inc)
	}
}
