"""Smoke tests for the demo app.

Phase 0 scope: the app responds correctly, its logs are valid JSON lines,
and /metrics parses as Prometheus text. Bug-specific regression tests land
alongside each injected bug in later phases, on both the demo-app side and
the monitor's detector fixtures.
"""

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_products():
    response = client.get("/products")
    assert response.status_code == 200
    products = response.json()
    assert len(products) == 3
    assert {"id", "name", "price_cents"} <= products[0].keys()


def test_checkout_success():
    response = client.post("/checkout", json={"product_ids": [1, 2]})
    assert response.status_code == 200
    body = response.json()
    assert body["total_cents"] == 1999 + 4999
    assert body["item_count"] == 2


def test_checkout_missing_product_ids():
    response = client.post("/checkout", json={})
    assert response.status_code == 400


def test_checkout_unknown_product():
    response = client.post("/checkout", json={"product_ids": [999]})
    assert response.status_code == 404


def test_metrics_endpoint_is_prometheus_text():
    # Generate at least one request so a metric sample exists to find.
    client.get("/health")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "demo_app_requests_total" in response.text


def test_log_file_contains_valid_json_lines(tmp_path, monkeypatch):
    # Route logging at a temp file so this test doesn't depend on— or
    # pollute — the real logs/demo-app.jsonl the monitor tails. Patching
    # the module globals is enough; reloading the module (tried first)
    # re-executes its top level and undoes the patch, since LOG_DIR/LOG_FILE
    # are recomputed from __file__ again on import.
    import app.logging as logging_module

    monkeypatch.setattr(logging_module, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging_module, "LOG_FILE", str(tmp_path / "demo-app.jsonl"))

    logger = logging_module.get_logger("test-json-lines")
    logging_module.log_event(logger, "info", "test.event", foo="bar")

    log_path = tmp_path / "demo-app.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert lines
    for line in lines:
        record = json.loads(line)
        assert "ts" in record
        assert "level" in record
        assert "event" in record
