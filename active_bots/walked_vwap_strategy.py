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

import math
import os
import time as _time
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

    def _run_parent_on_tick(
        self, btc_price, market_price_up, sigma, t_zero, market_price_ts,
    ):
        # Indirection so tests can monkeypatch this seam.
        return RefinedStrategy.on_tick(
            self,
            btc_price=btc_price,
            market_price_up=market_price_up,
            sigma=sigma,
            t_zero=t_zero,
            market_price_ts=market_price_ts,
        )

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
        action = self._run_parent_on_tick(
            btc_price, market_price_up, sigma, t_zero, market_price_ts,
        )
        if action is None:
            return None
        if action.get("action") != "ENTER":
            return action
        return self._gate(action, market_price_ts=market_price_ts, books=books)

    def _gate(
        self,
        action: dict[str, Any],
        *,
        market_price_ts: float,
        books: MarketBooks | None,
    ) -> dict[str, Any]:
        now = _time.time()

        # 1. Staleness
        if (
            market_price_ts is None
            or market_price_ts <= 0
            or (now - market_price_ts) > MARKET_PRICE_MAX_STALENESS_S
        ):
            return self._reject(
                action, "stale_market_price",
                staleness_s=(now - market_price_ts) if market_price_ts else None,
            )

        # 2. Book availability
        if books is None:
            return self._reject(action, "no_book_subscription")
        side = action["side"]
        book = books.yes if side == "Up" else books.no
        if book is None or not book.has_baseline:
            return self._reject(action, "no_book_subscription")
        if not book.asks:
            return self._reject(action, "empty_book")

        requested_shares = float(action["size_shares"])
        top_size = book.top_size("asks")

        # 3. Top-of-book liquidity (NaN-guarded — reviewer requirement)
        if not math.isfinite(top_size) or top_size < requested_shares * MIN_TOP_OF_BOOK_SHARES_RATIO:
            return self._reject(
                action, "insufficient_top_of_book",
                book_top_size_take=top_size if math.isfinite(top_size) else None,
                requested_shares=requested_shares,
            )

        # 4. Walked VWAP + fees (NaN-guarded — reviewer requirement: corrupt WS
        # data must not silently pass the gate. Treat non-finite VWAP as
        # equivalent to an empty book — same reject vocabulary, no new reasons.)
        walk = walk_for_vwap(book.asks, requested_shares)
        if walk.classification == "unfilled":
            return self._reject(action, "empty_book")
        if walk.vwap is None or not math.isfinite(walk.vwap):
            return self._reject(
                action, "empty_book",
                walked_VWAP=walk.vwap,
            )
        if walk.classification == "partial" and not WALKED_VWAP_PARTIAL_OK:
            return self._reject(
                action, "partial_fill_disallowed",
                walked_VWAP=walk.vwap, filled_shares=walk.filled_shares,
            )
        eff_vwap = self._effective_vwap_after_fees(walk.vwap, walk.filled_shares)
        if not math.isfinite(eff_vwap):
            return self._reject(
                action, "empty_book",
                walked_VWAP=walk.vwap, effective_VWAP=eff_vwap,
            )
        fair = float(action.get("fair") or action.get("fair_price") or 0.0)
        # walked_edge for a BUY (long YES or long NO): fair - effective per-share
        walked_edge = fair - eff_vwap

        if walked_edge < WALKED_EDGE_MIN:
            return self._reject(
                action, "insufficient_walked_edge",
                walked_VWAP=walk.vwap,
                effective_VWAP=eff_vwap,
                walked_edge=walked_edge,
            )

        # 5. Pass — augment action with diagnostics
        opposite = book.bids
        spread = (book.asks[0][0] - opposite[0][0]) if (book.asks and opposite) else 0.0
        mid = (book.asks[0][0] + opposite[0][0]) / 2 if (book.asks and opposite) else book.asks[0][0]
        staleness_ms = int((now - book.ts_ms / 1000.0) * 1000) if book.ts_ms else None

        # Down-size if partial allowed
        if walk.classification == "partial" and WALKED_VWAP_PARTIAL_OK:
            action["size_shares"] = walk.filled_shares

        # Replace the parent's mid-quoted entry with the realistic per-share cost
        # (effective_VWAP = walked book + bell-curve fee). This makes downstream
        # TP/SL math (EnhancedStrategy.evaluate uses position["entry_price"])
        # operate against what the trader actually paid, not the mid. The
        # original mid quote is preserved as entry_price_mid for diagnostics
        # and for the parallel pnl_mid computed at exit.
        size_shares = float(action["size_shares"])
        action["entry_price_mid"] = float(action["entry_price"])
        action["entry_price"] = eff_vwap
        action["size_usdc"] = size_shares * eff_vwap

        action.update({
            "walked_VWAP": walk.vwap,
            "effective_VWAP": eff_vwap,
            "walked_edge": walked_edge,
            "book_top_size_take": top_size,
            "book_top_size_make": book.top_size("bids"),
            "spread_at_entry": (spread / mid) if mid > 0 else 0.0,
            "book_staleness_at_entry_ms": staleness_ms,
            "gate_passed": True,
        })
        return action

    def _reject(
        self,
        original_action: dict[str, Any],
        reason: str,
        **diag: Any,
    ) -> dict[str, Any]:
        return {
            "action": "WALKED_VWAP_REJECT",
            "reject_reason": reason,
            "side": original_action.get("side"),
            "intended_size_usdc": original_action.get("size_usdc"),
            "intended_size_shares": original_action.get("size_shares"),
            "fair_at_decision": original_action.get("fair"),
            "market_at_decision": original_action.get("market"),
            **diag,
        }

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
