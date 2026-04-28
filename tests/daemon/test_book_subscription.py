"""Tests for daemon_base_v1.clob_book_feed message dispatch.

Uses a fake async generator standing in for the WS connection. Exercises
the dispatcher (snapshot, price_change, last_trade_price, tick_size_change)
into the per-token state machine.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from active_bots.execution.live_book_state import LiveBookState, MarketBooks
from daemon_base_v1 import _dispatch_book_message  # to be added


def _state_with_token(asset_id: str, slug: str, side: str):
    state = {asset_id: {"slug": slug, "side": side, "book": LiveBookState(tick_size=0.01)}}
    return state


def test_book_event_replaces_state():
    state = _state_with_token("AID1", "btc-updown-5m-100", "yes")
    msg = {
        "event_type": "book",
        "asset_id": "AID1",
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.42", "size": "8"}],
    }
    _dispatch_book_message(msg, state, ts_ms=1000)
    assert state["AID1"]["book"].asks == [(0.42, 8.0)]


def test_price_change_after_baseline():
    state = _state_with_token("AID1", "btc-updown-5m-100", "yes")
    state["AID1"]["book"].apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}],
        ts_ms=900,
    )
    msg = {
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "AID1", "side": "SELL", "price": "0.42", "size": "0"},
        ],
    }
    _dispatch_book_message(msg, state, ts_ms=1100)
    assert state["AID1"]["book"].asks == []


def test_unknown_asset_id_dropped():
    state = _state_with_token("AID1", "btc-updown-5m-100", "yes")
    msg = {
        "event_type": "book",
        "asset_id": "AID_UNKNOWN",
        "bids": [], "asks": [],
    }
    _dispatch_book_message(msg, state, ts_ms=1000)
    assert state["AID1"]["book"].has_baseline is False


def test_tick_size_change_updates_state():
    state = _state_with_token("AID1", "btc-updown-5m-100", "yes")
    state["AID1"]["book"].apply_snapshot(bids=[], asks=[], ts_ms=900)
    msg = {
        "event_type": "tick_size_change",
        "asset_id": "AID1",
        "new_tick_size": "0.001",
    }
    _dispatch_book_message(msg, state, ts_ms=1100)
    assert state["AID1"]["book"].tick_size == 0.001
