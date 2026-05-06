"""Tests for ``polyhustle.data.live.LiveDataProvider``.

The contract: every field today's strategies read out of their
``on_tick`` arguments must be present on the ``MarketTick`` the
provider yields. The data layer extraction is supposed to preserve
the field set without inventing or dropping anything.

These tests do NOT open real websockets. They drive the underlying
``DaemonState`` directly and verify ``_snapshot()`` emits a
properly-shaped ``MarketTick``.
"""

from __future__ import annotations

import time
from dataclasses import fields

import pytest

from polyhustle.data import DataProvider, MarketTick
from polyhustle.data.live import LiveDataProvider


def _populate(state, *, mid: float | None = 0.42) -> None:
    """Fill the daemon state with the minimum a tick needs."""
    state.btc_price = 110_000.0
    state.btc_ts = time.time()
    state.sigma = 0.55
    state.t_zero = 1_777_985_400
    state.strike = 110_000.0
    state.slug = "btc-updown-5m-1777985400"
    state.market_price_up = mid
    state.market_price_ts = time.time()


def test_marketTick_carries_all_strategy_fields():
    """Field set must include every kwarg today's strategies read on_tick."""
    expected = {
        "timestamp",
        "btc_price",
        "market_price_up",
        "sigma",
        "t_zero",
        "strike",
        "slug",
        "books",
        "market_price_ts",
    }
    actual = {f.name for f in fields(MarketTick)}
    assert expected <= actual, f"missing fields: {expected - actual}"


def test_provider_implements_abc():
    """LiveDataProvider must satisfy the DataProvider contract."""
    p = LiveDataProvider()
    assert isinstance(p, DataProvider)


def test_snapshot_returns_none_until_state_is_warm():
    """No btc_price → no tick yet."""
    p = LiveDataProvider()
    assert p._snapshot() is None


def test_snapshot_emits_full_tick_when_state_is_warm():
    """Once the underlying state has price + market context, ticks come out."""
    p = LiveDataProvider()
    _populate(p.state)
    tick = p._snapshot()
    assert tick is not None
    assert tick.btc_price == 110_000.0
    assert tick.market_price_up == pytest.approx(0.42)
    assert tick.sigma == pytest.approx(0.55)
    assert tick.t_zero == 1_777_985_400.0
    assert tick.strike == 110_000.0
    assert tick.slug == "btc-updown-5m-1777985400"
    assert tick.books is None  # no book WS subscription in this test
    assert tick.market_price_ts > 0


def test_snapshot_handles_unset_sigma_as_zero():
    """sigma is a float on the tick; the provider folds None → 0.0 so strategies see a number."""
    p = LiveDataProvider()
    _populate(p.state)
    p.state.sigma = None
    tick = p._snapshot()
    assert tick is not None
    assert tick.sigma == 0.0


def test_snapshot_passes_through_books_for_subscribed_slug():
    """If a MarketBooks is registered for the current slug, the tick carries it."""
    p = LiveDataProvider()
    _populate(p.state)
    sentinel = object()
    p.state.books_by_slug[p.state.slug] = sentinel  # type: ignore[assignment]
    tick = p._snapshot()
    assert tick is not None
    assert tick.books is sentinel


def test_market_tick_is_frozen():
    """Strategies must not mutate ticks."""
    tick = MarketTick(
        timestamp=time.time(),
        btc_price=1.0,
        market_price_up=0.5,
        sigma=0.3,
        t_zero=0.0,
        strike=1.0,
        slug="x",
        books=None,
        market_price_ts=0.0,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        tick.btc_price = 2.0  # type: ignore[misc]
