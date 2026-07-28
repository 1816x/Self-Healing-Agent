"""Structured JSON logging for the demo app.

The monitor (Go) tails ``logs/demo-app.jsonl`` line by line and expects one
JSON object per line. Using the stdlib ``logging`` module plus a small JSON
formatter avoids pulling in a logging framework for a demo app whose only
job is to emit predictable, greppable lines.
"""

from __future__ import annotations

import json
import logging
import os
import time

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "demo-app.jsonl")


class JsonFormatter(logging.Formatter):
    """Renders each log record as a single JSON line.

    Fields are picked deliberately narrow: the monitor's detectors key off
    ``level``, ``event``, and ``duration_ms`` — anything else is extra
    context carried through ``**fields`` at the call site.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload)


def get_logger(name: str = "demo-app") -> logging.Logger:
    """Returns a logger writing JSON lines to stdout and ``logs/demo-app.jsonl``.

    Idempotent: safe to call repeatedly (e.g. once per request) without
    stacking duplicate handlers, which uvicorn's reload would otherwise do.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = JsonFormatter()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    os.makedirs(LOG_DIR, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: str, event: str, **fields) -> None:
    """Logs one structured event, e.g. log_event(logger, "info", "checkout.ok", duration_ms=12)."""
    logger.log(logging.getLevelName(level.upper()), event, extra={"fields": fields})
