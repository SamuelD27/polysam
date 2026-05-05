"""Daemon-side per-token book state machine for the walked-VWAP gate.

Floats throughout — decision-time approximation, not canonical fill
arithmetic. For the canonical Decimal version used by the offline
backtester see ``experiments/backtest/harness.py:_RollingBookState``
and ``active_bots/execution/book.py:walk_book``.

# FUTURE-REFACTOR: extract a shared Protocol once a third caller
# (e.g. an in-daemon shadow reconciler) needs the same state machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class WalkResult:
    """Output of one ``walk_for_vwap`` call: ``"full"``, ``"partial"``, or ``"unfilled"``."""

    classification: Literal["full", "partial", "unfilled"]
    filled_shares: float
    residual_shares: float
    vwap: float | None
    levels_consumed: int


def walk_for_vwap(
    levels: list[tuple[float, float]],
    requested_shares: float,
) -> WalkResult:
    """Walk levels (price, size) in given order, accumulating up to requested_shares."""
    if requested_shares <= 0 or not levels:
        return WalkResult(
            classification="unfilled",
            filled_shares=0.0,
            residual_shares=requested_shares,
            vwap=None,
            levels_consumed=0,
        )
    remaining = requested_shares
    notional = 0.0
    filled = 0.0
    consumed = 0
    for price, size in levels:
        if remaining <= 0:
            break
        take = size if size < remaining else remaining
        if take <= 0:
            break
        notional += price * take
        filled += take
        remaining -= take
        consumed += 1
    if filled <= 0:
        return WalkResult("unfilled", 0.0, requested_shares, None, 0)
    if abs(filled - requested_shares) < 1e-9:
        return WalkResult("full", filled, 0.0, notional / filled, consumed)
    return WalkResult("partial", filled, requested_shares - filled, notional / filled, consumed)


@dataclass
class LiveBookState:
    """Per-token book. apply_snapshot replaces both sides; apply_delta mutates one level.

    apply_delta before any snapshot is dropped (recorded in dropped_deltas_no_baseline).
    """

    tick_size: float
    bids: list[tuple[float, float]] = field(default_factory=list)  # descending
    asks: list[tuple[float, float]] = field(default_factory=list)  # ascending
    ts_ms: int = 0
    has_baseline: bool = False
    dropped_deltas_no_baseline: int = 0

    def apply_snapshot(
        self,
        bids: list[dict],
        asks: list[dict],
        ts_ms: int,
        tick_size: float | None = None,
    ) -> None:
        """Replace both sides of the book from a CLOB snapshot frame.

        Filters out zero-size levels, sorts bids descending and asks
        ascending, and marks the book as baseline-ready (allowing subsequent
        deltas to mutate it).
        """
        if tick_size is not None:
            self.tick_size = tick_size
        self.bids = sorted(
            (
                (float(lvl["price"]), float(lvl["size"]))
                for lvl in (bids or [])
                if float(lvl["size"]) > 0
            ),
            key=lambda x: -x[0],
        )
        self.asks = sorted(
            (
                (float(lvl["price"]), float(lvl["size"]))
                for lvl in (asks or [])
                if float(lvl["size"]) > 0
            ),
            key=lambda x: x[0],
        )
        self.ts_ms = ts_ms
        self.has_baseline = True

    def apply_delta(self, delta: dict, ts_ms: int) -> None:
        """Apply one CLOB delta frame (single-level mutation).

        Drops the delta if no snapshot has been applied yet (counts in
        ``dropped_deltas_no_baseline``). Side is "BUY" → bids, "SELL" → asks.
        """
        if not self.has_baseline:
            self.dropped_deltas_no_baseline += 1
            return
        side = (delta.get("side") or "").upper()
        try:
            price = float(delta["price"])
            size = float(delta["size"])
        except (KeyError, TypeError, ValueError):
            return
        if side == "BUY":
            self.bids = _apply_one(self.bids, price, size, descending=True)
        elif side == "SELL":
            self.asks = _apply_one(self.asks, price, size, descending=False)
        self.ts_ms = ts_ms

    def top_size(self, which: Literal["bids", "asks"]) -> float:
        """Return the top-of-book size on the requested side, or 0.0 if empty."""
        levels = self.bids if which == "bids" else self.asks
        return levels[0][1] if levels else 0.0


def _apply_one(
    levels: list[tuple[float, float]],
    price: float,
    size: float,
    *,
    descending: bool,
) -> list[tuple[float, float]]:
    out = [lvl for lvl in levels if lvl[0] != price]
    if size > 0:
        out.append((price, size))
        out.sort(key=(lambda x: -x[0]) if descending else (lambda x: x[0]))
    return out


@dataclass
class MarketBooks:
    """Pair of YES + NO LiveBookState for a single market slug."""

    yes: LiveBookState | None = None
    no: LiveBookState | None = None

    def yes_staleness_ms(self, now_ms: int) -> int | None:
        """Age of the YES book in ms, or None if no baseline has been applied."""
        if self.yes is None or not self.yes.has_baseline:
            return None
        return now_ms - self.yes.ts_ms

    def no_staleness_ms(self, now_ms: int) -> int | None:
        """Age of the NO book in ms, or None if no baseline has been applied."""
        if self.no is None or not self.no.has_baseline:
            return None
        return now_ms - self.no.ts_ms
