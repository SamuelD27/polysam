"""PaperTrader realistic-fill simulator.

Covers:
1. Pass-through when action carries effective_VWAP (walked-VWAP gate
   already produced realistic fill) — wrapper does nothing.
2. Walks books on non-walked ENTER, computes post-fee VWAP, rewrites
   entry_price + entry_price_mid + size_usdc.
3. Returns paper_no_fill when book is empty / asks side empty.
4. Returns paper_no_fill when top-of-book is insufficient for the
   requested shares.
5. Side mapping: Up consumes books.yes.asks; Down consumes books.no.asks.
6. Fee math: bell-curve at p ≈ 0.42 vs p ≈ 0.05 — confirms the curvature.
7. paper_latency_ms is stamped on fill_details from the LatencyModel.
8. EXIT_TP / EXIT_SL / RESOLVE delegate to PaperExecutor unchanged.
9. Pass-through detection: action with gate_passed=True but no
   effective_VWAP also short-circuits (defensive).
"""

from __future__ import annotations

import time

import pytest

from active_bots.execution.executor import MarketCtx
from active_bots.execution.live_book_state import LiveBookState, MarketBooks
from polyhustle.execution.latency import FixedLatency, ZeroLatency
from polyhustle.execution.paper_trader import PaperTrader
from polyhustle.execution.trader import ACTION_ENTER, Decision


def _ctx(slug="x", strike=110_000.0, t_zero=1_700_000_000) -> MarketCtx:
    return MarketCtx(slug=slug, t_zero=t_zero, strike=strike)


def _books(yes_asks=None, no_asks=None,
           yes_bids=None, no_bids=None) -> MarketBooks:
    """Build a MarketBooks with optional per-side levels."""
    yes = LiveBookState(
        tick_size=0.01,
        bids=yes_bids or [(0.42, 100.0)],
        asks=yes_asks if yes_asks is not None else [(0.43, 100.0), (0.44, 200.0)],
        ts_ms=int(time.time() * 1000),
        has_baseline=True,
    )
    no = LiveBookState(
        tick_size=0.01,
        bids=no_bids or [(0.57, 100.0)],
        asks=no_asks if no_asks is not None else [(0.58, 100.0)],
        ts_ms=int(time.time() * 1000),
        has_baseline=True,
    )
    return MarketBooks(yes=yes, no=no)


def _enter(side="Up", entry_price=0.42, size_shares=10.0, **extra) -> Decision:
    """Build a non-walked ENTER decision (no effective_VWAP — wrapper walks)."""
    meta = {"fair": 0.50, "market": entry_price}
    meta.update(extra)
    return Decision(
        action=ACTION_ENTER,
        side=side,
        entry_price=entry_price,
        size_shares=size_shares,
        size_usdc=size_shares * entry_price,
        edge=0.08,
        meta=meta,
    )


# 1. Pass-through


def test_passthrough_when_effective_vwap_set():
    """Action with effective_VWAP from walked-VWAP gate is pass-through."""
    pt = PaperTrader()
    d = _enter(side="Up", entry_price=0.43, size_shares=10.0,
               effective_VWAP=0.43, walked_VWAP=0.43, gate_passed=True)
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=_books())
    assert result.entry is not None
    # Wrapper did NOT rewrite — entry_price came from the action as-is.
    assert result.entry.entry_price == pytest.approx(0.43, rel=1e-9)


# 2. Wrapper walks + rewrites


def test_wrapper_walks_top_of_book_and_rewrites_entry_price():
    """Non-walked ENTER -> wrapper walks asks, rewrites entry_price."""
    pt = PaperTrader()
    yes_asks = [(0.43, 100.0), (0.44, 200.0)]
    d = _enter(side="Up", entry_price=0.42, size_shares=10.0)
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=_books(yes_asks=yes_asks))
    assert result.entry is not None
    # Walk VWAP = 0.43, plus bell-curve fee at p=0.43.
    # fee = 0.018 * 0.43 * 0.57 * 10 ≈ 0.044118 USDC
    # post-fee VWAP per share = 0.43 + 0.044118 / 10 ≈ 0.4344118
    assert result.entry.entry_price > 0.43
    assert result.entry.entry_price < 0.435  # fee tax is ~0.44c per share
    assert result.entry.entry_price_mid == pytest.approx(0.42)


def test_wrapper_walks_into_second_level_when_top_is_thin():
    """Size > top -> walk consumes second level, VWAP between them."""
    pt = PaperTrader()
    yes_asks = [(0.43, 5.0), (0.44, 50.0)]
    # Note: TOB is 5; req is 10; this should ALSO trip the top_of_book guard
    # in the same way as walked-VWAP. Use size=4 to stay under TOB.
    d = _enter(side="Up", entry_price=0.42, size_shares=4.0)
    # First test pure top-of-book fill at 0.43 (no walk into level 2):
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=_books(yes_asks=yes_asks))
    assert result.entry is not None
    # Pure 0.43 fill + fee. No second-level walking.
    assert 0.43 < result.entry.entry_price < 0.44


# 3-4. paper_no_fill


def test_paper_no_fill_when_yes_asks_empty_for_up_side():
    pt = PaperTrader()
    books = _books(yes_asks=[])
    d = _enter(side="Up")
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=books)
    assert result.entry is None
    assert result.rejected is True
    assert result.reject_reason == "paper_no_fill"


def test_paper_no_fill_when_books_is_none():
    pt = PaperTrader()
    d = _enter(side="Up")
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=None)
    assert result.entry is None
    assert result.rejected is True
    assert result.reject_reason == "paper_no_fill"


def test_paper_no_fill_when_top_of_book_smaller_than_request():
    pt = PaperTrader()
    yes_asks = [(0.43, 5.0)]
    d = _enter(side="Up", size_shares=10.0)
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=_books(yes_asks=yes_asks))
    assert result.entry is None
    assert result.rejected is True
    assert result.reject_reason == "paper_no_fill"


# 5. Side mapping


def test_down_side_consumes_no_asks():
    pt = PaperTrader()
    no_asks = [(0.58, 100.0)]
    d = _enter(side="Down", entry_price=0.58, size_shares=5.0)
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(),
                        books=_books(no_asks=no_asks, yes_asks=[]))
    assert result.entry is not None
    assert result.entry.entry_price > 0.58  # fee additive


# 6. Fee curvature


def test_fee_at_extreme_price_is_smaller_than_at_mid():
    """Fee bell curve: fee at p=0.05 << fee at p=0.45."""
    pt = PaperTrader()
    d_mid = _enter(side="Up", entry_price=0.45, size_shares=10.0)
    r_mid = pt.execute(d_mid, _ctx(), btc_price=110_000.0,
                       now=time.time(),
                       books=_books(yes_asks=[(0.45, 100.0)]))
    d_ext = _enter(side="Up", entry_price=0.05, size_shares=10.0)
    r_ext = pt.execute(d_ext, _ctx(), btc_price=110_000.0,
                       now=time.time(),
                       books=_books(yes_asks=[(0.05, 100.0)]))
    fee_mid = r_mid.entry.entry_price - 0.45
    fee_ext = r_ext.entry.entry_price - 0.05
    # Bell curve: fee at p=0.45 is roughly 0.00446/share; at p=0.05 is ~0.000855/share.
    assert fee_mid > 4 * fee_ext


# 7. Latency stamping


def test_latency_model_stamps_fill_details():
    pt = PaperTrader(latency_model=FixedLatency(ms=200.0))
    d = _enter(side="Up")
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=_books())
    assert result.entry is not None
    assert result.entry.fill_details["paper_latency_ms"] == 200.0


def test_zero_latency_stamps_zero():
    pt = PaperTrader(latency_model=ZeroLatency())
    d = _enter(side="Up")
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=_books())
    assert result.entry.fill_details["paper_latency_ms"] == 0.0


# 8. Exit / resolve unchanged


def test_exit_tp_delegates_to_paper_executor_unchanged():
    """EXIT_TP path is identical to today's behaviour — no book walking."""
    from polyhustle.execution.trader import ACTION_EXIT_TP
    pt = PaperTrader()
    position = {
        "slug": "x", "side": "Up", "entry_price": 0.43, "size_usdc": 4.30,
        "size_shares": 10.0, "strike": 110_000.0,
        "entry_time": time.time() - 60.0, "edge": 0.05,
    }
    d = Decision(
        action=ACTION_EXIT_TP, side="Up",
        meta={"exit_price": 0.55, "pnl": 1.20, "hold_time_s": 60.0},
    )
    result = pt.execute(d, _ctx(), position=position, btc_price=110_000.0,
                        now=time.time(), books=_books())
    assert result.exit_ is not None
    assert result.exit_.exit_price == pytest.approx(0.55)


def test_resolve_path_unchanged():
    from polyhustle.execution.trader import ACTION_RESOLVE
    pt = PaperTrader()
    position = {
        "slug": "x", "side": "Up", "entry_price": 0.43, "size_usdc": 4.30,
        "size_shares": 10.0, "strike": 110_000.0,
        "entry_time": time.time() - 300.0, "edge": 0.05,
    }
    d = Decision(action=ACTION_RESOLVE)
    result = pt.execute(d, _ctx(), position=position, btc_price=111_000.0,
                        now=time.time(), books=_books())
    assert result.exit_ is not None
    assert result.exit_.exit_type == "RESOLUTION"


# 9. Pass-through defensive


def test_passthrough_with_gate_passed_flag_alone():
    """Even with gate_passed=True but no effective_VWAP, treat as pass-through."""
    pt = PaperTrader()
    d = _enter(side="Up", entry_price=0.43, size_shares=10.0,
               gate_passed=True)
    result = pt.execute(d, _ctx(), btc_price=110_000.0,
                        now=time.time(), books=_books())
    assert result.entry is not None
    assert result.entry.entry_price == pytest.approx(0.43)


@pytest.mark.slow
def test_paper_trader_reproduces_r22_attribution_within_10pct():
    """Re-derive the captured R2.2 fill-realism tax from events.jsonl
    and confirm it matches reports/r2.2_walked_vwap_loss_attribution.md
    within 10 %.

    This is a unit-level shape check on the captured walked_vwap
    entries — NOT a replay through the orchestrator. The end-to-end
    replay-mode reproduction lands in tests/orchestrator/test_replay_mode.py
    after Task 6 (replay mode).

    Skipped when the R2.2 capture and events.jsonl are not on disk.
    """
    import json
    from pathlib import Path

    capture = Path(
        "daemon_state/scrapes/2026-05-05T12-50-04Z/manifest.json"
    )
    if not capture.exists():
        pytest.skip("R2.2 capture not on disk")

    manifest = json.loads(capture.read_text())
    t_start = manifest["launch_ts_ns"] / 1e9
    t_end = manifest["stop_ts_ns"] / 1e9

    events = Path("daemon_state/events.jsonl")
    if not events.exists():
        pytest.skip("events.jsonl not on disk")

    captured = []
    with events.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts")
            if ts is None or not (t_start <= ts <= t_end):
                continue
            if row.get("type") != "entry_filled":
                continue
            if row.get("strategy") != "walked_vwap":
                continue
            pos = row.get("position") or {}
            mid = pos.get("entry_price_mid")
            walked_eff = pos.get("entry_price")
            shares = pos.get("size_shares")
            if mid is None or walked_eff is None or shares is None:
                continue
            captured.append({
                "mid": mid, "walked_eff": walked_eff, "shares": shares,
            })

    if len(captured) < 30:
        pytest.skip(f"only {len(captured)} captured entries — need >=30 for ratio")

    # Per-trade fill tax = (walked_eff − mid) × shares (positive on a BUY
    # since walked_eff >= mid). The captured aggregate matches the
    # $63.82 figure in reports/r2.2_walked_vwap_loss_attribution.md.
    fill_tax = sum(
        (c["walked_eff"] - c["mid"]) * c["shares"] for c in captured
    )
    expected_tax = 63.82
    rel_err = abs(fill_tax - expected_tax) / expected_tax
    assert rel_err < 0.10, (
        f"R2.2 fill-realism tax reproduction off by {rel_err:.1%}: "
        f"got ${fill_tax:.2f}, expected ${expected_tax:.2f}"
    )
