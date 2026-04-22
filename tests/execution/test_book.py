"""Tests for active_bots.execution.book."""

from __future__ import annotations

from decimal import Decimal

import pytest

from active_bots.execution.book import (
    Book,
    FillResult,
    Level,
    ROUNDING_CONFIG,
    SIZE_QUANTUM,
    apply_deltas,
    freeze_last_book,
    quantize_price,
    walk_book,
)


def _mk_book(
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    tick: str = "0.01",
    ts_ns: int = 0,
) -> Book:
    return Book(
        token_id="tok",
        side_bids=tuple(Level(Decimal(p), Decimal(s)) for p, s in bids),
        side_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in asks),
        tick_size=Decimal(tick),
        ts_ns=ts_ns,
    )


def test_monotonic_fill_prices_buy() -> None:
    book = _mk_book(
        bids=[],
        asks=[("0.50", "5"), ("0.52", "5"), ("0.55", "5")],
    )
    res = walk_book(book, "BUY", Decimal("12"), Decimal("0.60"))
    prices = [f.price for f in res.levels]
    assert prices == sorted(prices)
    assert prices == [Decimal("0.50"), Decimal("0.52"), Decimal("0.55")]
    assert res.filled_qty == Decimal("12")


def test_monotonic_fill_prices_sell() -> None:
    book = _mk_book(
        bids=[("0.55", "5"), ("0.52", "5"), ("0.50", "5")],
        asks=[],
    )
    res = walk_book(book, "SELL", Decimal("12"), Decimal("0.40"))
    prices = [f.price for f in res.levels]
    assert prices == sorted(prices, reverse=True)
    assert prices == [Decimal("0.55"), Decimal("0.52"), Decimal("0.50")]
    assert res.filled_qty == Decimal("12")


def test_vwap_arithmetic() -> None:
    book = _mk_book(
        bids=[],
        asks=[("0.50", "10"), ("0.52", "10"), ("0.55", "10")],
    )
    res = walk_book(book, "BUY", Decimal("30"), Decimal("0.60"))
    assert res.classification == "full"
    # VWAP = (0.50*10 + 0.52*10 + 0.55*10) / 30 = 15.70 / 30
    expected = (Decimal("15.70") / Decimal("30")).quantize(Decimal("0.00000001"))
    got = res.vwap.quantize(Decimal("0.00000001"))
    assert got == expected
    assert got == Decimal("0.52333333")


def test_overfill_tolerance() -> None:
    # Request aligned to SIZE_QUANTUM; total depth slightly above it.
    # The #3221 mitigation should ensure filled_qty <= requested_qty.
    book = _mk_book(
        bids=[],
        asks=[("0.50", "5.005"), ("0.51", "5.005")],
    )
    requested = Decimal("10.00")
    res = walk_book(book, "BUY", requested, Decimal("0.60"))
    assert res.filled_qty <= requested
    # Second level take is remaining=10.00-5.00=5.00 quantised DOWN is 5.00
    # First take is 5.005 quantised DOWN to 5.00, second is 5.00 -> 10.00 full.
    assert res.filled_qty == Decimal("10.00")
    assert res.classification == "full"

    # Now a case that forces a sub-quantum residual: request 10.005.
    res2 = walk_book(book, "BUY", Decimal("10.005"), Decimal("0.60"))
    assert res2.filled_qty <= Decimal("10.005")
    # Both levels quantised DOWN to 5.00 each -> 10.00 filled.
    assert res2.filled_qty == Decimal("10.00")
    assert res2.classification == "partial"


def test_residual_math_partial() -> None:
    book = _mk_book(
        bids=[],
        asks=[("0.50", "3"), ("0.52", "2")],
    )
    res = walk_book(book, "BUY", Decimal("10"), Decimal("0.60"))
    assert res.classification == "partial"
    assert res.filled_qty == Decimal("5")
    assert res.residual_qty == Decimal("5")
    assert res.filled_qty + res.residual_qty == Decimal("10")


def test_residual_math_full() -> None:
    book = _mk_book(
        bids=[],
        asks=[("0.50", "10")],
    )
    res = walk_book(book, "BUY", Decimal("7"), Decimal("0.55"))
    assert res.classification == "full"
    assert res.filled_qty == Decimal("7")
    assert res.residual_qty == Decimal("0")
    assert res.levels_consumed == 1


def test_residual_math_unfilled() -> None:
    # Only ask is above the worst_price_limit.
    book = _mk_book(
        bids=[],
        asks=[("0.80", "10")],
    )
    res = walk_book(book, "BUY", Decimal("5"), Decimal("0.55"))
    assert res.classification == "unfilled"
    assert res.filled_qty == Decimal("0")
    assert res.residual_qty == Decimal("5")
    assert res.vwap is None
    assert res.levels == ()
    assert res.levels_consumed == 0


def test_tick_boundary_rejection_worst_price() -> None:
    book = _mk_book(
        bids=[],
        asks=[("0.50", "5")],
        tick="0.01",
    )
    # worst_price_limit = 0, below tick_size boundary [0.01, 0.99].
    with pytest.raises(ValueError):
        walk_book(book, "BUY", Decimal("3"), Decimal("0"))
    with pytest.raises(ValueError):
        walk_book(book, "BUY", Decimal("3"), Decimal("1.00"))


def test_tick_boundary_rejection_book_price() -> None:
    # Build a book with an ask at 0.00 which is outside [tick, 1-tick].
    bad_book = Book(
        token_id="tok",
        side_bids=(),
        side_asks=(Level(Decimal("0.00"), Decimal("5")),),
        tick_size=Decimal("0.01"),
        ts_ns=0,
    )
    with pytest.raises(ValueError):
        walk_book(bad_book, "BUY", Decimal("3"), Decimal("0.50"))

    bad_book2 = Book(
        token_id="tok",
        side_bids=(Level(Decimal("1.00"), Decimal("5")),),
        side_asks=(),
        tick_size=Decimal("0.01"),
        ts_ns=0,
    )
    with pytest.raises(ValueError):
        walk_book(bad_book2, "SELL", Decimal("3"), Decimal("0.50"))


def test_empty_side_buy() -> None:
    book = _mk_book(bids=[("0.40", "5")], asks=[])
    res = walk_book(book, "BUY", Decimal("5"), Decimal("0.60"))
    assert res.classification == "unfilled"
    assert res.filled_qty == Decimal("0")
    assert res.residual_qty == Decimal("5")
    assert res.vwap is None
    assert res.levels_consumed == 0


def test_empty_side_sell() -> None:
    book = _mk_book(bids=[], asks=[("0.60", "5")])
    res = walk_book(book, "SELL", Decimal("5"), Decimal("0.40"))
    assert res.classification == "unfilled"
    assert res.filled_qty == Decimal("0")
    assert res.residual_qty == Decimal("5")
    assert res.vwap is None
    assert res.levels_consumed == 0


def test_freeze_staleness_basic() -> None:
    snaps = [
        _mk_book([], [], ts_ns=100 * 1_000_000),
        _mk_book([], [], ts_ns=200 * 1_000_000),
        _mk_book([], [], ts_ns=300 * 1_000_000),
    ]
    snap, staleness_ms = freeze_last_book(snaps, 250 * 1_000_000)
    assert snap.ts_ns == 200 * 1_000_000
    assert staleness_ms == 50


def test_freeze_raises_when_no_prior_snapshot() -> None:
    snaps = [
        _mk_book([], [], ts_ns=200 * 1_000_000),
        _mk_book([], [], ts_ns=300 * 1_000_000),
    ]
    with pytest.raises(LookupError):
        freeze_last_book(snaps, 100 * 1_000_000)
    with pytest.raises(LookupError):
        freeze_last_book([], 100 * 1_000_000)


def test_freeze_requires_sorted_snapshots() -> None:
    snaps = [
        _mk_book([], [], ts_ns=300 * 1_000_000),
        _mk_book([], [], ts_ns=100 * 1_000_000),
        _mk_book([], [], ts_ns=200 * 1_000_000),
    ]
    with pytest.raises(ValueError):
        freeze_last_book(snaps, 250 * 1_000_000)


def test_quantize_price_valid_ticks() -> None:
    # Round-half-even: 0.125 -> 0.12 at 2dp (banker's rounding).
    assert quantize_price(Decimal("0.125"), Decimal("0.01")) == Decimal("0.12")
    # 0.135 -> 0.14 at 2dp (banker's rounding to even).
    assert quantize_price(Decimal("0.135"), Decimal("0.01")) == Decimal("0.14")
    # 0.5 -> 0.5 at 1dp.
    assert quantize_price(Decimal("0.5"), Decimal("0.1")) == Decimal("0.5")
    # 0.12345 -> 0.1235 at 4dp (banker's rounding: 4 is even, 5 rounds to even).
    assert quantize_price(Decimal("0.12345"), Decimal("0.0001")) == Decimal("0.1234")
    # Spot-check all keys accepted.
    for k in ROUNDING_CONFIG:
        quantize_price(Decimal("0.5"), Decimal(k))


def test_quantize_price_unknown_tick_raises() -> None:
    with pytest.raises(ValueError):
        quantize_price(Decimal("0.5"), Decimal("0.05"))
    with pytest.raises(ValueError):
        quantize_price(Decimal("0.5"), Decimal("0.02"))
    # float input must be rejected on the boundary.
    with pytest.raises(TypeError):
        quantize_price(0.5, Decimal("0.01"))  # type: ignore[arg-type]


# ----- Per-side tick size (spec §2.2 / §8.2 nautilus_trader #2980) -----


def test_per_side_tick_inherits_from_default() -> None:
    b = _mk_book(
        bids=[("0.49", "10")],
        asks=[("0.51", "10")],
        tick="0.01",
    )
    assert b.bids_tick == Decimal("0.01")
    assert b.asks_tick == Decimal("0.01")


def test_per_side_tick_asymmetric_finer_asks() -> None:
    # YES-side book near the top end: asks live at 0.001-tick precision
    # while bids are still on 0.01 tick (the asymmetric case from #2980).
    b = Book(
        token_id="tok",
        side_bids=(Level(Decimal("0.49"), Decimal("10")),),
        side_asks=(Level(Decimal("0.999"), Decimal("10")),),
        tick_size=Decimal("0.01"),
        tick_size_asks=Decimal("0.001"),
        ts_ns=0,
    )
    assert b.bids_tick == Decimal("0.01")
    assert b.asks_tick == Decimal("0.001")
    # walk_book must accept a 0.999 ask on a 0.001-tick side without rejecting
    # the book. A 0.01-tick validator would have thrown because 0.999 > 0.99.
    res = walk_book(
        b,
        side="BUY",
        requested_shares=Decimal("5"),
        worst_price_limit=Decimal("0.999"),
    )
    assert res.classification == "full"
    assert res.vwap == Decimal("0.999")


def test_walk_rejects_worst_price_outside_sided_tick_bounds() -> None:
    # asks_tick = 0.01 → limit 0.999 should be rejected for BUY (outside [0.01, 0.99])
    # even though bids_tick=0.001 would allow it.
    b = Book(
        token_id="tok",
        side_bids=(Level(Decimal("0.499"), Decimal("10")),),
        side_asks=(Level(Decimal("0.51"), Decimal("10")),),
        tick_size=Decimal("0.01"),
        tick_size_bids=Decimal("0.001"),
        ts_ns=0,
    )
    with pytest.raises(ValueError, match="py-clob-client #218"):
        walk_book(
            b, side="BUY",
            requested_shares=Decimal("5"),
            worst_price_limit=Decimal("0.999"),
        )


# ----- apply_deltas -----


def test_apply_deltas_add_new_level() -> None:
    base = _mk_book(
        bids=[("0.49", "10")],
        asks=[("0.51", "10")],
        ts_ns=1000,
    )
    deltas = [{"side": "SELL", "price": "0.52", "size": "7"}]
    new = apply_deltas(base, deltas, new_ts_ns=2000)
    assert new.ts_ns == 2000
    assert new.side_asks == (
        Level(Decimal("0.51"), Decimal("10")),
        Level(Decimal("0.52"), Decimal("7")),
    )
    # base is untouched (frozen)
    assert base.ts_ns == 1000
    assert len(base.side_asks) == 1


def test_apply_deltas_update_existing_level() -> None:
    base = _mk_book(
        bids=[("0.49", "10")],
        asks=[("0.51", "10")],
    )
    deltas = [{"side": "SELL", "price": "0.51", "size": "25"}]
    new = apply_deltas(base, deltas)
    assert new.side_asks == (Level(Decimal("0.51"), Decimal("25")),)


def test_apply_deltas_remove_level_with_zero_size() -> None:
    base = _mk_book(
        bids=[("0.49", "10"), ("0.48", "5")],
        asks=[("0.51", "10")],
    )
    deltas = [{"side": "BUY", "price": "0.48", "size": "0"}]
    new = apply_deltas(base, deltas)
    assert new.side_bids == (Level(Decimal("0.49"), Decimal("10")),)


def test_apply_deltas_sort_invariant_preserved() -> None:
    # Asks should remain ascending; bids descending, regardless of delta order.
    base = _mk_book(
        bids=[("0.49", "10")],
        asks=[("0.55", "10")],
    )
    deltas = [
        {"side": "SELL", "price": "0.60", "size": "5"},
        {"side": "SELL", "price": "0.52", "size": "5"},
        {"side": "BUY", "price": "0.45", "size": "3"},
        {"side": "BUY", "price": "0.48", "size": "4"},
    ]
    new = apply_deltas(base, deltas)
    ask_prices = [lvl.price for lvl in new.side_asks]
    assert ask_prices == sorted(ask_prices)
    bid_prices = [lvl.price for lvl in new.side_bids]
    assert bid_prices == sorted(bid_prices, reverse=True)


def test_apply_deltas_ignores_unknown_side() -> None:
    base = _mk_book(bids=[("0.49", "10")], asks=[("0.51", "10")])
    # "BID" / "ASK" (wrong nouns) should be no-ops rather than mutate the book
    deltas = [{"side": "BID", "price": "0.40", "size": "99"}]
    new = apply_deltas(base, deltas)
    assert new.side_bids == base.side_bids
    assert new.side_asks == base.side_asks


def test_apply_deltas_rejects_float_price() -> None:
    base = _mk_book(bids=[("0.49", "10")], asks=[("0.51", "10")])
    with pytest.raises(TypeError):
        apply_deltas(base, [{"side": "BUY", "price": 0.48, "size": "5"}])  # type: ignore[dict-item]
