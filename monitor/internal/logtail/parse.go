package logtail

import (
	"encoding/json"
	"fmt"
	"time"
)

// LogEvent is the Go-side contract for one line of demo-app/app/logging.py
// output. Only the fields the detectors key off are parsed; unknown fields
// are ignored so the demo app can add context without breaking the monitor.
type LogEvent struct {
	TS         time.Time
	Level      string
	Event      string
	Route      string
	Method     string
	Status     int
	DurationMS float64
}

// IsError reports whether the event should count toward error-rate
// detection: an explicit error-level line, or any 5xx response.
func (e LogEvent) IsError() bool {
	return e.Level == "error" || e.Status >= 500
}

type rawEvent struct {
	TS         string  `json:"ts"`
	Level      string  `json:"level"`
	Event      string  `json:"event"`
	Route      string  `json:"route"`
	Method     string  `json:"method"`
	Status     int     `json:"status"`
	DurationMS float64 `json:"duration_ms"`
}

// ParseLine decodes one JSONL line into a LogEvent. ts, level, and event
// are required; everything else is optional context.
func ParseLine(line []byte) (LogEvent, error) {
	var raw rawEvent
	if err := json.Unmarshal(line, &raw); err != nil {
		return LogEvent{}, fmt.Errorf("not a JSON object: %w", err)
	}
	if raw.TS == "" || raw.Level == "" || raw.Event == "" {
		return LogEvent{}, fmt.Errorf("missing required field (ts/level/event): %s", line)
	}
	ts, err := time.Parse(time.RFC3339, raw.TS)
	if err != nil {
		return LogEvent{}, fmt.Errorf("bad ts %q: %w", raw.TS, err)
	}
	return LogEvent{
		TS:         ts,
		Level:      raw.Level,
		Event:      raw.Event,
		Route:      raw.Route,
		Method:     raw.Method,
		Status:     raw.Status,
		DurationMS: raw.DurationMS,
	}, nil
}
