package logtail

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

const validLine = `{"ts": "2026-07-28T16:34:06Z", "level": "info", "event": "request.completed", "route": "/health", "method": "GET", "status": 200, "duration_ms": 1.0}` + "\n"

func tempLog(t *testing.T) (string, *os.File) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "demo-app.jsonl")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	return path, f
}

func TestPollMissingFileIsNotAnError(t *testing.T) {
	tailer := New(filepath.Join(t.TempDir(), "nope.jsonl"), time.Millisecond)
	events, err := tailer.Poll()
	if err != nil {
		t.Fatalf("missing file must not error: %v", err)
	}
	if events != nil {
		t.Errorf("expected no events, got %d", len(events))
	}
}

func TestPollReadsOnlyNewCompleteLines(t *testing.T) {
	path, f := tempLog(t)
	tailer := New(path, time.Millisecond)

	// Two complete lines plus the start of a third.
	f.WriteString(validLine + validLine + `{"ts": "2026-07-`)
	events, err := tailer.Poll()
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}

	// Completing the partial line yields exactly one more event.
	f.WriteString(`28T16:34:07Z", "level": "info", "event": "x"}` + "\n")
	events, err = tailer.Poll()
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event after completing partial line, got %d", len(events))
	}
	if tailer.Malformed != 0 {
		t.Errorf("no line was malformed, counter says %d", tailer.Malformed)
	}
}

func TestPollSkipsMalformedAndCounts(t *testing.T) {
	path, f := tempLog(t)
	tailer := New(path, time.Millisecond)

	f.WriteString("garbage line\n" + validLine)
	events, err := tailer.Poll()
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 {
		t.Fatalf("expected the valid event only, got %d", len(events))
	}
	if tailer.Malformed != 1 {
		t.Errorf("expected 1 malformed line counted, got %d", tailer.Malformed)
	}
}

func TestPollHandlesRotation(t *testing.T) {
	path, f := tempLog(t)
	tailer := New(path, time.Millisecond)

	f.WriteString(validLine + validLine)
	if _, err := tailer.Poll(); err != nil {
		t.Fatal(err)
	}

	// Rotation: file replaced with something shorter than the old offset.
	f.Close()
	if err := os.WriteFile(path, []byte(validLine), 0o644); err != nil {
		t.Fatal(err)
	}
	events, err := tailer.Poll()
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event from rotated file, got %d", len(events))
	}
}

func TestSkipToEnd(t *testing.T) {
	path, f := tempLog(t)
	f.WriteString(validLine + validLine)

	tailer := New(path, time.Millisecond)
	if err := tailer.SkipToEnd(); err != nil {
		t.Fatal(err)
	}
	events, err := tailer.Poll()
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Fatalf("SkipToEnd must ignore pre-existing lines, got %d", len(events))
	}

	f.WriteString(validLine)
	events, err = tailer.Poll()
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 {
		t.Fatalf("expected only the line written after SkipToEnd, got %d", len(events))
	}
}
