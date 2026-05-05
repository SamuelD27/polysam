"""Property-based tests for spec section 6.2 invariants.

Hand-crafted golden cases exercise the three walk_book classifications.
Hypothesis-generated random books exercise the walk-side invariants on
both BUY and SELL. A small ReplayExecutor scenario validates the
total_IS == sum(components) invariant end-to-end.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from active_bots.execution.book import (
    Book,
    FillResult,
    Level,
    walk_book,
)
from active_bots.execution.fees import CRYPTO, FINANCE, GEOPOLITICS
from active_bots.execution.invariants import (
    invariant_fee_symmetry,
    invariant_fill_qty_bounds,
    invariant_monotonic_fill_prices,
    invariant_order_price_respects_218_bound,
    invariant_price_bounds_respect_tick,
    invariant_residual_conservation,
    invariant_total_is_sum,
    invariant_vwap_arithmetic,
)
from active_bots.execution.latency import LatencyProfile
from active_bots.execution.replay_executor import (
    DictBookStore,
    OrderRequest,
    ReplayExecutor,
)

TICK = Decimal("0.01")


# ----------------------------- golden cases -----------------------------


def _make_book(
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    *,
    ts_ns: int = 1_000_000_000,
    token_id: str = "tok",
    tick: Decimal = TICK,
) -> Book:
    return Book(
        token_id=token_id,
        side_bids=tuple(Level(Decimal(p), Decimal(s)) for p, s in bids),
        side_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in asks),
        tick_size=tick,
        ts_ns=ts_ns,
    )


def test_golden_full_fill_3_level_exhaustion():
    book = _make_book(
        bids=[("0.49", "100")],
        asks=[("0.51", "5"), ("0.52", "5"), ("0.53", "5")],
    )
    fill = walk_book(book, "BUY", Decimal("15"), Decimal("0.99"))

    assert fill.classification == "full"
    assert fill.filled_qty == Decimal("15")
    assert fill.residual_qty == Decimal("0")
    assert fill.levels_consumed == 3
    assert fill.vwap == Decimal("0.52")

    invariant_monotonic_fill_prices("BUY", fill)
    invariant_fill_qty_bounds(Decimal("15"), fill)
    invariant_vwap_arithmetic(fill)
    invariant_residual_conservation(Decimal("15"), fill)
    invariant_price_bounds_respect_tick(book)


def test_golden_partial_fill_at_worst_price_limit():
    book = _make_book(
        bids=[("0.49", "100")],
        asks=[("0.51", "5"), ("0.60", "5")],
    )
    fill = walk_book(book, "BUY", Decimal("20"), Decimal("0.55"))

    assert fill.classification == "partial"
    assert fill.filled_qty == Decimal("5")
    assert fill.residual_qty == Decimal("15")
    assert fill.levels_consumed == 1

    invariant_monotonic_fill_prices("BUY", fill)
    invariant_fill_qty_bounds(Decimal("20"), fill)
    invariant_vwap_arithmetic(fill)
    invariant_residual_conservation(Decimal("20"), fill)


def test_golden_unfilled_empty_side():
    book = _make_book(
        bids=[("0.49", "100")],
        asks=[],
    )
    fill = walk_book(book, "BUY", Decimal("10"), Decimal("0.99"))

    assert fill.classification == "unfilled"
    assert fill.vwap is None
    assert fill.filled_qty == Decimal("0")
    assert fill.residual_qty == Decimal("10")
    assert fill.levels_consumed == 0

    invariant_vwap_arithmetic(fill)
    invariant_fill_qty_bounds(Decimal("10"), fill)
    invariant_residual_conservation(Decimal("10"), fill)


# ----------------------------- hypothesis strategies -----------------------------


_TICK_D = TICK
_LO = _TICK_D  # 0.01
_HI = Decimal("1") - _TICK_D  # 0.99
_ASK_FLOOR = Decimal("0.51")  # keep asks strictly above mirror region
_BID_CEIL = Decimal("0.49")  # keep bids strictly below mirror region


@st.composite
def _book_strategy(draw: st.DrawFn) -> Book:
    n_asks = draw(st.integers(min_value=2, max_value=6))

    # Strictly ascending ask prices in [0.51, 0.99], quantised to tick.
    ask_prices_set: set[Decimal] = set()
    # Draw more candidates than needed, dedupe, sort, clamp.
    candidates = draw(
        st.lists(
            st.decimals(
                min_value=_ASK_FLOOR,
                max_value=_HI,
                places=2,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=n_asks,
            max_size=n_asks * 3,
        )
    )
    for c in candidates:
        ask_prices_set.add(Decimal(c))
        if len(ask_prices_set) >= n_asks:
            break
    assume(len(ask_prices_set) >= n_asks)
    ask_prices = sorted(ask_prices_set)[:n_asks]

    ask_sizes = draw(
        st.lists(
            st.decimals(
                min_value=Decimal("0.01"),
                max_value=Decimal("1000"),
                places=2,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=n_asks,
            max_size=n_asks,
        )
    )

    asks = tuple(
        Level(price=Decimal(p), size=Decimal(s))
        for p, s in zip(ask_prices, ask_sizes, strict=True)
    )

    # Mirror bids below 0.50: same count, descending prices in [0.01, 0.49].
    mirror_prices = [Decimal("1") - p for p in ask_prices]
    # ensure within [_LO, _BID_CEIL]
    mirror_prices = [
        p if p <= _BID_CEIL else _BID_CEIL for p in mirror_prices
    ]
    mirror_prices = [p if p >= _LO else _LO for p in mirror_prices]
    # descending sort for bids
    bid_prices = sorted(set(mirror_prices), reverse=True)
    assume(len(bid_prices) >= 1)
    bid_sizes = draw(
        st.lists(
            st.decimals(
                min_value=Decimal("0.01"),
                max_value=Decimal("1000"),
                places=2,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=len(bid_prices),
            max_size=len(bid_prices),
        )
    )
    bids = tuple(
        Level(price=Decimal(p), size=Decimal(s))
        for p, s in zip(bid_prices, bid_sizes, strict=True)
    )

    return Book(
        token_id="hyp",
        side_bids=bids,
        side_asks=asks,
        tick_size=_TICK_D,
        ts_ns=0,
    )


@given(book=_book_strategy(), side=st.sampled_from(["BUY", "SELL"]))
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_hypothesis_random_books_satisfy_monotonic_fill(book: Book, side: str):
    # Must have a non-empty side for this walk.
    if side == "BUY":
        assume(len(book.side_asks) > 0)
        # choose a requested qty that may exceed top, and a worst-price limit
        # that permits at least some fills.
        worst = _HI
    else:
        assume(len(book.side_bids) > 0)
        worst = _LO

    requested = Decimal("25.00")
    fill: FillResult = walk_book(book, side, requested, worst)  # type: ignore[arg-type]

    invariant_monotonic_fill_prices(side, fill)
    invariant_fill_qty_bounds(requested, fill)
    invariant_residual_conservation(requested, fill)
    invariant_vwap_arithmetic(fill)
    invariant_price_bounds_respect_tick(book)


@given(
    p=st.decimals(
        min_value=Decimal("0.00"),
        max_value=Decimal("1.00"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    shares=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("10000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    category=st.sampled_from([CRYPTO, FINANCE, GEOPOLITICS]),
)
@settings(max_examples=100, deadline=None)
def test_hypothesis_fee_symmetry(p, shares, category):
    invariant_fee_symmetry(Decimal(p), Decimal(shares), category)


# ----------------------------- end-to-end total_IS -----------------------------


def _fast_profile() -> LatencyProfile:
    return LatencyProfile(1.0, 2.0, 3.0, 5.0, "prior")


def _book_for_exec(
    ts_ns: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    tok: str = "t1",
) -> Book:
    return Book(
        token_id=tok,
        side_bids=tuple(Level(Decimal(p), Decimal(s)) for p, s in bids),
        side_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in asks),
        tick_size=TICK,
        ts_ns=ts_ns,
    )


def test_invariant_total_is_sum_passes_on_fresh_fill():
    book = _book_for_exec(
        ts_ns=1_000_000_000,
        bids=[("0.49", "100")],
        asks=[("0.51", "100"), ("0.52", "100")],
    )
    store = DictBookStore()
    store.add(book)
    store.freeze()
    ex = ReplayExecutor(
        books=store,
        latency_profile=_fast_profile(),
        rng=random.Random(0),
    )
    order = OrderRequest(
        token_id="t1",
        side="BUY",
        requested_shares=Decimal("10"),
        worst_price_limit=Decimal("0.99"),
        decision_mid=Decimal("0.50"),
        category=CRYPTO,
        tick_size=TICK,
    )
    rec = ex.post_fak(order, decision_ts_ns=1_000_000_000)

    assert rec.classification == "full"
    invariant_total_is_sum(rec)


def test_invariant_total_is_sum_skips_book_stale():
    # Book at ts_ns=0, decision at 1s -> staleness 1000ms > hard 500ms.
    book = _book_for_exec(
        ts_ns=0,
        bids=[("0.49", "100")],
        asks=[("0.51", "100")],
    )
    store = DictBookStore()
    store.add(book)
    store.freeze()
    ex = ReplayExecutor(
        books=store,
        latency_profile=_fast_profile(),
        staleness_hard_ms=500,
        rng=random.Random(0),
    )
    order = OrderRequest(
        token_id="t1",
        side="BUY",
        requested_shares=Decimal("10"),
        worst_price_limit=Decimal("0.99"),
        decision_mid=Decimal("0.50"),
        category=CRYPTO,
        tick_size=TICK,
    )
    rec = ex.post_fak(order, decision_ts_ns=1_000_000_000)

    assert rec.classification == "book_stale"
    # Should not raise (skipped per contract).
    invariant_total_is_sum(rec)


# ----- py-clob-client #218 REST validator bound -----


def _mk_book(bids, asks, *, tick="0.01", tick_bids=None, tick_asks=None) -> Book:
    return Book(
        token_id="tok",
        side_bids=tuple(Level(Decimal(p), Decimal(s)) for p, s in bids),
        side_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in asks),
        tick_size=Decimal(tick),
        tick_size_bids=Decimal(tick_bids) if tick_bids else None,
        tick_size_asks=Decimal(tick_asks) if tick_asks else None,
        ts_ns=0,
    )


def test_invariant_218_rejects_buy_near_one() -> None:
    # 0.01-tick BUY with worst_price_limit=0.995 is the exact #218 case:
    # frontend accepts it, REST rejects on submit. Replay must match REST.
    book = _mk_book(bids=[("0.49", "10")], asks=[("0.51", "10")])
    with pytest.raises(AssertionError, match="py-clob-client/issues/218"):
        invariant_order_price_respects_218_bound(
            worst_price_limit=Decimal("0.995"),
            side="BUY",
            book=book,
        )


def test_invariant_218_rejects_sell_near_zero() -> None:
    # 0.01-tick SELL with worst_price_limit=0.005 — analogous near-zero case.
    book = _mk_book(bids=[("0.49", "10")], asks=[("0.51", "10")])
    with pytest.raises(AssertionError, match="py-clob-client/issues/218"):
        invariant_order_price_respects_218_bound(
            worst_price_limit=Decimal("0.005"),
            side="SELL",
            book=book,
        )


def test_invariant_218_accepts_exact_tick_bounds() -> None:
    # Exactly at [tick, 1-tick] must pass (0.01 and 0.99 on a 0.01 tick).
    book = _mk_book(bids=[("0.49", "10")], asks=[("0.51", "10")])
    invariant_order_price_respects_218_bound(
        worst_price_limit=Decimal("0.99"), side="BUY", book=book,
    )
    invariant_order_price_respects_218_bound(
        worst_price_limit=Decimal("0.01"), side="SELL", book=book,
    )


def test_invariant_218_uses_per_side_tick() -> None:
    # Asks side runs 0.001 tick (YES near extremes). A 0.995 BUY is now
    # legitimate; a 0.0005 BUY still isn't because asks_tick=0.001 floor.
    book = _mk_book(
        bids=[("0.49", "10")],
        asks=[("0.995", "10")],
        tick="0.01",
        tick_asks="0.001",
    )
    # Accepts at 0.995 on asks_tick=0.001
    invariant_order_price_respects_218_bound(
        worst_price_limit=Decimal("0.995"), side="BUY", book=book,
    )
    # Still rejects below 0.001
    with pytest.raises(AssertionError, match="py-clob-client/issues/218"):
        invariant_order_price_respects_218_bound(
            worst_price_limit=Decimal("0.0005"),
            side="BUY",
            book=book,
        )


def test_invariant_price_bounds_uses_per_side_tick() -> None:
    # A book whose asks live at 0.999 (0.001 tick) must not trigger a 0.01-tick
    # bound violation on the bid side — per-side ticks are independent.
    book = _mk_book(
        bids=[("0.49", "10")],
        asks=[("0.999", "10")],
        tick="0.01",
        tick_asks="0.001",
    )
    invariant_price_bounds_respect_tick(book)  # must not raise
