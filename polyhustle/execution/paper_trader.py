"""PaperTrader — realistic walked-VWAP fill simulator over PaperExecutor.

Replaces the prior thin wrapper. On ENTER, the wrapper:

1. **Pass-through** if the action already carries ``effective_VWAP`` or
   ``gate_passed=True`` — the walked-VWAP strategy gate has already
   produced a realistic fill upstream and rewriting here would
   double-charge fees.
2. Otherwise: walk the relevant book side (asks for ``Up`` -> YES asks;
   asks for ``Down`` -> NO asks) for the requested shares, compute the
   post-fee bell-curve VWAP via ``active_bots/execution/fees.fee_usdc``,
   and rewrite ``action["entry_price"]`` (storing the original mid as
   ``action["entry_price_mid"]``).
3. Return ``paper_no_fill`` if the book is missing / empty on the
   relevant side, or top-of-book size is smaller than the requested
   shares (mirrors WalkedVWAPStrategy's ``insufficient_top_of_book``
   reject; partial fills are not simulated in v1).
4. Stamp ``paper_latency_ms`` (sampled from the LatencyModel) onto
   ``fill_details`` for downstream attribution.

EXIT_TP / EXIT_SL / RESOLVE are unchanged — those go straight to the
underlying ``PaperExecutor``.

Strategy-internal vs executor-recorded entry price.
---------------------------------------------------
For non-walked observers (``BaseStrategy`` / ``EnhancedStrategy`` /
``RefinedStrategy``), the strategy's own ``self._open_position
["entry_price"]`` continues to hold the *mid-quoted* entry price (because
the strategy never sees the wrapper's rewrite). The wrapper rewrites
only the *executor's* view, so:

- ``EntryResult.entry_price`` = post-fee walked VWAP (real fill).
- ``EntryResult.entry_price_mid`` = mid (preserved for ``pnl_mid``).
- The strategy's TP/SL fires at the same wall-clock as today (its own
  thresholds operate against the mid).
- The recorded ``pnl`` is honest: ``(exit_price − walked_VWAP) × shares``.
- ``pnl_mid`` is the counterfactual: ``(exit_price − mid) × shares``.

This is the intended decomposition for the R2.2 attribution validation
— ``pnl_mid - pnl`` reproduces the fill-realism tax.

WalkedVWAPStrategy already computes its own walked VWAP at gate time
and synchronises ``self._open_position`` itself; the wrapper detects
that pre-walk and is a pass-through.
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from active_bots.execution.fees import CATEGORIES, fee_usdc
from active_bots.execution.live_book_state import (
    LiveBookState,
    MarketBooks,
    walk_for_vwap,
)
from active_bots.execution.paper_executor import PaperExecutor
from polyhustle.execution.latency import LatencyModel, ZeroLatency
from polyhustle.execution.trader import (
    ACTION_ENTER,
    ACTION_EXIT_SL,
    ACTION_EXIT_TP,
    ACTION_REJECT,
    ACTION_RESOLVE,
    Decision,
    ExecutionResult,
    Trader,
)

if TYPE_CHECKING:
    from active_bots.execution.executor import MarketCtx

logger = logging.getLogger("polyhustle.execution.paper_trader")

# Default fee category — overridable via PAPER_TRADER_FEE_CATEGORY for
# future non-crypto markets. Mirrors WalkedVWAPStrategy's
# WALKED_VWAP_FEE_CATEGORY env so a single env can drive both.
DEFAULT_FEE_CATEGORY = os.environ.get(
    "PAPER_TRADER_FEE_CATEGORY",
    os.environ.get("WALKED_VWAP_FEE_CATEGORY", "crypto"),
)


class PaperTrader(Trader):
    """Paper-mode trader with realistic walked-VWAP fill simulation.

    Composes ``PaperExecutor`` rather than subclassing ``_ExecutorTrader``
    — the wrapper logic on ENTER is non-trivial enough that the dispatch
    is clearer inline.

    Args:
        latency_model: per-call latency sampler stamped on fill_details.
            Default ``ZeroLatency`` preserves capture-time fidelity for
            replay-against-capture validation.
        fee_category: ``"crypto"`` / ``"finance"`` / ``"geopolitics"``.
            Mirrors WalkedVWAPStrategy's fee category.
        executor: optional pre-built PaperExecutor (tests inject mocks).
    """

    mode = "paper"

    def __init__(
        self,
        *,
        latency_model: LatencyModel | None = None,
        fee_category: str = DEFAULT_FEE_CATEGORY,
        executor: PaperExecutor | None = None,
    ) -> None:
        self._executor = executor or PaperExecutor()
        self._latency_model = latency_model or ZeroLatency()
        self._fee_category = fee_category

    # Public API

    def execute(
        self,
        decision: Decision,
        ctx: MarketCtx,
        *,
        position: dict[str, Any] | None = None,
        now: float | None = None,
        btc_price: float | None = None,
        source: str = "edge",
        books: MarketBooks | None = None,
    ) -> ExecutionResult:
        """Dispatch one decision; rewrite ENTER fills via book-walk."""
        when = now if now is not None else time.time()

        if decision.action == ACTION_ENTER:
            return self._handle_entry(decision, ctx, when, source, books)

        if decision.action in (ACTION_EXIT_TP, ACTION_EXIT_SL):
            if position is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True, reject_reason="no_open_position",
                )
            if btc_price is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True, reject_reason="missing_btc_price",
                )
            exit_ = self._executor.exit(
                position, decision.to_action(), ctx, when, btc_price=btc_price,
            )
            if exit_ is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True, reject_reason="executor_rejected_exit",
                )
            return ExecutionResult(action=decision.action, exit_=exit_)

        if decision.action == ACTION_RESOLVE:
            if position is None or btc_price is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True, reject_reason="missing_position_or_btc_price",
                )
            res = self._executor.resolve(
                position, ctx, when, btc_price=btc_price,
            )
            if res is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True, reject_reason="executor_rejected_resolve",
                )
            return ExecutionResult(action=decision.action, exit_=res)

        if decision.action == ACTION_REJECT:
            return ExecutionResult(
                action=decision.action, rejected=True,
                reject_reason=decision.meta.get("reject_reason", "walked_vwap_reject"),
            )

        return ExecutionResult(
            action=decision.action, rejected=True,
            reject_reason="unknown_action",
        )

    def reconcile(self, now: float) -> None:
        """Per-tick housekeeping — paper has none."""
        self._executor.reconcile(now)

    # Internals

    def _handle_entry(
        self,
        decision: Decision,
        ctx: MarketCtx,
        when: float,
        source: str,
        books: MarketBooks | None,
    ) -> ExecutionResult:
        """Walk the book + rewrite, OR pass-through, OR no_fill."""
        side = (decision.side or "").strip()
        latency_ms = self._latency_model.sample_one_way_ms(side or "BUY")

        # 1. Pass-through detection: walked-VWAP gate already ran.
        if self._is_pre_walked(decision):
            entry_result = self._executor.enter(
                decision.to_action(), ctx, when, source=source,
            )
            if entry_result is None:
                return ExecutionResult(
                    action=decision.action, rejected=True,
                    reject_reason="executor_rejected_entry",
                )
            entry_result.fill_details["paper_latency_ms"] = latency_ms
            entry_result.ack_ts = time.time()
            return ExecutionResult(action=decision.action, entry=entry_result)

        # 2. Walk the book; reject on missing / empty / insufficient.
        walked = self._walk_for_entry(decision, books)
        if walked is None:
            return ExecutionResult(
                action=decision.action, rejected=True,
                reject_reason="paper_no_fill",
            )

        post_fee_vwap, mid_quote = walked

        # 3. Rewrite the action: entry_price -> walked VWAP, save mid.
        action = decision.to_action()
        action["entry_price_mid"] = mid_quote
        action["entry_price"] = post_fee_vwap
        size_shares = float(action.get("size_shares") or 0.0)
        action["size_usdc"] = size_shares * post_fee_vwap

        entry_result = self._executor.enter(action, ctx, when, source=source)
        if entry_result is None:
            return ExecutionResult(
                action=decision.action, rejected=True,
                reject_reason="executor_rejected_entry",
            )
        entry_result.fill_details["paper_latency_ms"] = latency_ms
        entry_result.ack_ts = time.time()
        return ExecutionResult(action=decision.action, entry=entry_result)

    @staticmethod
    def _is_pre_walked(decision: Decision) -> bool:
        """True if the walked-VWAP gate already produced this fill.

        Two markers any of which is sufficient:
        - ``effective_VWAP`` populated (the gate's primary output).
        - ``gate_passed=True`` (defensive — should always coincide).
        """
        meta = decision.meta or {}
        if meta.get("effective_VWAP") is not None:
            return True
        if meta.get("gate_passed") is True:
            return True
        return False

    def _walk_for_entry(
        self,
        decision: Decision,
        books: MarketBooks | None,
    ) -> tuple[float, float] | None:
        """Return (post_fee_vwap, mid_quote) or None on no_fill.

        ``mid_quote`` is the strategy's quoted entry_price — preserved as
        ``entry_price_mid`` for the dual-PnL attribution.
        """
        if not isinstance(books, MarketBooks):
            return None
        side = (decision.side or "").strip()
        size_shares = float(decision.size_shares or 0.0)
        if size_shares <= 0:
            return None

        if side == "Up":
            book = books.yes
        elif side == "Down":
            book = books.no
        else:
            return None

        if not isinstance(book, LiveBookState):
            return None
        if not book.has_baseline or not book.asks:
            return None

        # Top-of-book guard — mirrors WalkedVWAPStrategy's
        # MIN_TOP_OF_BOOK_SHARES_RATIO=1.0 default.
        top_size = book.top_size("asks")
        if top_size < size_shares:
            return None

        walk = walk_for_vwap(book.asks, size_shares)
        if walk.classification != "full" or walk.vwap is None:
            return None

        post_fee = self._post_fee_vwap(walk.vwap, walk.filled_shares)
        mid_quote = float(decision.entry_price or walk.vwap)
        return post_fee, mid_quote

    def _post_fee_vwap(self, fill_vwap: float, filled_shares: float) -> float:
        """Per-share entry cost including the bell-curve taker fee.

        Symmetric with ``WalkedVWAPStrategy._effective_vwap_after_fees``.
        """
        if filled_shares <= 0:
            return fill_vwap
        category = CATEGORIES.get(self._fee_category, CATEGORIES["crypto"])
        fee = fee_usdc(
            p=Decimal(str(fill_vwap)),
            shares=Decimal(str(filled_shares)),
            category=category,
        )
        return fill_vwap + float(fee) / filled_shares
