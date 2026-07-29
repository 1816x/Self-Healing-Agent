"""Demo "orders" service.

A deliberately small FastAPI app that exists to misbehave on cue. It ships
clean in Phase 0; ``scripts/inject_bug.sh`` (Phase 1) applies real commits
that introduce specific, catalogued bugs (see PLAN.md) so the monitor and
agent have something genuine to detect, diagnose, and fix.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.logging import get_logger, log_event
from app.metrics import ERRORS_TOTAL, REQUEST_LATENCY, REQUESTS_TOTAL

app = FastAPI(title="demo-app", version="0.0.0")
logger = get_logger()

PRODUCTS = [
    {"id": 1, "name": "widget", "price_cents": 1999},
    {"id": 2, "name": "gadget", "price_cents": 4999},
    {"id": 3, "name": "gizmo", "price_cents": 999},
]


@app.middleware("http")
async def instrument_requests(request: Request, call_next):
    """Records latency/count/error metrics and a JSON log line per request.

    Centralized here rather than per-route so every future route gets the
    same observability for free — the monitor should never miss a request
    because a handler forgot to log it.
    """
    route = request.url.path
    method = request.method
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000
        ERRORS_TOTAL.labels(route=route, method=method).inc()
        REQUESTS_TOTAL.labels(route=route, method=method).inc()
        REQUEST_LATENCY.labels(route=route, method=method).observe(duration_ms / 1000)
        log_event(
            logger, "error", "request.unhandled_exception",
            route=route, method=method, duration_ms=round(duration_ms, 2),
            status=500,  # FastAPI's default response for an unhandled exception
        )
        raise

    duration_ms = (time.perf_counter() - start) * 1000
    REQUESTS_TOTAL.labels(route=route, method=method).inc()
    REQUEST_LATENCY.labels(route=route, method=method).observe(duration_ms / 1000)
    if response.status_code >= 500:
        ERRORS_TOTAL.labels(route=route, method=method).inc()

    log_event(
        logger,
        "error" if response.status_code >= 500 else "info",
        "request.completed",
        route=route,
        method=method,
        status=response.status_code,
        duration_ms=round(duration_ms, 2),
    )
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/products")
def list_products() -> list[dict]:
    return PRODUCTS


@app.post("/checkout")
def checkout(payload: dict) -> dict:
    """Charges for the requested product IDs.

    Validation is intentionally minimal: this is the endpoint bugs get
    injected into (see PLAN.md bug catalog), and the fix PRs Phase 4
    produces should read as genuine corrections to genuine gaps, not
    patches for edge cases nobody would hit.
    """
    product_ids = payload.get("product_ids", [])
    if not product_ids:
        raise HTTPException(status_code=400, detail="product_ids required")

    total_cents = 0
    for pid in product_ids:
        match = next((p for p in PRODUCTS if p["id"] == pid), None)
        if match is None:
            raise HTTPException(status_code=404, detail=f"unknown product_id {pid}")
        total_cents += match["price_cents"]

    return {"total_cents": total_cents, "item_count": len(product_ids)}


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
