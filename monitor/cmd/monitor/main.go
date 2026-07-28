// Command monitor is the Go daemon that will tail the demo app's logs and
// scrape its metrics, detect anomalies, and write incidents to SQLite.
//
// Phase 0: compiling stub only. Detection logic lands in Phase 1 (log-based)
// and Phase 2 (metric-based).
package main

import "fmt"

func main() {
	fmt.Println("monitor: scaffolding only, detectors not implemented yet (see PLAN.md)")
}
