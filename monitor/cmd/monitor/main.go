// Command monitor tails the demo app's JSON logs, runs the anomaly
// detectors over the event stream, and writes fired incidents to the
// shared SQLite store that the diagnosis agent (Phase 3) and dashboard
// (Phase 5) read from.
package main

import (
	"context"
	"flag"
	"log"
	"os/signal"
	"syscall"
	"time"

	"github.com/1816x/self-healing-agent/monitor/internal/detect"
	"github.com/1816x/self-healing-agent/monitor/internal/logtail"
	"github.com/1816x/self-healing-agent/monitor/internal/store"
)

func main() {
	logFile := flag.String("log-file", "demo-app/logs/demo-app.jsonl", "JSONL log file to tail")
	dbPath := flag.String("db", "incidents.db", "SQLite incident store")
	window := flag.Duration("window", time.Minute, "sliding window for the error-rate detector")
	threshold := flag.Int("threshold", 5, "errors within the window that fire an incident")
	poll := flag.Duration("poll", 250*time.Millisecond, "log poll interval")
	flag.Parse()

	incidents, err := store.Open(*dbPath)
	if err != nil {
		log.Fatalf("open incident store: %v", err)
	}
	defer incidents.Close()

	tailer := logtail.New(*logFile, *poll)
	if err := tailer.SkipToEnd(); err != nil {
		log.Fatalf("skip to end of %s: %v", *logFile, err)
	}

	detector := &detect.ErrorRate{Window: *window, Threshold: *threshold}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	events := make(chan logtail.LogEvent, 256)
	go func() {
		defer close(events)
		if err := tailer.Run(ctx, events); err != nil && ctx.Err() == nil {
			log.Printf("tailer stopped: %v", err)
		}
	}()

	log.Printf("monitor started: tailing %s, incidents -> %s (window=%s threshold=%d)",
		*logFile, *dbPath, *window, *threshold)

	for event := range events {
		incident := detector.Observe(event)
		if incident == nil {
			continue
		}
		id, err := incidents.InsertIncident(incident)
		if err != nil {
			log.Printf("ALERT dropped, insert failed: %v", err)
			continue
		}
		log.Printf("ALERT incident #%d: %s — %s [%s .. %s]",
			id, incident.Kind, incident.Summary,
			incident.WindowStart.Format(time.RFC3339),
			incident.WindowEnd.Format(time.RFC3339))
	}
}
