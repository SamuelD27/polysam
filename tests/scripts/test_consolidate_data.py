"""Tests for scripts/consolidate_data.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def test_module_imports():
    from scripts import consolidate_data  # noqa: F401
