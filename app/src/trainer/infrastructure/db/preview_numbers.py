"""Exact, human-readable numbers for server confirmation previews."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def format_number(value: Any) -> str:
    """Keep every significant digit while removing only trailing decimal zeroes."""
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("preview number must be finite")
    if not decimal:
        return "0"
    return format(decimal.normalize(), "f").replace(".", ",")
