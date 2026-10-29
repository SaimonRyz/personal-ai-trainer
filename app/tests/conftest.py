from __future__ import annotations

from pathlib import Path

import pytest

MARKERS_BY_DIRECTORY = {
    "architecture": "architecture",
    "contract": "contract",
    "integration": "integration",
    "migration": "migration",
    "security": "security",
    "spike": "spike",
    "unit": "unit",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Assign one stable test level marker from the suite directory."""
    for item in items:
        parts = Path(str(item.path)).parts
        for directory, marker in MARKERS_BY_DIRECTORY.items():
            if directory in parts:
                item.add_marker(getattr(pytest.mark, marker))
                break
