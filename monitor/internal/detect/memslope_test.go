package detect

import (
	"testing"
	"time"
)

func TestLeastSquaresSlopeExactLine(t *testing.T) {
	// y = 2x + 5 exactly: slope must come back as 2, regardless of noise.
	samples := []gaugeSample{
		{t: t0, v: 5},
		{t: t0.Add(time.Second), v: 7},
		{t: t0.Add(2 * time.Second), v: 9},
		{t: t0.Add(3 * time.Second), v: 11},
	}
	slope := leastSquaresSlope(samples)
	if diff := slope - 2; diff > 1e-9 || diff < -1e-9 {
		t.Errorf("slope = %v, want 2", slope)
	}
}

func TestMemorySlopeBelowMinSamplesStaysQuiet(t *testing.T) {
	d := &MemorySlope{Window: time.Minute, RatePerSec: 0.5, MinSamples: 5}
	for i := 0; i < 4; i++ {
		if inc := d.Observe(t0.Add(time.Duration(i)*time.Second), float64(i)*10); inc != nil {
			t.Fatalf("fired below MinSamples at sample %d", i+1)
		}
	}
}

func TestMemorySlopeFiresOnSustainedGrowth(t *testing.T) {
	d := &MemorySlope{Window: time.Minute, RatePerSec: 1.0, MinSamples: 5}
	var incident *Incident
	// +5 items every second for 5 seconds: 5 items/sec, well above the 1/s threshold.
	for i := 0; i < 5; i++ {
		incident = d.Observe(t0.Add(time.Duration(i)*time.Second), float64(i)*5)
	}
	if incident == nil {
		t.Fatal("expected an incident on sustained growth")
	}
	if incident.Kind != "memory_growth" || incident.DedupKey != "memory_growth" {
		t.Errorf("kind/dedup key = %q/%q", incident.Kind, incident.DedupKey)
	}
	if incident.Metrics["slope_per_sec"] < 1.0 {
		t.Errorf("slope_per_sec = %v, want >= 1.0", incident.Metrics["slope_per_sec"])
	}
	if incident.Metrics["start_value"] != 0 || incident.Metrics["end_value"] != 20 {
		t.Errorf("start/end = %v/%v, want 0/20", incident.Metrics["start_value"], incident.Metrics["end_value"])
	}
}

func TestMemorySlopeNoisyButFlatDoesNotFire(t *testing.T) {
	d := &MemorySlope{Window: time.Minute, RatePerSec: 1.0, MinSamples: 5}
	// Oscillates around 50 with no net trend; a naive (last-first)/duration
	// comparison using just the endpoints (50 -> 52) would wrongly suggest
	// growth. The regression over all 6 points must not be fooled by that.
	values := []float64{50, 55, 45, 53, 47, 52}
	var incident *Incident
	for i, v := range values {
		incident = d.Observe(t0.Add(time.Duration(i)*time.Second), v)
	}
	if incident != nil {
		t.Fatalf("noisy-but-flat data must not fire, got %v", incident)
	}
}

func TestMemorySlopeOldSamplesExpireFromWindow(t *testing.T) {
	// MinSamples set high enough that Observe never evaluates (or resets)
	// during this test — isolates the window-expiry mechanism itself from
	// the firing/reset behavior already covered by other tests.
	d := &MemorySlope{Window: 10 * time.Second, RatePerSec: 1.0, MinSamples: 100}
	d.Observe(t0, 0)
	d.Observe(t0.Add(time.Second), 10)
	d.Observe(t0.Add(2*time.Second), 20)
	if len(d.samples) != 3 {
		t.Fatalf("expected 3 buffered samples, got %d", len(d.samples))
	}

	// A reading long after Window has passed must drop the stale ones.
	d.Observe(t0.Add(time.Minute), 20)
	if len(d.samples) != 1 {
		t.Fatalf("stale samples outside the window must be dropped, got %d buffered", len(d.samples))
	}
}

func TestMemorySlopeResetsAfterFiring(t *testing.T) {
	d := &MemorySlope{Window: time.Minute, RatePerSec: 1.0, MinSamples: 3}
	d.Observe(t0, 0)
	d.Observe(t0.Add(time.Second), 10)
	if inc := d.Observe(t0.Add(2*time.Second), 20); inc == nil {
		t.Fatal("expected an incident at MinSamples with strong growth")
	}
	// Immediately after firing, the sample buffer is empty — a single
	// flat reading must not be enough to fire again.
	if inc := d.Observe(t0.Add(3*time.Second), 20); inc != nil {
		t.Fatalf("detector must reset after firing, not refire on the next sample, got %v", inc)
	}
}
