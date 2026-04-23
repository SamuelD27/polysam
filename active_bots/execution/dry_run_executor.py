"""Live-shaped dry-run executor (Option C per R2.1 design discussion).

Used by daemon_base_v1.py when POLYMARKET_MODE=live AND POLYMARKET_DRY_RUN=1.

Purpose: let the operator watch the bot behave end-to-end — realistic fills
at the real market mid (RTDS), ProfitGrabber TP/SL firing at actual
realizable prices, PnL accumulating the way it would live — without posting
any CLOB orders or risking USDC, while still emitting events.jsonl rows in
the shape ``experiments/backtest/reconcile.py``'s §6.3 predicate consumes
(``order_id`` + ``ack_ts`` + ``entry_price``).

Design:

- Fill math delegates to PaperExecutor, so entries land at
  ``action["entry_price"]`` (real RTDS mid) and exits at the strategy's
  quoted realizable exit price — identical to how the daemon has been
  paper-trading all along.
- order_id is a synthetic ``dry-run-<ms>`` token so reconcile.py's
  non-null predicate passes. Distinguishable by prefix from a real
  live order_id for audit.
- token_id is resolved from ``market_ctx.yes_token_id`` /
  ``.no_token_id``, exactly like ``LiveExecutor._token_for_side`` does.
- ack_ts is stamped in the caller (daemon_base_v1.py) right after
  ``enter()`` returns, so no change is needed inside this wrapper.

Deliberately does NOT exercise ``live_executor.py`` — that path is tested
only the first time the operator runs real LIVE_MODE without DRY_RUN.
Dryrun in this mode is a credential/tunnel/event-shape smoke test plus an
honest look at strategy behaviour under paper-quality fills.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .executor import EntryResult, ExitResult, MarketCtx
from .paper_executor import PaperExecutor

logger = logging.getLogger("execution.dry_run")


def _token_for_side(side: str, market_ctx: MarketCtx) -> str | None:
    s = side.strip().lower()
    if s == "up":
        return market_ctx.yes_token_id
    if s == "down":
        return market_ctx.no_token_id
    return None


class DryRunExecutor:
    """PaperExecutor fill behaviour with LiveExecutor-shaped event metadata."""

    mode = "live"  # downstream dashboards check .mode; "live" keeps labels honest

    def __init__(self, paper: PaperExecutor | None = None):
        self._paper = paper or PaperExecutor()

    def enter(
        self,
        action: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        source: str = "edge",
    ) -> EntryResult | None:
        result = self._paper.enter(action, market_ctx, now, source=source)
        if result is None:
            return None
        result.order_id = f"dry-run-{int(time.time() * 1000)}"
        result.token_id = _token_for_side(result.side, market_ctx)
        logger.info(
            "DRY_RUN entry %s %s @%.4f shares=%.2f $%.2f src=%s order_id=%s",
            result.side, market_ctx.slug, result.entry_price,
            result.size_shares, result.size_usdc, source, result.order_id,
        )
        return result

    def exit(
        self,
        position: dict[str, Any],
        action: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        btc_price: float,
    ) -> ExitResult | None:
        result = self._paper.exit(
            position, action, market_ctx, now, btc_price=btc_price
        )
        if result is not None:
            logger.info(
                "DRY_RUN exit %s %s @%.4f pnl=%+.2f type=%s",
                result.side, market_ctx.slug, result.exit_price,
                result.pnl, result.exit_type,
            )
        return result

    def resolve(
        self,
        position: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        btc_price: float,
    ) -> ExitResult | None:
        return self._paper.resolve(
            position, market_ctx, now, btc_price=btc_price
        )

    def reconcile(self, now: float) -> None:
        return self._paper.reconcile(now)


__all__ = ["DryRunExecutor"]
