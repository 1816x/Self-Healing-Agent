// Command monitor tails the demo app's JSON logs and scrapes its
// metrics, runs the anomaly detectors over both streams, and writes
// fired incidents to the shared SQLite store that the diagnosis agent
// (Phase 3) and dashboard (Phase 5) read from.
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
	"github.com/1816x/self-healing-agent/monitor/internal/scrape"
	"github.com/1816x/self-healing-agent/monitor/internal/store"
)

func main() {
	logFile := flag.String("log-file", "demo-app/logs/demo-app.jsonl", "JSONL log file to tail")
	dbPath := flag.String("db", "incidents.db", "SQLite incident store")
	window := flag.Duration("window", time.Minute, "sliding window for the error-rate detector")
	threshold := flag.Int("threshold", 5, "errors within the window that fire an incident")
	poll := flag.Duration("poll", 250*time.Millisecond, "log poll interval")
	dedupWindow := flag.Duration("dedup-window", 2*time.Minute,
		"how long an ongoing condition merges into the same incident row instead of opening a new one")

	scrapeURL := flag.String("scrape-url", "http://127.0.0.1:8000/metrics", "demo app /metrics endpoint")
	scrapeInterval := flag.Duration("scrape-interval", 2*time.Second, "metrics scrape interval")
	latencyThreshold := flag.Duration("latency-threshold", 100*time.Millisecond, "p95 latency that counts as a breach")
	latencyMinBreaches := flag.Int("latency-min-breaches", 2, "consecutive over-threshold scrapes before firing")
	latencyMinSamples := flag.Float64("latency-min-samples", 3, "minimum requests in a scrape interval to trust its p95")
	memWindow := flag.Duration("mem-window", 20*time.Second, "lookback window for the memory-growth detector")
	memRatePerSec := flag.Float64("mem-rate", 0.5, "minimum sustained cache growth (items/sec) to count as a leak")
	memMinSamples := flag.Int("mem-min-samples", 5, "minimum scrapes in the window before trusting the slope")
	flag.Parse()

	incidents, err := store.Open(*dbPath)
	if err != nil {
		log.Fatalf("open incident store: %v", err)
	}
	defer incidents.Close()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	report := func(incident *detect.Incident) {
		if incident == nil {
			return
		}
		id, merged, err := incidents.UpsertIncident(incident, *dedupWindow)
		if err != nil {
			log.Printf("ALERT dropped, upsert failed: %v", err)
			return
		}
		verb := "new"
		if merged {
			verb = "ongoing"
		}
		log.Printf("ALERT incident #%d (%s): %s — %s [%s .. %s]",
			id, verb, incident.Kind, incident.Summary,
			incident.WindowStart.Format(time.RFC3339),
			incident.WindowEnd.Format(time.RFC3339))
	}

	// --- log tailer: error-rate detection ---
	tailer := logtail.New(*logFile, *poll)
	if err := tailer.SkipToEnd(); err != nil {
		log.Fatalf("skip to end of %s: %v", *logFile, err)
	}
	errorRate := &detect.ErrorRate{Window: *window, Threshold: *threshold}

	events := make(chan logtail.LogEvent, 256)
	go func() {
		defer close(events)
		if err := tailer.Run(ctx, events); err != nil && ctx.Err() == nil {
			log.Printf("tailer stopped: %v", err)
		}
	}()

	// --- metrics scraper: latency + memory-growth detection ---
	latency := &detect.Latency{
		Threshold: *latencyThreshold, MinBreaches: *latencyMinBreaches, MinSamples: *latencyMinSamples,
	}
	memGrowth := &detect.MemorySlope{Window: *memWindow, RatePerSec: *memRatePerSec, MinSamples: *memMinSamples}

	scrapeDone := make(chan struct{})
	go func() {
		defer close(scrapeDone)
		ticker := time.NewTicker(*scrapeInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				// Scraping is best-effort on a timer: the app may not be
				// listening yet, or a single request may time out. A
				// failed scrape just means this cycle contributes no
				// data, not a fatal error — same tolerance as the log
				// tailer's handling of a not-yet-created file.
				snap, err := scrape.Fetch(*scrapeURL)
				if err != nil {
					continue
				}
				now := time.Now()
				for _, incident := range latency.Observe(now, snap.Latency) {
					report(incident)
				}
				if snap.HasCacheItems {
					report(memGrowth.Observe(now, snap.CacheItems))
				}
			}
		}
	}()

	log.Printf("monitor started: tailing %s, scraping %s every %s, incidents -> %s",
		*logFile, *scrapeURL, *scrapeInterval, *dbPath)

	for event := range events {
		report(errorRate.Observe(event))
	}
	<-scrapeDone
}
