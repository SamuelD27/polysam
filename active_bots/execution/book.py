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
import dataclasses
from collections.abc import Iterable
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
    side_bids: tuple[Level, ...]
    side_asks: tuple[Level, ...]
    tick_size: Decimal  # symmetric default (also back-compat)
    ts_ns: int
    source_seq: int | None = None
    # Per-side overrides (spec §2.2 / §8.2 nautilus_trader #2980): when the YES
    # token runs a finer tick than the NO token on the same market, or during
    # a mid-stream tick_size_change on only one side. Leave None to inherit
    # tick_size.
    tick_size_bids: Decimal | None = None
    tick_size_asks: Decimal | None = None

    @property
    def bids_tick(self) -> Decimal:
        """Effective tick size on the bid side (per-side override or shared)."""
        return self.tick_size_bids if self.tick_size_bids is not None else self.tick_size

    @property
    def asks_tick(self) -> Decimal:
        """Effective tick size on the ask side (per-side override or shared)."""
        return self.tick_size_asks if self.tick_size_asks is not None else self.tick_size


@dataclass(frozen=True)
class FillLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class FillResult:
    classification: Literal["full", "partial", "unfilled"]
    filled_qty: Decimal
    residual_qty: Decimal
    levels: tuple[FillLevel, ...]
    vwap: Decimal | None
    levels_consumed: int


def _reject_float(*values: object) -> None:
    for v in values:
        if isinstance(v, float):
            raise TypeError("float not accepted on boundary; pass Decimal or str")


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


def freeze_last_book(snapshots: list[Book], t_query_ns: int) -> tuple[Book, int]:
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
        raise LookupError(f"no snapshot with ts_ns <= {t_query_ns}")
    snap = snapshots[idx]
    staleness_ms = (t_query_ns - snap.ts_ns) // 1_000_000
    return snap, staleness_ms


def _validate_book_prices(book: Book) -> None:
    bids_tick = book.bids_tick
    asks_tick = book.asks_tick
    _tick_key(bids_tick)
    _tick_key(asks_tick)
    bid_lo = bids_tick
    bid_hi = Decimal(1) - bids_tick
    ask_lo = asks_tick
    ask_hi = Decimal(1) - asks_tick
    for lvl in book.side_bids:
        if lvl.price < bid_lo or lvl.price > bid_hi:
            raise ValueError(
                f"bid price {lvl.price} outside [{bid_lo}, {bid_hi}] "
                f"(bids_tick={bids_tick}; see py-clob-client #218)"
            )
    for lvl in book.side_asks:
        if lvl.price < ask_lo or lvl.price > ask_hi:
            raise ValueError(
                f"ask price {lvl.price} outside [{ask_lo}, {ask_hi}] "
                f"(asks_tick={asks_tick}; see py-clob-client #218)"
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
    # worst_price_limit is validated against the side that will actually be
    # consumed (BUY consumes asks; SELL consumes bids). Prevents placing a
    # limit outside the py-clob-client #218 validator bounds on that side.
    if side == "BUY":
        tick = book.asks_tick
        side_levels = book.side_asks
        price_ok = lambda p: p <= worst_price_limit  # noqa: E731
    elif side == "SELL":
        tick = book.bids_tick
        side_levels = book.side_bids
        price_ok = lambda p: p >= worst_price_limit  # noqa: E731
    else:
        raise ValueError(f"invalid side {side!r}")
    lo = tick
    hi = Decimal(1) - tick
    if worst_price_limit < lo or worst_price_limit > hi:
        raise ValueError(
            f"worst_price_limit {worst_price_limit} outside [{lo}, {hi}] "
            f"for side={side} (tick={tick}; see py-clob-client #218)"
        )

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


def apply_deltas(
    base: Book,
    deltas: Iterable[dict],
    *,
    new_ts_ns: int | None = None,
    new_source_seq: int | None = None,
) -> Book:
    """Rebuild a Book by applying price-change deltas to a base snapshot.

    Each delta is a dict matching the shape emitted by scrape_book.py's
    price_change records:

        {"side": "BUY" | "SELL", "price": "0.51", "size": "100"}

    A ``size`` of ``"0"`` removes the level. Deltas are applied in
    iteration order. The returned Book shares ``token_id`` and tick-size
    state with ``base``; ``ts_ns`` and ``source_seq`` can be overridden
    via kwargs (otherwise they are preserved from ``base``).

    This is a pure function. It does not mutate ``base``. Caller must
    ensure all deltas belong to the same token as ``base`` — no cross-
    token checking is done here.
    """
    bids: dict[Decimal, Decimal] = {lvl.price: lvl.size for lvl in base.side_bids}
    asks: dict[Decimal, Decimal] = {lvl.price: lvl.size for lvl in base.side_asks}

    for d in deltas:
        side = str(d.get("side", "")).upper()
        price_raw = d.get("price")
        size_raw = d.get("size", "0")
        if price_raw is None:
            continue
        _reject_float(price_raw, size_raw)
        price = price_raw if isinstance(price_raw, Decimal) else Decimal(str(price_raw))
        size = size_raw if isinstance(size_raw, Decimal) else Decimal(str(size_raw))
        book_side = bids if side == "BUY" else asks if side == "SELL" else None
        if book_side is None:
            continue
        if size == Decimal(0):
            book_side.pop(price, None)
        else:
            book_side[price] = size

    new_bids = tuple(Level(p, bids[p]) for p in sorted(bids.keys(), reverse=True))
    new_asks = tuple(Level(p, asks[p]) for p in sorted(asks.keys()))
    return dataclasses.replace(
        base,
        side_bids=new_bids,
        side_asks=new_asks,
        ts_ns=new_ts_ns if new_ts_ns is not None else base.ts_ns,
        source_seq=new_source_seq if new_source_seq is not None else base.source_seq,
    )
