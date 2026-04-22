"""Spec section 6.2 invariants for the book-walked replay backtester.

Each invariant is a plain, stateless function that raises
``AssertionError`` with a descriptive message (including offending numeric
values) on violation. They are callable from both tests and the harness and
have no side effects.

Decimal is used on the walk boundary (prices, quantities). Floats and
``math.isclose`` are used only for fee symmetry and for summing the
``ExecutionRecord`` basis-point columns (which are stored as floats).
"""

from __future__ import annotations

import math
from decimal import Decimal

from .book import Book, FillResult, SIZE_QUANTUM
from .fees import FeeCategory, fee_usdc
from .replay_executor import ExecutionRecord


_VWAP_ABS_TOL = Decimal("1e-12")
_TOTAL_IS_BPS_TOL = 1e-6  # tolerance in bps for total_IS sum check


def invariant_monotonic_fill_prices(side: str, fill: FillResult) -> None:
    """Spec section 6.2: filled level prices are monotonically
    non-decreasing for BUY, non-increasing for SELL."""
    if side not in ("BUY", "SELL"):
        raise AssertionError(f"invalid side {side!r}")
    prev: Decimal | None = None
    for i, lvl in enumerate(fill.levels):
        if prev is not None:
            if side == "BUY" and lvl.price < prev:
                raise AssertionError(
                    f"BUY fill prices not non-decreasing at level {i}: "
                    f"prev={prev} curr={lvl.price} levels={fill.levels}"
                )
            if side == "SELL" and lvl.price > prev:
                raise AssertionError(
                    f"SELL fill prices not non-increasing at level {i}: "
                    f"prev={prev} curr={lvl.price} levels={fill.levels}"
                )
        prev = lvl.price


def invariant_fill_qty_bounds(requested: Decimal, fill: FillResult) -> None:
    """filled_qty in [0, requested] with SIZE_QUANTUM overfill tolerance."""
    if fill.filled_qty < Decimal(0):
        raise AssertionError(
            f"filled_qty negative: filled_qty={fill.filled_qty}"
        )
    upper = requested + SIZE_QUANTUM
    if fill.filled_qty > upper:
        raise AssertionError(
            f"filled_qty {fill.filled_qty} exceeds requested {requested} "
            f"by more than SIZE_QUANTUM={SIZE_QUANTUM}"
        )


def invariant_vwap_arithmetic(fill: FillResult) -> None:
    """vwap == sum(p*q)/sum(q) over fill.levels when filled_qty > 0;
    None when classification == 'unfilled' (filled_qty == 0)."""
    if fill.filled_qty == Decimal(0):
        if fill.vwap is not None:
            raise AssertionError(
                f"unfilled result has non-None vwap={fill.vwap}"
            )
        return
    if fill.vwap is None:
        raise AssertionError(
            f"filled_qty={fill.filled_qty} > 0 but vwap is None"
        )
    notional = sum(
        (lvl.price * lvl.qty for lvl in fill.levels), start=Decimal(0)
    )
    qty = sum((lvl.qty for lvl in fill.levels), start=Decimal(0))
    if qty == Decimal(0):
        raise AssertionError(
            f"filled_qty={fill.filled_qty} but levels qty sum is zero"
        )
    expected = notional / qty
    if abs(expected - fill.vwap) > _VWAP_ABS_TOL:
        raise AssertionError(
            f"vwap mismatch: reported={fill.vwap} expected={expected} "
            f"notional={notional} qty={qty}"
        )


def invariant_fee_symmetry(
    p: Decimal, shares: Decimal, category: FeeCategory
) -> None:
    """fee(p) == fee(1 - p) and fee is non-negative."""
    fa = fee_usdc(p, shares, category)
    fb = fee_usdc(Decimal(1) - p, shares, category)
    if fa < Decimal(0):
        raise AssertionError(
            f"fee negative: fee({p})={fa} shares={shares} cat={category.name}"
        )
    if fb < Decimal(0):
        raise AssertionError(
            f"fee negative: fee({Decimal(1)-p})={fb} shares={shares} "
            f"cat={category.name}"
        )
    # Fees can be dust-clipped to 0 on either side of the bell, so we
    # compare as floats via math.isclose with a generous relative tolerance.
    if not math.isclose(float(fa), float(fb), rel_tol=1e-12, abs_tol=1e-12):
        raise AssertionError(
            f"fee asymmetric: fee({p})={fa} fee(1-{p})={fb} "
            f"shares={shares} cat={category.name}"
        )


def invariant_residual_conservation(
    requested: Decimal, fill: FillResult
) -> None:
    """filled_qty + residual_qty == requested (within SIZE_QUANTUM tol)."""
    total = fill.filled_qty + fill.residual_qty
    if abs(total - requested) > SIZE_QUANTUM:
        raise AssertionError(
            f"residual conservation: filled={fill.filled_qty} "
            f"residual={fill.residual_qty} sum={total} requested={requested}"
        )


def invariant_price_bounds_respect_tick(book: Book) -> None:
    """Every level price is in [tick_size, 1 - tick_size]."""
    lo = book.tick_size
    hi = Decimal(1) - book.tick_size
    for lvl in book.side_bids:
        if lvl.price < lo or lvl.price > hi:
            raise AssertionError(
                f"bid price {lvl.price} outside [{lo}, {hi}] tick={book.tick_size}"
            )
    for lvl in book.side_asks:
        if lvl.price < lo or lvl.price > hi:
            raise AssertionError(
                f"ask price {lvl.price} outside [{lo}, {hi}] tick={book.tick_size}"
            )


def invariant_staleness_monotonic_within_session(
    records: list[ExecutionRecord],
) -> None:
    """For records sorted by t_signal_ns, book_staleness_ms is
    non-decreasing per token_id group when t_signal_ns is non-decreasing.
    Inter-token order is not constrained. Staleness values < 0 are ignored."""
    per_token: dict[str, tuple[int, int]] = {}
    sorted_recs = sorted(records, key=lambda r: r.t_signal_ns)
    for rec in sorted_recs:
        if rec.book_staleness_ms < 0:
            continue
        prev = per_token.get(rec.token_id)
        if prev is not None:
            prev_ts, prev_stale = prev
            if rec.t_signal_ns >= prev_ts and rec.book_staleness_ms < prev_stale:
                raise AssertionError(
                    f"staleness decreased for token={rec.token_id}: "
                    f"prev_ts={prev_ts} prev_stale={prev_stale} "
                    f"curr_ts={rec.t_signal_ns} curr_stale={rec.book_staleness_ms}"
                )
        per_token[rec.token_id] = (rec.t_signal_ns, rec.book_staleness_ms)


def invariant_total_is_sum(rec: ExecutionRecord) -> None:
    """For non-book_stale, non-unfilled rows, total_IS ==
    half_spread_cost + latency_drift_cost + book_walk_cost + fees_cost
    to within 1e-6 bps. Rows with any NaN component are skipped."""
    if rec.classification in ("book_stale", "unfilled"):
        return
    comps = (
        rec.half_spread_cost,
        rec.latency_drift_cost,
        rec.book_walk_cost,
        rec.fees_cost,
    )
    if any(math.isnan(v) for v in comps) or math.isnan(rec.total_IS):
        return
    expected = sum(comps)
    if not math.isclose(
        rec.total_IS, expected, rel_tol=0.0, abs_tol=_TOTAL_IS_BPS_TOL
    ):
        raise AssertionError(
            f"total_IS mismatch: total_IS={rec.total_IS} expected={expected} "
            f"half_spread={rec.half_spread_cost} "
            f"latency_drift={rec.latency_drift_cost} "
            f"book_walk={rec.book_walk_cost} fees={rec.fees_cost}"
        )
