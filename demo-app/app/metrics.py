"""Prometheus metrics exposed at /metrics.

Three metrics, matched to the two detectors Phase 2 needs plus the memory
gauge for the stretch detector (B3):

- ``demo_app_requests_total`` / ``demo_app_errors_total`` — error-rate detector (B1)
- ``demo_app_request_latency_seconds`` — p95 latency detector (B2)
- ``demo_app_cache_items`` — memory-slope detector (B3, stretch)
"""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS_TOTAL = Counter(
    "demo_app_requests_total", "Total requests handled", ["route", "method"]
)
ERRORS_TOTAL = Counter(
    "demo_app_errors_total", "Total requests that raised an error", ["route", "method"]
)
REQUEST_LATENCY = Histogram(
    "demo_app_request_latency_seconds",
    "Request latency in seconds",
    ["route", "method"],
)
CACHE_ITEMS = Gauge(
    "demo_app_cache_items", "Number of entries currently held in the in-memory cache"
)
