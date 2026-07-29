package scrape

import (
	"regexp"
	"strconv"
	"strings"
)

var (
	// Captures: 1=metric name, 3=label content (without braces, may be
	// absent), 4=value. Values can be plain floats or scientific notation
	// ("2.36e+08") — never a bare +Inf/NaN in practice for our own app,
	// but strconv.ParseFloat accepts those too if it ever came to that.
	lineRe = regexp.MustCompile(`^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)\s*$`)

	// key="value" pairs inside a label block. Doesn't handle backslash
	// escapes beyond the simple \" case — acceptable because we only ever
	// parse output from our own app/metrics.py, whose label values are
	// plain route strings.
	labelRe = regexp.MustCompile(`([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"`)
)

// Parse reads Prometheus text-exposition output and extracts exactly the
// latency histogram and cache gauge — every other metric family (Python
// process/GC stats, our own request/error counters, which the log-based
// error-rate detector already covers) is ignored by construction: nothing
// else matches the two exact metric names below.
func Parse(text string) Snapshot {
	snap := Snapshot{Latency: map[RouteKey]Histogram{}}
	buckets := map[RouteKey]map[string]float64{}
	sums := map[RouteKey]float64{}
	counts := map[RouteKey]float64{}
	seenRoutes := map[RouteKey]bool{}

	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		m := lineRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		name, labelsRaw, valueStr := m[1], m[3], m[4]
		value, err := strconv.ParseFloat(valueStr, 64)
		if err != nil {
			continue
		}
		labels := parseLabels(labelsRaw)

		switch name {
		case latencyMetric + "_bucket":
			key := RouteKey{Route: labels["route"], Method: labels["method"]}
			if buckets[key] == nil {
				buckets[key] = map[string]float64{}
			}
			buckets[key][labels["le"]] = value
			seenRoutes[key] = true
		case latencyMetric + "_sum":
			key := RouteKey{Route: labels["route"], Method: labels["method"]}
			sums[key] = value
			seenRoutes[key] = true
		case latencyMetric + "_count":
			key := RouteKey{Route: labels["route"], Method: labels["method"]}
			counts[key] = value
			seenRoutes[key] = true
		case cacheMetric:
			snap.CacheItems = value
			snap.HasCacheItems = true
		}
	}

	for key := range seenRoutes {
		snap.Latency[key] = Histogram{
			Buckets: buckets[key],
			Sum:     sums[key],
			Count:   counts[key],
		}
	}
	return snap
}

func parseLabels(raw string) map[string]string {
	labels := map[string]string{}
	if raw == "" {
		return labels
	}
	for _, m := range labelRe.FindAllStringSubmatch(raw, -1) {
		labels[m[1]] = m[2]
	}
	return labels
}
