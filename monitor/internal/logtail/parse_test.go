package logtail

import (
	"strings"
	"testing"
)

// A real line captured from demo-app/app/logging.py output — if the Python
// side changes format, this is the test that should break first.
const sampleLine = `{"ts": "2026-07-28T16:34:06Z", "level": "info", "event": "request.completed", "route": "/checkout", "method": "POST", "status": 200, "duration_ms": 1.72}`

func TestParseLineRealSample(t *testing.T) {
	event, err := ParseLine([]byte(sampleLine))
	if err != nil {
		t.Fatalf("ParseLine failed on real sample: %v", err)
	}
	if event.Route != "/checkout" || event.Method != "POST" || event.Status != 200 {
		t.Errorf("unexpected fields: %+v", event)
	}
	if event.TS.IsZero() {
		t.Error("timestamp not parsed")
	}
	if event.IsError() {
		t.Error("a 200 info line must not count as an error")
	}
}

func TestParseLineErrorEvent(t *testing.T) {
	line := `{"ts": "2026-07-28T16:34:06Z", "level": "error", "event": "request.completed", "route": "/checkout", "method": "POST", "status": 500, "duration_ms": 3.1}`
	event, err := ParseLine([]byte(line))
	if err != nil {
		t.Fatalf("ParseLine failed: %v", err)
	}
	if !event.IsError() {
		t.Error("error-level 500 line must count as an error")
	}
}

func TestParseLineUnknownFieldsIgnored(t *testing.T) {
	line := `{"ts": "2026-07-28T16:34:06Z", "level": "info", "event": "checkout.ok", "total_cents": 6998}`
	if _, err := ParseLine([]byte(line)); err != nil {
		t.Errorf("extra context fields must not fail parsing: %v", err)
	}
}

func TestParseLineRejects(t *testing.T) {
	cases := map[string]string{
		"not json":       `request completed route=/checkout`,
		"missing ts":     `{"level": "info", "event": "x"}`,
		"missing level":  `{"ts": "2026-07-28T16:34:06Z", "event": "x"}`,
		"missing event":  `{"ts": "2026-07-28T16:34:06Z", "level": "info"}`,
		"unparseable ts": `{"ts": "yesterday", "level": "info", "event": "x"}`,
		"json array":     `[1, 2, 3]`,
	}
	for name, line := range cases {
		if _, err := ParseLine([]byte(line)); err == nil {
			t.Errorf("%s: expected error, got none", name)
		} else if strings.Contains(err.Error(), "panic") {
			t.Errorf("%s: parser must fail cleanly: %v", name, err)
		}
	}
}
