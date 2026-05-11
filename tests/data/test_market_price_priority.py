"""ReplayDataProvider — H2 RTDS market_price source priority + lookup.

Mirrors the BTC-tape tests in tests/daemon/test_btc_ticks_emission.py
(replay-side half) but for the H2 RTDS tape:

    Priority 1: <session_dir>/market_price.jsonl   (H2 — preferred)
    Priority 2: events.jsonl rows of type
                rtds_market_price / market_price_update (legacy)
    Priority 3: none                                (orchestrator
                substitutes 0.5 — the R4 bug)

market_price_source on the provider exposes which source fired, for
diagnostics + post-validation assertions in replay reports.
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path


def _write_manifest(session_dir: Path, t_lo: float, t_hi: float, *,
                    events_path: str = "", daemon_log_path: str = "") -> None:
    """Minimal manifest covering the [t_lo, t_hi] window."""
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "manifest.json").write_text(json.dumps({
        "session_id": session_dir.name,
        "launch_ts_ns": int(t_lo * 1e9),
        "stop_ts_ns": int(t_hi * 1e9),
        "events_jsonl_path": events_path,
        "daemon_log_path": daemon_log_path,
    }))


def _write_market_price(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_events(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ── Source priority ──────────────────────────────────────────────────────


def test_replay_prefers_session_market_price_over_events_jsonl(tmp_path):
    """When both sources exist for the same slug, market_price.jsonl wins;
    the events.jsonl rows are NOT indexed (priority-2 stays dormant).

    Catches a regression where someone accidentally double-loads both
    and the per-slug list becomes a noisy concatenation.
    """
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    t_lo, t_hi = 1_700_000_000.0, 1_700_000_300.0
    events_path = tmp_path / "events.jsonl"
    _write_manifest(
        session_dir, t_lo, t_hi,
        events_path=str(events_path),
        daemon_log_path=str(tmp_path / "daemon.log"),
    )

    slug = "btc-updown-5m-1700000000"

    # Priority-1 source: 5 rows at market_price_up=0.42.
    _write_market_price(session_dir / "market_price.jsonl", [
        {
            "ts_ns": int((t_lo + i) * 1e9), "ts": t_lo + i,
            "slug": slug, "outcome": "yes",
            "raw_price": 0.42, "market_price_up": 0.42,
        }
        for i in range(5)
    ])
    # Priority-2 source: 10 rows at market_price_up=0.99 + 2 market_rollover
    # markers (markets are always indexed, regardless of source).
    _write_events(events_path, [
        {
            "ts": t_lo, "type": "market_rollover",
            "slug": slug, "t_zero": t_lo, "strike": 110_000.0,
        },
    ] + [
        {
            "ts": t_lo + 10 + i, "type": "rtds_market_price",
            "slug": slug, "market_price_up": 0.99,
        }
        for i in range(10)
    ])

    p = ReplayDataProvider(session_dir)
    p.load()
    assert p.market_price_source == "market_price.jsonl"
    assert slug in p.rtds_by_slug
    # Priority-1 wins → 5 rows total, all at 0.42 (not 0.99).
    assert len(p.rtds_by_slug[slug]) == 5
    assert all(abs(m - 0.42) < 1e-9 for _, m in p.rtds_by_slug[slug])


def test_replay_falls_back_to_events_jsonl_when_market_price_absent(tmp_path):
    """No market_price.jsonl → events.jsonl rtds_market_price rows are
    indexed (legacy / forward-compat path). Source = 'events_jsonl'.

    The synthetic_session fixture already exercises this implicitly;
    this test makes the priority-2 contract explicit and resilient
    to fixture changes.
    """
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    t_lo, t_hi = 1_700_000_000.0, 1_700_000_300.0
    events_path = tmp_path / "events.jsonl"
    _write_manifest(
        session_dir, t_lo, t_hi,
        events_path=str(events_path),
        daemon_log_path=str(tmp_path / "daemon.log"),
    )

    slug = "btc-updown-5m-1700000000"
    _write_events(events_path, [
        {
            "ts": t_lo, "type": "market_rollover",
            "slug": slug, "t_zero": t_lo, "strike": 110_000.0,
        },
        {
            "ts": t_lo + 5, "type": "rtds_market_price",
            "slug": slug, "market_price_up": 0.30,
        },
        {
            "ts": t_lo + 60, "type": "market_price_update",
            "slug": slug, "market_price_up": 0.55,
        },
    ])

    p = ReplayDataProvider(session_dir)
    p.load()
    assert p.market_price_source == "events_jsonl"
    assert slug in p.rtds_by_slug
    assert len(p.rtds_by_slug[slug]) == 2
    assert {round(m, 2) for _, m in p.rtds_by_slug[slug]} == {0.30, 0.55}


def test_replay_market_price_source_none_when_neither_present(tmp_path):
    """Neither market_price.jsonl nor matching events → source 'none' and
    rtds_by_slug stays empty. _market_price_at returns (None, None);
    the orchestrator's 'or 0.5' fallback then fires (this IS the R4
    bug; the test pins the failure-mode shape so it can be detected
    in diagnostics)."""
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    t_lo, t_hi = 1_700_000_000.0, 1_700_000_300.0
    events_path = tmp_path / "events.jsonl"
    _write_manifest(
        session_dir, t_lo, t_hi,
        events_path=str(events_path),
        daemon_log_path=str(tmp_path / "daemon.log"),
    )
    _write_events(events_path, [
        {
            "ts": t_lo, "type": "market_rollover",
            "slug": "btc-updown-5m-1700000000",
            "t_zero": t_lo, "strike": 110_000.0,
        },
    ])

    p = ReplayDataProvider(session_dir)
    p.load()
    assert p.market_price_source == "none"
    assert p.rtds_by_slug == {}
    assert p._market_price_at("btc-updown-5m-1700000000", t_lo + 10) == (None, None)


# ── Window filter + per-slug indexing ────────────────────────────────────


def test_load_market_price_filters_to_session_window(tmp_path):
    """Rows outside [launch_ts_ns, stop_ts_ns] are skipped at load time,
    matching _load_btc_tape's behaviour. The canonical market_price.jsonl
    is a per-session file (one window) so out-of-window rows shouldn't
    normally exist — but a resume scenario or a corrupted file could
    have leftovers; the filter is defensive."""
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    t_lo, t_hi = 1_700_000_000.0, 1_700_000_300.0
    _write_manifest(session_dir, t_lo, t_hi)

    slug = "btc-updown-5m-1700000000"
    _write_market_price(session_dir / "market_price.jsonl", [
        # in window
        {"ts": t_lo + 10, "slug": slug, "outcome": "yes",
         "raw_price": 0.50, "market_price_up": 0.50},
        # below launch_ts
        {"ts": t_lo - 1, "slug": slug, "outcome": "yes",
         "raw_price": 0.40, "market_price_up": 0.40},
        # above stop_ts
        {"ts": t_hi + 1, "slug": slug, "outcome": "yes",
         "raw_price": 0.60, "market_price_up": 0.60},
    ])

    p = ReplayDataProvider(session_dir)
    p.load()
    assert p.market_price_source == "market_price.jsonl"
    assert len(p.rtds_by_slug[slug]) == 1
    assert p.rtds_by_slug[slug][0] == (t_lo + 10, 0.50)


def test_load_market_price_indexes_per_slug(tmp_path):
    """Multiple slugs in the same tape file are bucketed by slug — a
    smoke for the cross-slug independence that book / events / btc
    sources already give us."""
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    t_lo, t_hi = 1_700_000_000.0, 1_700_000_600.0
    _write_manifest(session_dir, t_lo, t_hi)

    slug_a = "btc-updown-5m-1700000000"
    slug_b = "btc-updown-5m-1700000300"
    rows = []
    for i in range(3):
        rows.append({
            "ts": t_lo + i, "slug": slug_a, "outcome": "yes",
            "raw_price": 0.30, "market_price_up": 0.30,
        })
    for i in range(5):
        rows.append({
            "ts": t_lo + 300 + i, "slug": slug_b, "outcome": "no",
            "raw_price": 0.20, "market_price_up": 0.80,
        })
    _write_market_price(session_dir / "market_price.jsonl", rows)

    p = ReplayDataProvider(session_dir)
    p.load()
    assert len(p.rtds_by_slug[slug_a]) == 3
    assert len(p.rtds_by_slug[slug_b]) == 5
    assert all(abs(m - 0.30) < 1e-9 for _, m in p.rtds_by_slug[slug_a])
    assert all(abs(m - 0.80) < 1e-9 for _, m in p.rtds_by_slug[slug_b])


def test_market_price_at_bisect_returns_latest_le_now(tmp_path):
    """_market_price_at uses bisect_right - 1 over the cached ts list:
    return the most recent record at-or-before `now`. Lookups before
    the first record return (None, None)."""
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    t_lo, t_hi = 1_700_000_000.0, 1_700_000_300.0
    _write_manifest(session_dir, t_lo, t_hi)

    slug = "btc-updown-5m-1700000000"
    points = [(t_lo + 10, 0.40), (t_lo + 60, 0.45), (t_lo + 120, 0.50)]
    _write_market_price(session_dir / "market_price.jsonl", [
        {"ts": ts, "slug": slug, "outcome": "yes",
         "raw_price": m, "market_price_up": m}
        for ts, m in points
    ])

    p = ReplayDataProvider(session_dir)
    p.load()
    # Before any point → (None, None).
    assert p._market_price_at(slug, t_lo + 5) == (None, None)
    # Exact match → that point.
    assert p._market_price_at(slug, t_lo + 10) == (0.40, t_lo + 10)
    # Between points → the earlier one.
    assert p._market_price_at(slug, t_lo + 30) == (0.40, t_lo + 10)
    assert p._market_price_at(slug, t_lo + 100) == (0.45, t_lo + 60)
    # After all points → the last one.
    assert p._market_price_at(slug, t_lo + 200) == (0.50, t_lo + 120)
    # Unknown slug → (None, None).
    assert p._market_price_at("unknown-slug", t_lo + 100) == (None, None)


def test_load_market_price_records_sorted_after_load(tmp_path):
    """Records are time-sorted by ts after load, even if the on-disk file
    is unsorted — bisect lookups assume sorted input. _load_market_price
    appends in file order; _index_markets_and_rtds sorts. Catches
    regression if the sort is removed."""
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    t_lo, t_hi = 1_700_000_000.0, 1_700_000_300.0
    _write_manifest(session_dir, t_lo, t_hi)

    slug = "btc-updown-5m-1700000000"
    # Write out of order.
    _write_market_price(session_dir / "market_price.jsonl", [
        {"ts": t_lo + 60, "slug": slug, "outcome": "yes",
         "raw_price": 0.45, "market_price_up": 0.45},
        {"ts": t_lo + 10, "slug": slug, "outcome": "yes",
         "raw_price": 0.40, "market_price_up": 0.40},
        {"ts": t_lo + 30, "slug": slug, "outcome": "yes",
         "raw_price": 0.42, "market_price_up": 0.42},
    ])

    p = ReplayDataProvider(session_dir)
    p.load()
    ts_list = [t for t, _ in p.rtds_by_slug[slug]]
    assert ts_list == sorted(ts_list)
    assert p._rtds_ts_cache[slug] == sorted(p._rtds_ts_cache[slug])


def test_market_price_source_attribute_set_before_load(tmp_path):
    """market_price_source initialised to 'none' in __init__ so direct
    attribute access is safe before load() — diagnostics that probe
    the source without first loading must not raise AttributeError."""
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    _write_manifest(session_dir, 1.0, 2.0)
    p = ReplayDataProvider(session_dir)
    # No load() call yet.
    assert p.market_price_source == "none"


def test_bisect_module_imported_in_test_for_smoke():
    """Sanity: bisect is what _market_price_at uses — quick assertion
    that the algorithm matches the module's behaviour on a small
    fixture."""
    ts = [10.0, 20.0, 30.0]
    # bisect_right(ts, x) - 1 should give the index of the largest ts <= x.
    assert bisect.bisect_right(ts, 15.0) - 1 == 0  # ts[0] = 10
    assert bisect.bisect_right(ts, 25.0) - 1 == 1  # ts[1] = 20
    assert bisect.bisect_right(ts, 30.0) - 1 == 2  # exact match → last
    assert bisect.bisect_right(ts, 5.0) - 1 == -1  # before first → None
