package scrape

import "testing"

// A real /metrics scrape captured from the running demo app after 3
// GET /products and 3 POST /checkout requests — including the process/GC
// noise every prometheus_client app emits, to prove the parser ignores it
// rather than merely "not having encountered" it in a hand-trimmed fixture.
const realScrape = `# HELP python_gc_objects_collected_total Objects collected during gc
# TYPE python_gc_objects_collected_total counter
python_gc_objects_collected_total{generation="0"} 370.0
# HELP process_virtual_memory_bytes Virtual memory size in bytes.
# TYPE process_virtual_memory_bytes gauge
process_virtual_memory_bytes 2.36732416e+08
# HELP demo_app_requests_total Total requests handled
# TYPE demo_app_requests_total counter
demo_app_requests_total{method="GET",route="/products"} 3.0
demo_app_requests_total{method="POST",route="/checkout"} 3.0
# HELP demo_app_requests_created Total requests handled
# TYPE demo_app_requests_created gauge
demo_app_requests_created{method="GET",route="/products"} 1.7853395664822323e+09
# HELP demo_app_errors_total Total requests that raised an error
# TYPE demo_app_errors_total counter
# HELP demo_app_request_latency_seconds Request latency in seconds
# TYPE demo_app_request_latency_seconds histogram
demo_app_request_latency_seconds_bucket{le="0.005",method="GET",route="/products"} 3.0
demo_app_request_latency_seconds_bucket{le="0.01",method="GET",route="/products"} 3.0
demo_app_request_latency_seconds_bucket{le="0.025",method="GET",route="/products"} 3.0
demo_app_request_latency_seconds_bucket{le="+Inf",method="GET",route="/products"} 3.0
demo_app_request_latency_seconds_count{method="GET",route="/products"} 3.0
demo_app_request_latency_seconds_sum{method="GET",route="/products"} 0.0050405160000082105
demo_app_request_latency_seconds_bucket{le="0.005",method="POST",route="/checkout"} 3.0
demo_app_request_latency_seconds_bucket{le="0.01",method="POST",route="/checkout"} 3.0
demo_app_request_latency_seconds_bucket{le="+Inf",method="POST",route="/checkout"} 3.0
demo_app_request_latency_seconds_count{method="POST",route="/checkout"} 3.0
demo_app_request_latency_seconds_sum{method="POST",route="/checkout"} 0.003788960000008501
# HELP demo_app_request_latency_seconds_created Request latency in seconds
# TYPE demo_app_request_latency_seconds_created gauge
demo_app_request_latency_seconds_created{method="GET",route="/products"} 1.785339566482267e+09
# HELP demo_app_cache_items Number of entries currently held in the in-memory cache
# TYPE demo_app_cache_items gauge
demo_app_cache_items 0.0
`

func TestParseRealScrapeExtractsLatencyPerRoute(t *testing.T) {
	snap := Parse(realScrape)

	products := RouteKey{Route: "/products", Method: "GET"}
	h, ok := snap.Latency[products]
	if !ok {
		t.Fatal("expected a histogram for GET /products")
	}
	if h.Count != 3 {
		t.Errorf("products count = %v, want 3", h.Count)
	}
	if h.Buckets["0.005"] != 3 || h.Buckets["+Inf"] != 3 {
		t.Errorf("products buckets = %v", h.Buckets)
	}
	if h.Sum <= 0 {
		t.Errorf("products sum should be positive, got %v", h.Sum)
	}

	checkout := RouteKey{Route: "/checkout", Method: "POST"}
	if h, ok := snap.Latency[checkout]; !ok || h.Count != 3 {
		t.Errorf("expected a 3-count histogram for POST /checkout, got %+v ok=%v", h, ok)
	}
}

func TestParseRealScrapeExtractsCacheGauge(t *testing.T) {
	snap := Parse(realScrape)
	if !snap.HasCacheItems {
		t.Fatal("expected the cache gauge to be present")
	}
	if snap.CacheItems != 0 {
		t.Errorf("cache items = %v, want 0", snap.CacheItems)
	}
}

func TestParseIgnoresUnrelatedMetrics(t *testing.T) {
	snap := Parse(realScrape)
	// demo_app_requests_created is a gauge with a name that shares the
	// "demo_app_request..." prefix with our histogram — a prefix-based
	// matcher would misparse it as latency data. Exact-name matching
	// must keep it out entirely.
	for key, h := range snap.Latency {
		if h.Sum > 1e8 {
			t.Errorf("route %+v picked up an unrelated timestamp-scale value: %+v", key, h)
		}
	}
}

func TestParseEmptyInput(t *testing.T) {
	snap := Parse("")
	if len(snap.Latency) != 0 {
		t.Errorf("expected no routes from empty input, got %v", snap.Latency)
	}
	if snap.HasCacheItems {
		t.Error("expected HasCacheItems=false for empty input")
	}
}

func TestParseMissingCacheGaugeLeavesHasCacheItemsFalse(t *testing.T) {
	snap := Parse(`demo_app_request_latency_seconds_count{method="GET",route="/health"} 1.0`)
	if snap.HasCacheItems {
		t.Error("HasCacheItems must stay false when the gauge line is absent")
	}
}

func TestParseSkipsMalformedLines(t *testing.T) {
	text := "not a metric line at all\n" + realScrape
	snap := Parse(text) // must not panic
	if !snap.HasCacheItems {
		t.Error("valid lines after a malformed one must still parse")
	}
}
