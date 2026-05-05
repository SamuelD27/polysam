"""Integration-lite tests for ReplayExecutor.

Synthetic books and synthetic orders; no network, no disk, no daemon state.
"""

from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest

from active_bots.execution.book import Book, Level
from active_bots.execution.fees import CRYPTO
from active_bots.execution.latency import (
    DUBLIN_PRIOR,
    SG_WG_PRIOR,
    ConditionedSampler,
    LatencyProfile,
)
from active_bots.execution.replay_executor import (
    DictBookStore,
    OrderRequest,
    ReplayExecutor,
)

TICK = Decimal("0.01")


def _book(ts_ns: int, bids: list[tuple[str, str]], asks: list[tuple[str, str]], tok: str = "t1") -> Book:
    return Book(
        token_id=tok,
        side_bids=tuple(Level(Decimal(p), Decimal(s)) for p, s in bids),
        side_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in asks),
        tick_size=TICK,
        ts_ns=ts_ns,
    )


def _store(books: list[Book]) -> DictBookStore:
    s = DictBookStore()
    for b in books:
        s.add(b)
    s.freeze()
    return s


def _order(
    side: str = "BUY",
    shares: str = "10",
    mid: str = "0.50",
    limit: str = "0.99",
    token_id: str = "t1",
) -> OrderRequest:
    return OrderRequest(
        token_id=token_id,
        side=side,  # type: ignore[arg-type]
        requested_shares=Decimal(shares),
        worst_price_limit=Decimal(limit),
        decision_mid=Decimal(mid),
        category=CRYPTO,
        tick_size=TICK,
    )


def _fast_profile() -> LatencyProfile:
    return LatencyProfile(1.0, 2.0, 3.0, 5.0, "prior")


def test_full_fill_on_fresh_book():
    # Asks tight around 0.51, decision_mid 0.50.
    book = _book(
        ts_ns=1_000_000_000,
        bids=[("0.49", "100")],
        asks=[("0.51", "100"), ("0.52", "100")],
    )
    store = _store([book])
    ex = ReplayExecutor(
        books=store,
        latency_profile=_fast_profile(),
        rng=random.Random(1),
    )
    rec = ex.post_fak(_order(mid="0.50"), decision_ts_ns=1_000_000_000)
    assert rec.classification == "full"
    assert math.isfinite(rec.total_IS)
    assert rec.fees_cost > 0
    # Half-spread (0.51 vs 0.50) = 200 bps; no latency drift (same snapshot);
    # no book-walk (full fill at level 1). Plus fees bps.
    assert rec.half_spread_cost == pytest.approx(200.0, rel=1e-9)
    assert rec.latency_drift_cost == pytest.approx(0.0, abs=1e-9)
    assert rec.book_walk_cost == pytest.approx(0.0, abs=1e-9)


def test_book_stale_rejection():
    # Snapshot at 0s; decision at 1s; latency ~1ms. Staleness ~1s = 1000ms > 500.
    book = _book(ts_ns=0, bids=[("0.49", "100")], asks=[("0.51", "100")])
    store = _store([book])
    ex = ReplayExecutor(
        books=store,
        latency_profile=_fast_profile(),
        staleness_hard_ms=500,
        rng=random.Random(2),
    )
    rec = ex.post_fak(_order(), decision_ts_ns=1_000_000_000)
    assert rec.classification == "book_stale"
    assert math.isnan(rec.half_spread_cost)
    assert math.isnan(rec.book_walk_cost)
    assert math.isnan(rec.latency_drift_cost)
    assert math.isnan(rec.total_IS)
    assert rec.book_staleness_ms >= 1000


def test_mode_snap_back_differs_from_freeze_depleted():
    # Two snapshots: s1 at t=0 with tight ask 0.51; s2 at t=2s with wider ask 0.60.
    # Decision at t=1s with ~1ms latency -> t_ack ~1.001s.
    # freeze_depleted: uses s1 (ask 0.51).
    # snap_back:       uses s2 (ask 0.60).
    s1 = _book(ts_ns=0, bids=[("0.49", "100")], asks=[("0.51", "100")])
    s2 = _book(ts_ns=2_000_000_000, bids=[("0.40", "100")], asks=[("0.60", "100")])
    store = _store([s1, s2])

    ex_fd = ReplayExecutor(
        books=store,
        latency_profile=_fast_profile(),
        mode="freeze_depleted",
        staleness_hard_ms=10_000,
        rng=random.Random(3),
    )
    ex_sb = ReplayExecutor(
        books=store,
        latency_profile=_fast_profile(),
        mode="snap_back",
        staleness_hard_ms=10_000,
        rng=random.Random(3),
    )
    o = _order()
    rec_fd = ex_fd.post_fak(o, decision_ts_ns=1_000_000_000)
    rec_sb = ex_sb.post_fak(o, decision_ts_ns=1_000_000_000)
    assert rec_fd.classification == "full"
    assert rec_sb.classification == "full"
    assert rec_fd.fill_vwap != rec_sb.fill_vwap
    assert rec_sb.fill_vwap == pytest.approx(0.60)
    assert rec_fd.fill_vwap == pytest.approx(0.51)


def test_total_is_sums_components():
    book = _book(
        ts_ns=1_000_000_000,
        bids=[("0.48", "100")],
        asks=[("0.52", "5"), ("0.54", "100")],
    )
    store = _store([book])
    ex = ReplayExecutor(
        books=store,
        latency_profile=_fast_profile(),
        staleness_hard_ms=1_000_000,
        rng=random.Random(4),
    )
    # Request 10 shares, 5 at 0.52 then 5 at 0.54 -> VWAP 0.53.
    rec = ex.post_fak(_order(shares="10", mid="0.50"), decision_ts_ns=1_000_000_000)
    assert rec.classification == "full"
    total = rec.half_spread_cost + rec.book_walk_cost + rec.latency_drift_cost + rec.fees_cost
    assert rec.total_IS == pytest.approx(total, abs=1e-9)


def test_fields_full_schema_present():
    # Sanity: the dataclass exposes every §7 column name we care about.
    book = _book(ts_ns=0, bids=[("0.49", "10")], asks=[("0.51", "10")])
    store = _store([book])
    ex = ReplayExecutor(books=store, latency_profile=_fast_profile(), rng=random.Random(5))
    rec = ex.post_fak(_order(), decision_ts_ns=0)
    required = {
        "trade_id", "parent_order_id", "strategy_id", "backtest_run_id",
        "t_signal_ns", "t_send_ns", "t_ack_ns", "t_first_fill_ns", "t_last_fill_ns",
        "token_id", "market_id", "side", "tick_size", "decision_mid",
        "best_bid", "best_ask", "spread",
        "top_of_book_size_bid", "top_of_book_size_ask",
        "cumulative_depth_5bps", "cumulative_depth_20bps",
        "book_staleness_ms", "market_age_s", "market_remaining_s",
        "requested_qty_shares", "requested_notional_usdc", "worst_price_limit",
        "filled_qty", "residual_qty", "fill_vwap", "levels_consumed", "classification",
        "sampled_latency_ms", "latency_source", "p_bucket_used",
        "half_spread_cost", "book_walk_cost", "latency_drift_cost",
        "adverse_selection_1s", "adverse_selection_5s", "adverse_selection_30s",
        "fees_cost", "opportunity_cost_unfilled", "total_IS",
        "edge_at_signal", "edge_at_fill", "realised_pnl_at_close",
        "paper_pnl_flat_0_5", "diff_paper_minus_realised",
        "thin_book_flag", "price_extreme_flag", "vol_regime",
        "tunnel_age_bucket", "time_in_market_bucket", "mode",
    }
    present = {f.name for f in rec.__dataclass_fields__.values()}
    missing = required - present
    assert not missing, f"missing §7 columns: {missing}"


def test_thin_book_flag_triggers_when_top_size_small():
    book = _book(
        ts_ns=0,
        bids=[("0.49", "100")],
        asks=[("0.51", "5"), ("0.52", "100")],  # top ask only 5, requesting 10 -> thin
    )
    store = _store([book])
    ex = ReplayExecutor(books=store, latency_profile=_fast_profile(), rng=random.Random(6))
    rec = ex.post_fak(_order(shares="10"), decision_ts_ns=0)
    assert rec.thin_book_flag is True


def test_price_extreme_flag_near_zero():
    book = _book(ts_ns=0, bids=[("0.02", "100")], asks=[("0.04", "100")])
    store = _store([book])
    ex = ReplayExecutor(books=store, latency_profile=_fast_profile(), rng=random.Random(7))
    rec = ex.post_fak(_order(mid="0.03"), decision_ts_ns=0)
    assert rec.price_extreme_flag is True


def test_time_in_market_bucket_last_30s():
    # 5-minute market: t_zero=0, t_end=300s; snapshot at 290s so staleness is tiny;
    # decision at 290s -> last 10s bucket.
    book = _book(ts_ns=290_000_000_000, bids=[("0.49", "100")], asks=[("0.51", "100")])
    store = _store([book])
    ex = ReplayExecutor(books=store, latency_profile=_fast_profile(), rng=random.Random(8))
    order = OrderRequest(
        token_id="t1",
        side="BUY",
        requested_shares=Decimal("1"),
        worst_price_limit=Decimal("0.99"),
        decision_mid=Decimal("0.50"),
        category=CRYPTO,
        tick_size=TICK,
        t_zero_ns=0,
        t_end_ns=300_000_000_000,
    )
    rec = ex.post_fak(order, decision_ts_ns=290_000_000_000)
    assert rec.classification == "full"
    assert rec.time_in_market_bucket == "last_30s"


# ----- ConditionedSampler wiring -----


def _order_with_context(
    *, tunnel_age: str = "unk", vol: str = "unk"
) -> OrderRequest:
    return OrderRequest(
        token_id="t1",
        side="BUY",
        requested_shares=Decimal("1"),
        worst_price_limit=Decimal("0.99"),
        decision_mid=Decimal("0.50"),
        category=CRYPTO,
        tick_size=TICK,
        tunnel_age_bucket=tunnel_age,
        vol_regime=vol,
    )


def test_executor_uses_conditioned_sampler_when_given() -> None:
    book = _book(ts_ns=0, bids=[("0.49", "100")], asks=[("0.51", "100")])
    store = _store([book])
    sampler = ConditionedSampler(
        profiles={"fresh_low": DUBLIN_PRIOR},
        key_fn=lambda ctx: f"{ctx['tunnel_age_bucket']}_{ctx['vol_regime']}",
        fallback=SG_WG_PRIOR,
    )
    ex = ReplayExecutor(
        books=store,
        latency_sampler=sampler,
        staleness_hard_ms=10_000,
        rng=random.Random(1),
    )
    rec = ex.post_fak(
        _order_with_context(tunnel_age="fresh", vol="low"),
        decision_ts_ns=0,
    )
    # Matched bucket → p_bucket_used is the key verbatim.
    assert rec.p_bucket_used == "fresh_low"
    # Record should also echo back the context tags for downstream groupby.
    assert rec.tunnel_age_bucket == "fresh"
    assert rec.vol_regime == "low"


def test_executor_sampler_falls_back_when_bucket_missing() -> None:
    book = _book(ts_ns=0, bids=[("0.49", "100")], asks=[("0.51", "100")])
    store = _store([book])
    sampler = ConditionedSampler(
        profiles={"fresh_low": DUBLIN_PRIOR},  # bucket "stale_high" absent
        key_fn=lambda ctx: f"{ctx['tunnel_age_bucket']}_{ctx['vol_regime']}",
        fallback=SG_WG_PRIOR,
    )
    ex = ReplayExecutor(
        books=store,
        latency_sampler=sampler,
        staleness_hard_ms=10_000,
        rng=random.Random(2),
    )
    rec = ex.post_fak(
        _order_with_context(tunnel_age="stale", vol="high"),
        decision_ts_ns=0,
    )
    assert rec.p_bucket_used == "fallback:stale_high"


def test_executor_without_sampler_still_uses_single_profile() -> None:
    book = _book(ts_ns=0, bids=[("0.49", "100")], asks=[("0.51", "100")])
    store = _store([book])
    ex = ReplayExecutor(
        books=store,
        latency_profile=_fast_profile(),
        staleness_hard_ms=10_000,
        rng=random.Random(3),
    )
    rec = ex.post_fak(_order_with_context(), decision_ts_ns=0)
    assert rec.p_bucket_used == "base"
