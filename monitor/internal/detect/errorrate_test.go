package detect

import (
	"testing"
	"time"

	"github.com/1816x/self-healing-agent/monitor/internal/logtail"
)

var t0 = time.Date(2026, 7, 28, 12, 0, 0, 0, time.UTC)

func errorAt(ts time.Time, route string) logtail.LogEvent {
	return logtail.LogEvent{
		TS: ts, Level: "error", Event: "request.completed",
		Route: route, Method: "POST", Status: 500,
	}
}

func infoAt(ts time.Time) logtail.LogEvent {
	return logtail.LogEvent{
		TS: ts, Level: "info", Event: "request.completed",
		Route: "/health", Method: "GET", Status: 200,
	}
}

func TestBelowThresholdStaysQuiet(t *testing.T) {
	d := &ErrorRate{Window: time.Minute, Threshold: 5}
	for i := 0; i < 4; i++ {
		if inc := d.Observe(errorAt(t0.Add(time.Duration(i)*time.Second), "/checkout")); inc != nil {
			t.Fatalf("fired at %d errors, threshold is 5", i+1)
		}
	}
}

func TestFiresAtThresholdWithEvidence(t *testing.T) {
	d := &ErrorRate{Window: time.Minute, Threshold: 3}
	var incident *Incident
	for i := 0; i < 3; i++ {
		incident = d.Observe(errorAt(t0.Add(time.Duration(i)*time.Second), "/checkout"))
	}
	if incident == nil {
		t.Fatal("expected incident at threshold")
	}
	if incident.Kind != "error_rate" {
		t.Errorf("kind = %q", incident.Kind)
	}
	if incident.DedupKey != "error_rate" {
		t.Errorf("dedup key = %q", incident.DedupKey)
	}
	if incident.Metrics["error_count"] != 3 {
		t.Errorf("error count = %v, want 3", incident.Metrics["error_count"])
	}
	if incident.Routes["/checkout"] != 3 {
		t.Errorf("route counts = %v", incident.Routes)
	}
	if len(incident.Samples) != 3 {
		t.Errorf("samples = %d, want 3", len(incident.Samples))
	}
	if !incident.WindowEnd.After(incident.WindowStart) {
		t.Errorf("window [%v, %v] is not ordered", incident.WindowStart, incident.WindowEnd)
	}
}

func TestOldErrorsExpireFromWindow(t *testing.T) {
	d := &ErrorRate{Window: time.Minute, Threshold: 3}
	d.Observe(errorAt(t0, "/checkout"))
	d.Observe(errorAt(t0.Add(time.Second), "/checkout"))
	// Third error arrives 2 minutes later — the first two are stale.
	if inc := d.Observe(errorAt(t0.Add(2*time.Minute), "/checkout")); inc != nil {
		t.Fatal("stale errors outside the window must not count toward the threshold")
	}
}

func TestNonErrorsNeverCount(t *testing.T) {
	d := &ErrorRate{Window: time.Minute, Threshold: 2}
	d.Observe(errorAt(t0, "/checkout"))
	for i := 1; i <= 10; i++ {
		if inc := d.Observe(infoAt(t0.Add(time.Duration(i) * time.Second))); inc != nil {
			t.Fatal("info events must not trigger the detector")
		}
	}
}

func TestResetsAfterFiring(t *testing.T) {
	d := &ErrorRate{Window: time.Minute, Threshold: 2}
	d.Observe(errorAt(t0, "/checkout"))
	if inc := d.Observe(errorAt(t0.Add(time.Second), "/checkout")); inc == nil {
		t.Fatal("expected incident at threshold")
	}
	// The very next error must start counting from zero again.
	if inc := d.Observe(errorAt(t0.Add(2*time.Second), "/checkout")); inc != nil {
		t.Fatal("detector must reset after firing, not refire per error")
	}
}
