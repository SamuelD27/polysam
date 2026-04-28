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


# Bug surfaced by the 2026-04-28 60-min paper smoke: clob_book_feed only
# refreshed token_index/books_by_slug on WS reconnect. When state.t_zero
# rolled within an active WS session (the common case — Polymarket only
# disconnects ~every 10 min), markets created after the last reconnect
# were silently un-subscribed → walked_vwap rejected with
# `no_book_subscription` for the entire 5-min window.


@pytest.mark.asyncio
async def test_clob_book_feed_resubscribes_on_t_zero_rollover(monkeypatch):
    """When state.t_zero rolls during an active WS session, the existing WS
    is closed and a new subscription fires for the new slugs within ~1s.
    """
    import daemon_base_v1

    state = daemon_base_v1.DaemonState()
    state.t_zero = 1000
    state.slug = "slug-A"

    def fake_resolver(s):
        if s.t_zero == 1000:
            book = LiveBookState(tick_size=0.01)
            return ({"AID_A": {"slug": "slug-A", "side": "yes", "book": book}},
                    {"slug-A": MarketBooks(yes=book, no=None)})
        else:
            book = LiveBookState(tick_size=0.01)
            return ({"AID_B": {"slug": "slug-B", "side": "yes", "book": book}},
                    {"slug-B": MarketBooks(yes=book, no=None)})

    monkeypatch.setattr(daemon_base_v1, "_resolve_books_for_window", fake_resolver)

    subscribe_calls: list[tuple[str, str]] = []

    class FakeWS:
        def __init__(self, name):
            self.name = name
            self.queue: asyncio.Queue = asyncio.Queue()
            self.closed = asyncio.Event()

        async def send(self, msg):
            subscribe_calls.append((self.name, msg))

        async def recv(self):
            msg = await self.queue.get()
            if msg is None:
                from daemon_base_v1 import websockets as ws_mod
                raise ws_mod.ConnectionClosed(None, None)
            return msg

        async def close(self):
            if not self.closed.is_set():
                self.closed.set()
                await self.queue.put(None)

    fake_ws_a = FakeWS("ws-A")
    fake_ws_b = FakeWS("ws-B")
    fake_ws_iter = iter([fake_ws_a, fake_ws_b])

    class FakeConnect:
        def __init__(self, ws):
            self.ws = ws

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *exc):
            await self.ws.close()
            return False

    def fake_connect(url, **kwargs):
        return FakeConnect(next(fake_ws_iter))

    monkeypatch.setattr(daemon_base_v1.websockets, "connect", fake_connect)

    task = asyncio.create_task(daemon_base_v1.clob_book_feed(state))
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if subscribe_calls:
                break
        assert len(subscribe_calls) == 1, f"first subscribe missing: {subscribe_calls!r}"
        assert subscribe_calls[0][0] == "ws-A"
        assert "AID_A" in subscribe_calls[0][1]

        await fake_ws_a.queue.put(json.dumps({
            "event_type": "book", "asset_id": "AID_A",
            "bids": [], "asks": [{"price": "0.50", "size": "10"}],
        }))
        await asyncio.sleep(0.2)

        state.t_zero = 2000
        state.slug = "slug-B"

        for _ in range(30):
            await asyncio.sleep(0.1)
            if len(subscribe_calls) >= 2:
                break

        assert fake_ws_a.closed.is_set(), "ws-A should be closed after rollover"
        assert len(subscribe_calls) == 2, (
            f"second subscribe missing — feed did not detect rollover: {subscribe_calls!r}"
        )
        assert subscribe_calls[1][0] == "ws-B"
        assert "AID_B" in subscribe_calls[1][1]
        assert "AID_A" not in subscribe_calls[1][1], (
            "second subscribe must use NEW slug's tokens, not stale ones"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
