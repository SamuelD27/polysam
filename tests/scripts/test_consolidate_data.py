"""Tests for scripts/consolidate_data.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def test_module_imports():
    from scripts import consolidate_data  # noqa: F401


import pytest
from scripts.consolidate_data import slug_to_asset


@pytest.mark.parametrize("slug, expected", [
    ("btc-updown-5m-1777864200", "btc"),
    ("doge-up-or-down-5m-123", "doge"),
    ("eth-updown-5m-99", "eth"),
    ("sol-up-or-down-5m-1", "sol"),
    ("xrp-updown-5m-42", "xrp"),
    ("XRP-FOO", "xrp"),
])
def test_slug_to_asset_known_prefixes(slug, expected):
    assert slug_to_asset(slug) == expected


def test_slug_to_asset_unknown_returns_none():
    assert slug_to_asset("trump-2028-winner") is None
    assert slug_to_asset("") is None
    assert slug_to_asset(None) is None
