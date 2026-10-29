from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

SAFE_FIELDS = {
    "correlation_id",
    "causation_id",
    "operation_id",
    "method",
    "path",
    "status_code",
    "latency_ms",
    "error_code",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field in SAFE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                document[field] = value
        if record.exc_info is not None and record.exc_info[0] is not None:
            document["exception_type"] = record.exc_info[0].__name__
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
