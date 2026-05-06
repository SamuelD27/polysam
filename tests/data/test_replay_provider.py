"""ReplayDataProvider — load + index + chronology tests."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_replay_provider_reads_manifest(synthetic_session: Path):
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    assert p.session_id == "2026-05-06T00-00-00Z"
    assert p.launch_ts_ns > 0
    assert p.stop_ts_ns > p.launch_ts_ns


def test_replay_provider_indexes_book_feed_per_slug(synthetic_session: Path):
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    assert "btc-updown-5m-1777900000" in p.books_by_slug
    assert "btc-updown-5m-1777900300" in p.books_by_slug


def test_replay_provider_book_records_are_time_sorted(synthetic_session: Path):
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    for slug, records in p.books_by_slug.items():
        ts_list = [r.ts_ns for r in records]
        assert ts_list == sorted(ts_list), (
            f"slug {slug} has out-of-order book records"
        )


def test_replay_provider_loads_events_in_window(synthetic_session: Path):
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    rollovers = [e for e in p.events if e.get("type") == "market_rollover"]
    assert len(rollovers) == 2


def test_replay_provider_reconstructs_btc_tape(synthetic_session: Path):
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    assert len(p.btc_ticks) >= 5


@pytest.mark.asyncio
async def test_replay_provider_yields_ticks_chronologically(synthetic_session: Path):
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    ticks = []
    async for t in p.stream():
        ticks.append(t)
    assert len(ticks) > 0
    timestamps = [t.timestamp for t in ticks]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_replay_provider_yields_books_attached_to_ticks(synthetic_session: Path):
    """Ticks for a slug carry the appropriate MarketBooks snapshot."""
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    ticks = []
    async for t in p.stream():
        ticks.append(t)
    assert any(t.books is not None for t in ticks)


@pytest.mark.asyncio
async def test_replay_provider_market_price_up_changes_with_rtds(synthetic_session: Path):
    """RTDS price events drive market_price_up forward."""
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    ticks = []
    async for t in p.stream():
        if t.market_price_up is not None:
            ticks.append((t.timestamp, t.market_price_up))
    prices = {round(p, 2) for _, p in ticks}
    assert 0.42 in prices or 0.45 in prices


@pytest.mark.asyncio
async def test_replay_provider_market_price_ts_set_when_rtds_observed(
    synthetic_session: Path,
):
    """When market_price_up is non-None, market_price_ts should be the RTDS event ts."""
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    found = False
    async for t in p.stream():
        if t.market_price_up == 0.42:
            assert t.market_price_ts > 0
            found = True
            break
    # Synthetic data ensures we'll see m=0.42 within the first slug's window.
    assert found, "expected to observe market_price_up=0.42 in stream"
