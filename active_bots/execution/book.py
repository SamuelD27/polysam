"""Immutable L2 orderbook primitives for the replay backtester.

Pure Decimal arithmetic. No network, no daemon state, no floats on the
boundary. Implements spec sections:

- Section 2.2: ``ROUNDING_CONFIG`` tick-to-decimal-places mapping and
  round-half-even price quantisation.
- Section 2.4: ``walk_book`` FAK/IOC level walk with the
  ``nautilus_trader`` #3221 overfill mitigation (quantise last fill qty
  DOWN to ``SIZE_QUANTUM`` when accumulated floating imprecision would
  otherwise push us past the requested quantity).
- Section 1.3: ``freeze_last_book`` returns the latest snapshot with
  ``ts_ns <= t_query_ns`` and its ``staleness_ms``.

This module is imported directly by ``replay_executor.py``; it does not
import from ``replay_executor`` or ``harness`` and has no side effects.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Literal

# Spec section 2.2: tick_size (string key) -> price decimal places.
ROUNDING_CONFIG: dict[str, int] = {
    "0.1": 1,
    "0.01": 2,
    "0.001": 3,
    "0.0001": 4,
}

# Size always has 2dp per spec section 2.2.
SIZE_DECIMALS: int = 2
SIZE_QUANTUM: Decimal = Decimal("0.01")

assert SIZE_DECIMALS == 2


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class Book:
    token_id: str
    side_bids: tuple["Level", ...]
    side_asks: tuple["Level", ...]
    tick_size: Decimal
    ts_ns: int
    source_seq: int | None = None


@dataclass(frozen=True)
class FillLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class FillResult:
    classification: Literal["full", "partial", "unfilled"]
    filled_qty: Decimal
    residual_qty: Decimal
    levels: tuple["FillLevel", ...]
    vwap: Decimal | None
    levels_consumed: int


def _reject_float(*values: object) -> None:
    for v in values:
        if isinstance(v, float):
            raise TypeError(
                "float not accepted on boundary; pass Decimal or str"
            )


def _tick_key(tick_size: Decimal) -> str:
    # Match against ROUNDING_CONFIG keys. Decimal("0.01") == Decimal("0.01")
    # but str(Decimal("0.010")) == "0.010" so we normalise via exact lookup.
    for k in ROUNDING_CONFIG:
        if Decimal(k) == tick_size:
            return k
    raise ValueError(f"unknown tick_size {tick_size!r}")


def quantize_price(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round ``price`` to the decimal places implied by ``tick_size``.

    Uses banker's rounding (ROUND_HALF_EVEN). Raises ``ValueError`` if
    ``tick_size`` is not one of the ``ROUNDING_CONFIG`` keys.
    """
    _reject_float(price, tick_size)
    if not isinstance(price, Decimal):
        price = Decimal(str(price))
    if not isinstance(tick_size, Decimal):
        tick_size = Decimal(str(tick_size))
    key = _tick_key(tick_size)
    dp = ROUNDING_CONFIG[key]
    quant = Decimal(1).scaleb(-dp)
    return price.quantize(quant, rounding=ROUND_HALF_EVEN)


def freeze_last_book(
    snapshots: list[Book], t_query_ns: int
) -> tuple[Book, int]:
    """Return the latest snapshot at or before ``t_query_ns`` and its
    staleness in milliseconds.

    Snapshots must be sorted ascending by ``ts_ns``; a ``ValueError`` is
    raised otherwise. A ``LookupError`` is raised if no snapshot with
    ``ts_ns <= t_query_ns`` exists.
    """
    _reject_float(t_query_ns)
    if not snapshots:
        raise LookupError("no snapshots available")
    ts_list = [s.ts_ns for s in snapshots]
    for i in range(1, len(ts_list)):
        if ts_list[i] < ts_list[i - 1]:
            raise ValueError("snapshots must be sorted ascending by ts_ns")
    idx = bisect.bisect_right(ts_list, t_query_ns) - 1
    if idx < 0:
        raise LookupError(
            f"no snapshot with ts_ns <= {t_query_ns}"
        )
    snap = snapshots[idx]
    staleness_ms = (t_query_ns - snap.ts_ns) // 1_000_000
    return snap, staleness_ms


def _validate_book_prices(book: Book) -> None:
    tick = book.tick_size
    _tick_key(tick)  # raises ValueError if unknown
    lo = tick
    hi = Decimal(1) - tick
    for lvl in book.side_bids:
        if lvl.price < lo or lvl.price > hi:
            raise ValueError(
                f"bid price {lvl.price} outside [{lo}, {hi}]"
            )
    for lvl in book.side_asks:
        if lvl.price < lo or lvl.price > hi:
            raise ValueError(
                f"ask price {lvl.price} outside [{lo}, {hi}]"
            )


def walk_book(
    book: Book,
    side: Literal["BUY", "SELL"],
    requested_shares: Decimal,
    worst_price_limit: Decimal,
) -> FillResult:
    """Walk the opposite side of ``book`` level-by-level, FAK/IOC.

    BUY consumes asks ascending and requires each fill price to be
    ``<= worst_price_limit``. SELL consumes bids descending and requires
    each fill price to be ``>= worst_price_limit``. Stops on exhaustion,
    price-limit violation, or empty side.

    Applies the ``nautilus_trader`` #3221 mitigation: if accumulated
    Decimal imprecision would overshoot ``requested_shares`` by less
    than one ``SIZE_QUANTUM``, the last level's fill qty is quantised
    DOWN to the nearest quantum so ``filled_qty <= requested_shares``.
    """
    _reject_float(requested_shares, worst_price_limit)
    if not isinstance(requested_shares, Decimal):
        requested_shares = Decimal(str(requested_shares))
    if not isinstance(worst_price_limit, Decimal):
        worst_price_limit = Decimal(str(worst_price_limit))

    _validate_book_prices(book)
    tick = book.tick_size
    lo = tick
    hi = Decimal(1) - tick
    if worst_price_limit < lo or worst_price_limit > hi:
        raise ValueError(
            f"worst_price_limit {worst_price_limit} outside [{lo}, {hi}]"
        )

    if side == "BUY":
        side_levels = book.side_asks
        price_ok = lambda p: p <= worst_price_limit  # noqa: E731
    elif side == "SELL":
        side_levels = book.side_bids
        price_ok = lambda p: p >= worst_price_limit  # noqa: E731
    else:
        raise ValueError(f"invalid side {side!r}")

    remaining = requested_shares
    fills: list[FillLevel] = []
    levels_consumed = 0
    for lvl in side_levels:
        if remaining <= Decimal(0):
            break
        if not price_ok(lvl.price):
            break
        take = lvl.size if lvl.size < remaining else remaining
        # #3221 mitigation: quantise DOWN so we never overfill.
        take = take.quantize(SIZE_QUANTUM, rounding=ROUND_DOWN)
        if take <= Decimal(0):
            break
        fills.append(FillLevel(price=lvl.price, qty=take))
        remaining -= take
        levels_consumed += 1

    filled_qty = sum((f.qty for f in fills), start=Decimal(0))
    residual_qty = requested_shares - filled_qty
    if residual_qty < Decimal(0):
        # Defensive: should be impossible given ROUND_DOWN above.
        raise AssertionError("overfill detected; #3221 guard failed")

    if filled_qty == Decimal(0):
        classification: Literal["full", "partial", "unfilled"] = "unfilled"
        vwap: Decimal | None = None
    elif filled_qty == requested_shares:
        classification = "full"
        notional = sum((f.price * f.qty for f in fills), start=Decimal(0))
        vwap = notional / filled_qty
    else:
        classification = "partial"
        notional = sum((f.price * f.qty for f in fills), start=Decimal(0))
        vwap = notional / filled_qty

    return FillResult(
        classification=classification,
        filled_qty=filled_qty,
        residual_qty=residual_qty,
        levels=tuple(fills),
        vwap=vwap,
        levels_consumed=levels_consumed,
    )
