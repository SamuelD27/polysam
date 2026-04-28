"""WalkedVWAPStrategy — RefinedStrategy with a book-aware entry gate.

Inherits all of Refined's TP/SL / squeeze-off / time-zone behaviour. The
override on on_tick adds a 5-step gate that runs AFTER Refined would have
returned an ENTER action but BEFORE the action propagates to the executor.
The gate rejects entries that would consume an empty / thin / fee-eaten
book — see docs/superpowers/plans/2026-04-27-walked-vwap-strategy.md.

Per-side convention: 'Up' buys YES (consumes books.yes.asks); 'Down' buys
NO (consumes books.no.asks). Both are LONG positions in different tokens.

Floats throughout. For canonical Decimal arithmetic see
experiments/backtest/replay_executor.py.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from .refined_strategy import RefinedStrategy
from .execution.fees import CATEGORIES, fee_usdc
from .execution.live_book_state import (
    LiveBookState,
    MarketBooks,
    WalkResult,
    walk_for_vwap,
)

# Defaults — env overridable per project convention.
MARKET_PRICE_MAX_STALENESS_S = float(
    os.environ.get("WALKED_VWAP_MARKET_STALENESS_S", 30.0)
)
MIN_TOP_OF_BOOK_SHARES_RATIO = float(
    os.environ.get("WALKED_VWAP_MIN_TOP_RATIO", 1.0)
)
WALKED_EDGE_MIN = float(os.environ.get("WALKED_VWAP_EDGE_MIN", 0.02))
WALKED_VWAP_PARTIAL_OK = (
    os.environ.get("WALKED_VWAP_PARTIAL_OK", "0").strip() in ("1", "true", "True")
)
FEE_CATEGORY = os.environ.get("WALKED_VWAP_FEE_CATEGORY", "crypto")


class WalkedVWAPStrategy(RefinedStrategy):
    """RefinedStrategy + walked-VWAP entry gate."""

    def on_tick(
        self,
        btc_price: float,
        market_price_up: float,
        sigma: float,
        t_zero: float,
        market_price_ts: float = 0.0,
        *,
        books: MarketBooks | None = None,
    ) -> dict[str, Any] | None:
        # Run the parent first.
        action = super().on_tick(
            btc_price=btc_price,
            market_price_up=market_price_up,
            sigma=sigma,
            t_zero=t_zero,
            market_price_ts=market_price_ts,
        )
        if action is None:
            return None
        # Only gate ENTER (and squeeze enter) actions; pass exits through unchanged.
        if action.get("action") not in ("ENTER",):
            return action
        # B4 implements the actual gate; for now, no-op pass-through.
        return action

    def _walked_vwap_for_entry(
        self,
        side: str,
        requested_shares: float,
        books: MarketBooks,
    ) -> WalkResult:
        """Walk the appropriate token's asks for an entry of side ('Up'|'Down')."""
        if side == "Up":
            book = books.yes
        elif side == "Down":
            book = books.no
        else:
            return WalkResult("unfilled", 0.0, requested_shares, None, 0)
        if book is None or not book.has_baseline:
            return WalkResult("unfilled", 0.0, requested_shares, None, 0)
        return walk_for_vwap(book.asks, requested_shares)

    def _effective_vwap_after_fees(
        self,
        fill_vwap: float,
        filled_shares: float,
    ) -> float:
        """Per-share entry cost including the Polymarket bell-curve taker fee.

        Effective price = fill_vwap + fee_usdc / filled_shares (signed for the
        BUY side: paying more per share). Symmetric around p=0.50; zero at the
        extremes per fees.py.
        """
        if filled_shares <= 0:
            return fill_vwap
        category = CATEGORIES.get(FEE_CATEGORY, CATEGORIES["crypto"])
        fee = fee_usdc(
            p=Decimal(str(fill_vwap)),
            shares=Decimal(str(filled_shares)),
            category=category,
        )
        return fill_vwap + float(fee) / filled_shares
