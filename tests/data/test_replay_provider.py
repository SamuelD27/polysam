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


def test_replay_provider_skips_out_of_window_book_files(tmp_path):
    """Book_feed files whose 5-min market window doesn't overlap the
    session window are skipped at load time.

    This is the headline perf-pass optimisation: the scraper's
    canonical book_feed dir is rotated daily and accumulates files
    spanning many days. Without the filename-window filter the loader
    decompresses every file regardless of session bounds — on the R2.2
    capture (3829 files, 1.9 GB) this OOMs the box.
    """
    import gzip
    import json

    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "scrapes" / "x"
    bf = session_dir / "book_feed" / "2026-05-06"
    bf.mkdir(parents=True)
    t_start = 1_777_900_000.0
    t_end = t_start + 300.0  # only ONE 5-min window in scope

    (session_dir / "manifest.json").write_text(json.dumps({
        "session_id": "x",
        "launch_ts_ns": int(t_start * 1e9),
        "stop_ts_ns": int(t_end * 1e9),
        "events_jsonl_path": str(tmp_path / "events.jsonl"),
        "daemon_log_path": str(tmp_path / "daemon.log"),
    }))
    (tmp_path / "events.jsonl").write_text("")

    # In-window slug — overlaps [t_start, t_end].
    in_slug = f"btc-updown-5m-{int(t_start)}"
    with gzip.open(bf / f"{in_slug}.jsonl.gz", "wt") as f:
        f.write(json.dumps({
            "type": "snapshot", "ts_ns": int(t_start * 1e9),
            "asset_id": "y", "slug": in_slug, "side": "yes",
            "canonical_tick": "0.01", "effective_tick": "0.01",
            "bids": [], "asks": [{"price": "0.43", "size": "100"}],
        }) + "\n")
    # Out-of-window slug whose 5-min window starts AFTER stop_ts.
    out_slug = f"btc-updown-5m-{int(t_end + 600)}"
    with gzip.open(bf / f"{out_slug}.jsonl.gz", "wt") as f:
        f.write(json.dumps({
            "type": "snapshot", "ts_ns": int((t_end + 600) * 1e9),
            "asset_id": "y", "slug": out_slug, "side": "yes",
            "canonical_tick": "0.01", "effective_tick": "0.01",
            "bids": [], "asks": [],
        }) + "\n")
    # Out-of-window slug whose 5-min window ends BEFORE launch_ts.
    pre_slug = f"btc-updown-5m-{int(t_start - 1200)}"
    with gzip.open(bf / f"{pre_slug}.jsonl.gz", "wt") as f:
        f.write(json.dumps({
            "type": "snapshot", "ts_ns": int((t_start - 1200) * 1e9),
            "asset_id": "y", "slug": pre_slug, "side": "yes",
            "canonical_tick": "0.01", "effective_tick": "0.01",
            "bids": [], "asks": [],
        }) + "\n")

    p = ReplayDataProvider(session_dir)
    p.load()
    assert in_slug in p.books_by_slug
    assert out_slug not in p.books_by_slug
    assert pre_slug not in p.books_by_slug


def test_slug_t_zero_parses_known_slug_shape():
    """``btc-updown-5m-1777942500`` should parse to 1777942500.0."""
    from polyhustle.data.replay import ReplayDataProvider
    assert (
        ReplayDataProvider._slug_t_zero("btc-updown-5m-1777942500")
        == 1_777_942_500.0
    )


def test_slug_t_zero_returns_none_on_unparseable_slug():
    """Defensive: weirdly-shaped slug => None (don't filter it out)."""
    from polyhustle.data.replay import ReplayDataProvider
    assert ReplayDataProvider._slug_t_zero("totally-bogus") is None
    assert ReplayDataProvider._slug_t_zero("noseparator") is None


def test_replay_provider_caches_bisect_ts_lists(synthetic_session: Path):
    """Loader pre-builds parallel ts lists so per-tick bisect lookups
    are O(log n) without rebuilding the list every call.

    This catches a regression where the lookup helpers fall back to a
    list comprehension over the records on every tick — turns the
    per-tick cost from O(log n) to O(n) and pushes wall clock past 60s
    on the R2.2 capture.
    """
    from polyhustle.data.replay import ReplayDataProvider
    p = ReplayDataProvider(synthetic_session)
    p.load()
    # btc + market caches populated unconditionally.
    assert p._btc_ts_list == [b.ts for b in p.btc_ticks]
    assert p._market_t_zeros == [t for (t, _, _, _) in p.markets]
    # Per-slug RTDS + books caches populated for every observed slug.
    for slug, records in p.rtds_by_slug.items():
        assert p._rtds_ts_cache[slug] == [t for (t, _) in records]
    for slug, records in p.books_by_slug.items():
        assert p._books_ts_cache[slug] == [r.ts_ns for r in records]
