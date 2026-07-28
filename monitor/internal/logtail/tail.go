// Package logtail follows the demo app's JSONL log file and turns newly
// appended lines into LogEvents for the detectors.
//
// Plain polling instead of inotify: the demo app writes a handful of lines
// per second at most, so a sub-second poll is indistinguishable from
// instant — and it keeps the monitor stdlib-only and portable.
package logtail

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"time"
)

type Tailer struct {
	path     string
	interval time.Duration
	offset   int64
	partial  []byte

	// Malformed counts lines that failed to parse; the daemon reports it
	// instead of dying, so one corrupt line can't take monitoring down.
	Malformed int
}

func New(path string, interval time.Duration) *Tailer {
	return &Tailer{path: path, interval: interval}
}

// SkipToEnd moves the tailer past everything already in the file, so a
// monitor started mid-run only reports what happens after it started.
func (t *Tailer) SkipToEnd() error {
	info, err := os.Stat(t.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	t.offset = info.Size()
	return nil
}

// Poll reads any complete new lines appended since the last call and
// parses them. A missing file is not an error (the app may not have
// started yet), and a shrunken file is treated as rotation: reading
// restarts from the top.
func (t *Tailer) Poll() ([]LogEvent, error) {
	f, err := os.Open(t.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()

	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if info.Size() < t.offset {
		t.offset = 0
		t.partial = nil
	}

	if _, err := f.Seek(t.offset, io.SeekStart); err != nil {
		return nil, err
	}
	data, err := io.ReadAll(f)
	if err != nil {
		return nil, err
	}
	t.offset += int64(len(data))

	buf := append(t.partial, data...)
	lines := bytes.Split(buf, []byte("\n"))
	// The final element is an incomplete line (or empty); keep a copy for
	// the next poll rather than a subslice pinning the whole buffer.
	t.partial = append([]byte(nil), lines[len(lines)-1]...)
	lines = lines[:len(lines)-1]

	var events []LogEvent
	for _, line := range lines {
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		event, err := ParseLine(line)
		if err != nil {
			t.Malformed++
			continue
		}
		events = append(events, event)
	}
	return events, nil
}

// Run polls until the context is cancelled, sending parsed events to out.
func (t *Tailer) Run(ctx context.Context, out chan<- LogEvent) error {
	ticker := time.NewTicker(t.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			events, err := t.Poll()
			if err != nil {
				return err
			}
			for _, event := range events {
				select {
				case out <- event:
				case <-ctx.Done():
					return ctx.Err()
				}
			}
		}
	}
}
