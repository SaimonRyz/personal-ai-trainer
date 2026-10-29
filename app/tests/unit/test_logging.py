from __future__ import annotations

import json
import logging
from uuid import uuid4

from trainer.observability.logging import JsonFormatter


def test_json_formatter_emits_one_safe_event_without_unapproved_fields() -> None:
    correlation_id = str(uuid4())
    record = logging.LogRecord(
        name="trainer.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.correlation_id = correlation_id
    record.status_code = 401
    record.authorization = "Bearer must-not-appear"
    record.payload = {"private": "must-not-appear"}

    encoded = JsonFormatter().format(record)
    document = json.loads(encoded)

    assert "\n" not in encoded
    assert document["correlation_id"] == correlation_id
    assert document["status_code"] == 401
    assert "authorization" not in document
    assert "payload" not in document
    assert "must-not-appear" not in encoded
