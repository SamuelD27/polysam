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

import logging
import math
import os
import time as _time
from decimal import Decimal
from typing import Any

from .enhanced_strategy import MARKET_DURATION, ProfitGrabber
from .execution.fees import CATEGORIES, fee_usdc
from .execution.live_book_state import (
    MarketBooks,
    WalkResult,
    walk_for_vwap,
)
from .refined_strategy import RefinedStrategy

# Defaults — env overridable per project convention.
MARKET_PRICE_MAX_STALENESS_S = float(os.environ.get("WALKED_VWAP_MARKET_STALENESS_S", 30.0))
MIN_TOP_OF_BOOK_SHARES_RATIO = float(os.environ.get("WALKED_VWAP_MIN_TOP_RATIO", 1.0))
WALKED_EDGE_MIN = float(os.environ.get("WALKED_VWAP_EDGE_MIN", 0.02))
WALKED_VWAP_PARTIAL_OK = os.environ.get("WALKED_VWAP_PARTIAL_OK", "0").strip() in (
    "1",
    "true",
    "True",
)
FEE_CATEGORY = os.environ.get("WALKED_VWAP_FEE_CATEGORY", "crypto")

# Exit-gate knobs — default-off; mid-quoted exit path is preserved bit-for-bit
# until WALKED_VWAP_EXIT_ENABLE is flipped per-deployment after paper-validation.
EXIT_ENABLE = os.environ.get("WALKED_VWAP_EXIT_ENABLE", "0").strip() in ("1", "true", "True")
EXIT_STALENESS_S = float(os.environ.get("WALKED_VWAP_EXIT_STALENESS_S", 30.0))
EXIT_PARTIAL_OK = os.environ.get("WALKED_VWAP_EXIT_PARTIAL_OK", "0").strip() in (
    "1",
    "true",
    "True",
)
EXIT_FALLBACK_MID = os.environ.get("WALKED_VWAP_EXIT_FALLBACK_MID", "1").strip() in (
    "1",
    "true",
    "True",
)


class WalkedExitProfitGrabber(ProfitGrabber):
    """ProfitGrabber that re-prices realizable exit on walked-bid VWAP (post fee).

    Reads books from self._latest_books, stashed by WalkedVWAPStrategy.on_tick
    every tick before parent delegation (whether or not books is None — the
    None branch is handled here so the seam stays tick-uniform). Falls back to
    mid via the parent's own check_exit when the book is stale, empty,
    partial-without-flag, or NaN. Inside the force-exit window, partial fills
    are accepted and a genuinely empty book falls back to mid so the parent's
    force-close still fires (never orphan a residual position pre-resolution).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Set fresh by wrapper.on_tick each tick (even when books is None);
        # subclass's own None branch handles fallback so the wrapper does not
        # need to gate the stash.
        self._latest_books: MarketBooks | None = None

    def check_exit(
        self,
        position: dict[str, Any],
        btc_price: float,
        sigma: float,
        t_zero: float,
        market_price_up: float | None = None,
        market_price_ts: float = 0.0,
    ) -> dict[str, Any] | None:
        """Re-price exit on walked-bid VWAP (post-fee), then defer to parent.

        See STRATEGY.md §4 for the full mechanics. With ``EXIT_ENABLE=False``
        this is a transparent passthrough to ``ProfitGrabber.check_exit``.
        """
        # Default-off: short-circuit so behaviour is identical to vanilla
        # ProfitGrabber.check_exit when EXIT_ENABLE is False (preserves mid
        # quoting bit-for-bit, no logging side effects).
        if not EXIT_ENABLE:
            return super().check_exit(
                position,
                btc_price,
                sigma,
                t_zero,
                market_price_up=market_price_up,
                market_price_ts=market_price_ts,
            )

        # Force-window detection mirrors enhanced_strategy.py:282-284 so the
        # gate's bad-book fallback decision matches the parent's own
        # force-close semantics (never hold past resolution).
        time_remaining = MARKET_DURATION - (_time.time() - t_zero)
        in_force_window = (
            self.force_exit_before_s > 0 and time_remaining <= self.force_exit_before_s
        )

        translated = self._compute_translated_market_price_up(
            position,
            market_price_up,
            in_force_window,
        )
        if translated is None:
            # FALLBACK_MID=0 + non-force-window + bad book → hold position;
            # next tick re-runs the gate against a fresh book.
            return None

        return super().check_exit(
            position,
            btc_price,
            sigma,
            t_zero,
            market_price_up=translated,
            market_price_ts=market_price_ts,
        )

    def _compute_translated_market_price_up(
        self,
        position: dict[str, Any],
        mid_market_price_up: float | None,
        in_force_window: bool,
    ) -> float | None:
        """Returns market_price_up to feed parent.check_exit, or None to hold.

        Returns the original mid_market_price_up unchanged if the book is bad
        and FALLBACK_MID=1 (or in_force_window). Returns the walked-bid VWAP
        translated into the YES-frame on success. Returns None to instruct the
        caller to hold (FALLBACK_MID=0 + non-force-window + bad book).
        """
        side = position["side"]
        size_shares = float(position["size_shares"])
        books = self._latest_books

        # No subscription this tick → fallback per knob / force-window.
        if books is None:
            return mid_market_price_up if (EXIT_FALLBACK_MID or in_force_window) else None

        book = books.yes if side == "Up" else books.no
        if book is None or not book.has_baseline or not book.bids:
            self._log_liquidity_gap(reason="empty_book", side=side)
            return mid_market_price_up if (EXIT_FALLBACK_MID or in_force_window) else None

        # Staleness gate — book.ts_ms is milliseconds (per LiveBookState).
        now = _time.time()
        if book.ts_ms and (now - book.ts_ms / 1000.0) > EXIT_STALENESS_S:
            self._log_liquidity_gap(reason="stale_book", side=side)
            return mid_market_price_up if (EXIT_FALLBACK_MID or in_force_window) else None

        walk = walk_for_vwap(book.bids, size_shares)
        # Force-window flips PARTIAL_OK True before the disallowed-partial
        # branch is reached, so the partial-disallowed fallback only consults
        # EXIT_FALLBACK_MID — by then in_force_window has already widened the
        # accept condition (see plan gate matrix).
        partial_ok = EXIT_PARTIAL_OK or in_force_window
        if walk.classification == "unfilled" or walk.vwap is None or not math.isfinite(walk.vwap):
            self._log_liquidity_gap(reason="empty_book", side=side)
            return mid_market_price_up if (EXIT_FALLBACK_MID or in_force_window) else None
        if walk.classification == "partial" and not partial_ok:
            self._log_liquidity_gap(
                reason="partial_fill_disallowed",
                side=side,
                filled_shares=walk.filled_shares,
            )
            return mid_market_price_up if EXIT_FALLBACK_MID else None

        # Effective per-share proceeds for the holder = VWAP MINUS per-share fee
        # (mirror of the entry-side _effective_vwap_after_fees, which ADDS the
        # fee for the buyer; on the sell side the taker pays the fee out of
        # proceeds, so it lowers the effective receive price).
        eff = self._effective_exit_after_fees(walk.vwap, walk.filled_shares)
        if not math.isfinite(eff):
            return mid_market_price_up if (EXIT_FALLBACK_MID or in_force_window) else None

        # Translate into YES-frame: parent computes
        #   realizable = market_price_up if side=="Up" else 1.0 - market_price_up
        # so feeding eff (Up) / 1-eff (Down) makes parent.realizable == eff
        # for both sides — the holder's true post-fee per-share exit proceeds.
        return eff if side == "Up" else (1.0 - eff)

    def _effective_exit_after_fees(self, fill_vwap: float, filled_shares: float) -> float:
        """Per-share exit proceeds net of bell-curve taker fee (selling side).

        Symmetric to _effective_vwap_after_fees but SUBTRACTS the per-share
        fee — on a sell, the taker pays the fee out of proceeds, so effective
        receive price < quoted VWAP.
        """
        if filled_shares <= 0:
            return fill_vwap
        category = CATEGORIES.get(FEE_CATEGORY, CATEGORIES["crypto"])
        fee = fee_usdc(
            p=Decimal(str(fill_vwap)),
            shares=Decimal(str(filled_shares)),
            category=category,
        )
        return fill_vwap - float(fee) / filled_shares  # SUBTRACT on the sell side

    def _log_liquidity_gap(self, **diag: Any) -> None:
        # Light structured log — no event-bus dep on the daemon path.
        logging.getLogger(__name__).info("walked_vwap_exit_liquidity_gap %s", diag)


class WalkedVWAPStrategy(RefinedStrategy):
    """RefinedStrategy + walked-VWAP entry gate."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build a RefinedStrategy and swap in the walked-bid exit grabber.

        See STRATEGY.md §1.5 / §3 for full mechanics. Parent receives all
        positional and keyword args; the override only swaps
        ``self.profit_grabber`` for the exit-aware subclass when present.
        """
        super().__init__(*args, **kwargs)
        # Swap the parent's ProfitGrabber for the exit-aware subclass, carrying
        # the SAME tp/sl/force config the parent already built. Reading the
        # values off the parent's instance keeps this hands-off w.r.t. how
        # EnhancedStrategy resolves env defaults (do not re-derive them).
        # When enable_profit_grabber=False (EnhancedStrategy.__init__), the
        # parent leaves self.profit_grabber=None — preserve that branch
        # untouched so callers that disabled exits still see the same shape.
        if self.profit_grabber is not None:
            self.profit_grabber = WalkedExitProfitGrabber(
                tp_delta_min=self.profit_grabber.tp_delta_min,
                sl_delta_min=self.profit_grabber.sl_delta_min,
                sl_delta_max=self.profit_grabber.sl_delta_max,
                tp_absolute_favor=self.profit_grabber.tp_absolute_favor,
                force_exit_before_s=self.profit_grabber.force_exit_before_s,
            )

    def _run_parent_on_tick(
        self,
        btc_price: float,
        market_price_up: float,
        sigma: float,
        t_zero: float,
        market_price_ts: float,
    ) -> dict[str, Any] | None:
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
        """Run the parent's on_tick and gate any ENTER through walked-VWAP.

        See STRATEGY.md §3 for the per-tick flow. Returns the parent's action
        unchanged for non-ENTER, the gate-augmented action on entry pass, or
        a ``WALKED_VWAP_REJECT`` action when an entry is rejected.
        """
        # Stash books onto the exit-aware ProfitGrabber every tick, even when
        # books is None — the subclass's own None branch handles fallback, so
        # keeping the stash unconditional avoids a stale book leaking from a
        # prior tick into the next exit-gate evaluation.
        if isinstance(self.profit_grabber, WalkedExitProfitGrabber):
            self.profit_grabber._latest_books = books
        action = self._run_parent_on_tick(
            btc_price,
            market_price_up,
            sigma,
            t_zero,
            market_price_ts,
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
                action,
                "stale_market_price",
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
        if (
            not math.isfinite(top_size)
            or top_size < requested_shares * MIN_TOP_OF_BOOK_SHARES_RATIO
        ):
            return self._reject(
                action,
                "insufficient_top_of_book",
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
                action,
                "empty_book",
                walked_VWAP=walk.vwap,
            )
        if walk.classification == "partial" and not WALKED_VWAP_PARTIAL_OK:
            return self._reject(
                action,
                "partial_fill_disallowed",
                walked_VWAP=walk.vwap,
                filled_shares=walk.filled_shares,
            )
        eff_vwap = self._effective_vwap_after_fees(walk.vwap, walk.filled_shares)
        if not math.isfinite(eff_vwap):
            return self._reject(
                action,
                "empty_book",
                walked_VWAP=walk.vwap,
                effective_VWAP=eff_vwap,
            )
        fair = float(action.get("fair") or action.get("fair_price") or 0.0)
        # walked_edge for a BUY (long YES or long NO): fair - effective per-share
        walked_edge = fair - eff_vwap

        if walked_edge < WALKED_EDGE_MIN:
            return self._reject(
                action,
                "insufficient_walked_edge",
                walked_VWAP=walk.vwap,
                effective_VWAP=eff_vwap,
                walked_edge=walked_edge,
            )

        # 5. Pass — augment action with diagnostics
        opposite = book.bids
        spread = (book.asks[0][0] - opposite[0][0]) if (book.asks and opposite) else 0.0
        mid = (
            (book.asks[0][0] + opposite[0][0]) / 2 if (book.asks and opposite) else book.asks[0][0]
        )
        staleness_ms = int((now - book.ts_ms / 1000.0) * 1000) if book.ts_ms else None

        # Down-size if partial allowed
        if walk.classification == "partial" and WALKED_VWAP_PARTIAL_OK:
            action["size_shares"] = walk.filled_shares

        # Replace the parent's mid-quoted entry with the realistic per-share cost
        # (effective_VWAP = walked book + bell-curve fee). This makes downstream
        # TP/SL math (ProfitGrabber.check_exit reads position["entry_price"])
        # operate against what the trader actually paid, not the mid. The
        # original mid quote is preserved as entry_price_mid for diagnostics
        # and for the parallel pnl_mid computed at exit.
        size_shares = float(action["size_shares"])
        action["entry_price_mid"] = float(action["entry_price"])
        action["entry_price"] = eff_vwap
        action["size_usdc"] = size_shares * eff_vwap

        # Sync the parent's internal stash. EnhancedStrategy.on_tick stored
        # self._open_position BEFORE this gate ran (enhanced_strategy.py:634
        # for edge entries, :613 for squeeze) using the mid-quoted entry_price.
        # ProfitGrabber.check_exit reads self._open_position["entry_price"] on
        # every subsequent tick — both for the TP/SL thresholds and for the pnl
        # field returned in the exit action. Without this sync, the daemon-side
        # position dict (built from to_position_dict on EntryResult) carries
        # the walked entry while the parent's check_exit operates on the mid,
        # and trade.pnl ends up identical to trade.pnl_mid.
        if getattr(self, "_open_position", None) is not None:
            self._open_position["entry_price"] = eff_vwap
            self._open_position["size_shares"] = size_shares
            self._open_position["size_usdc"] = size_shares * eff_vwap

        action.update(
            {
                "walked_VWAP": walk.vwap,
                "effective_VWAP": eff_vwap,
                "walked_edge": walked_edge,
                "book_top_size_take": top_size,
                "book_top_size_make": book.top_size("bids"),
                "spread_at_entry": (spread / mid) if mid > 0 else 0.0,
                "book_staleness_at_entry_ms": staleness_ms,
                "gate_passed": True,
            }
        )
        return action

    def _reject(
        self,
        original_action: dict[str, Any],
        reason: str,
        **diag: Any,
    ) -> dict[str, Any]:
        # Clear the parent's phantom position. EnhancedStrategy.on_tick stores
        # self._open_position BEFORE this gate runs (enhanced_strategy.py:634
        # for edge entries, :613 for squeeze) as part of returning the ENTER
        # action. On a reject, the daemon never enters — but the parent still
        # thinks it has a position, which (a) blocks future entries this market
        # (has_position guard at on_tick:609 / 630) and (b) emits phantom EXIT
        # actions on subsequent ticks that the daemon silently swallows. Worst
        # case: profit_grabber sets self._resolved=True (on_tick:597) and the
        # strategy is dead-stuck until the next market rollover.
        if getattr(self, "_open_position", None) is not None:
            self._open_position = None
            self._position_source = None
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
