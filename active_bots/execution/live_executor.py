"""LiveExecutor — send real CLOB orders via py-clob-client.

Entries:
  * strategy emits action {side=Up|Down, entry_price, size_usdc, size_shares}
  * we BUY the corresponding outcome token (YES for Up, NO for Down) with a
    market-style FAK order. amount=size_usdc (USD for BUY).
  * response gives us actual filled shares + avg price which we record as the
    position's entry_price/size (NOT the strategy's estimate).

Exits:
  * TP / SL actions both map to SELLing the same outcome token.
  * amount=size_shares (share count for SELL). FAK again.
  * PnL = (avg_fill_price - entry_price) * filled_shares  (minus fee/spread
    which is already baked into the CLOB fill price).

Resolutions:
  * At T+300 Polymarket settles on chain automatically. The live executor
    returns None for RESOLVE and lets Reconciler pick up the settlement
    trade once it posts.

Per-fill observability (``EntryResult.fill_details`` /
``ExitResult.fill_details``):

  The canonical schema is ``executor.FILL_DETAILS_KEYS``. This executor
  populates the fill-outcome subset (requested/filled/residual shares,
  classification, fill_vwap) from the CLOB ``post_order`` response.

  Book-context fields — ``best_bid_at_{decision,ack}``,
  ``best_ask_at_{decision,ack}``, ``top_of_book_size_{bid,ask}_at_ack``,
  ``book_staleness_ms_at_{decision,ack}`` — are deliberately left ``None``
  in this phase. The daemon (daemon_base_v1.py) subscribes only to RTDS
  ``orders_matched`` events; there is no in-process CLOB book feed from
  which to read top-of-book at either decision time or ack time. Adding
  such a subscription is separate architectural work — see the
  standalone ``scripts/scrape_book.py`` process and
  ``experiments/backtest/reconcile.py``. The current plan is for
  reconcile.py to join the scraper's book snapshots to each fill
  post-hoc using ``ack_ts`` as the key; executor-side logging records
  the per-fill anchors (``ack_ts``, ``order_id``, fill outcome) that
  that join depends on.

  ``fill_levels`` and ``levels_consumed`` are also ``None``: the CLOB
  ``post_order`` REST response does not include a per-level breakdown
  of what maker orders the taker consumed — only aggregated
  ``makingAmount``/``takingAmount``. Inferring per-level fills requires
  matching against a local book we don't have; it belongs in the same
  future book-subscription work or in reconcile.py's scraper-join.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from .executor import EntryResult, ExitResult, MarketCtx, empty_fill_details
from .reconciler import Reconciler
from .risk_manager import RiskManager
from .token_resolver import TokenResolver

logger = logging.getLogger("execution.live")

# py-clob-client order types — we don't import the enum at module load
# because py-clob-client may not be installed in paper-only environments.
# These are string constants accepted by post_order().
ORDER_TYPE_FAK = "FAK"
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"


class LiveExecutor:
    mode = "live"

    def __init__(
        self,
        client: Any,
        token_resolver: TokenResolver,
        risk: RiskManager,
        *,
        reconciler: Reconciler | None = None,
        dry_run: bool = False,
        exit_retry: int = 3,
        exit_retry_sleep_s: float = 1.5,
    ):
        self._client = client
        self._resolver = token_resolver
        self._risk = risk
        self._reconciler = reconciler
        self._dry_run = dry_run
        self._exit_retry = exit_retry
        self._exit_retry_sleep_s = exit_retry_sleep_s

    # ── Entry ─────────────────────────────────────────────────────────────

    def enter(
        self,
        action: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        source: str = "edge",
    ) -> EntryResult | None:
        decision = self._risk.check_entry(action)
        if not decision.allowed:
            logger.warning(
                "entry blocked (%s) side=%s slug=%s",
                decision.reason, action.get("side"), market_ctx.slug,
            )
            return None
        if decision.reason:
            logger.info("entry %s", decision.reason)

        token_id = _token_for_side(action["side"], market_ctx)
        if token_id is None:
            logger.warning(
                "entry blocked: missing token id for %s (slug=%s)",
                action["side"], market_ctx.slug,
            )
            return None

        size_usdc = decision.size_usdc
        resp = self._place_market_order(
            token_id=token_id,
            side=SIDE_BUY,
            amount=size_usdc,
        )
        if resp is None:
            logger.warning("entry rejected for %s %s", action["side"], market_ctx.slug)
            return None

        filled_shares, avg_price = _extract_fills(resp, side=SIDE_BUY)
        if filled_shares <= 0 or avg_price <= 0:
            logger.warning(
                "entry unfilled (FAK): slug=%s resp=%s",
                market_ctx.slug, _redact(resp),
            )
            return None

        actual_usdc = filled_shares * avg_price
        logger.info(
            "ENTRY FILLED %s %s @%.4f shares=%.2f $%.2f src=%s order_id=%s",
            action["side"], market_ctx.slug, avg_price,
            filled_shares, actual_usdc, source, _get_order_id(resp),
        )

        requested_shares = float(action.get("size_shares") or 0.0)
        if requested_shares <= 0:
            # Strategy fed size_usdc but not size_shares; infer from the
            # quoted entry price so fill_details still carries a number.
            quoted = float(action.get("entry_price") or 0.0)
            requested_shares = size_usdc / quoted if quoted > 0 else filled_shares
        fill_details = _build_fill_details(
            requested_shares=requested_shares,
            filled_shares=filled_shares,
            avg_price=avg_price,
        )

        spike = action.get("spike_score")
        return EntryResult(
            slug=market_ctx.slug,
            side=action["side"],
            entry_price=avg_price,
            size_usdc=actual_usdc,
            size_shares=filled_shares,
            strike=market_ctx.strike,
            entry_time=now,
            edge=float(action.get("edge", 0.0)),
            source=source,
            time_zone=action.get("time_zone"),
            spike_score=float(spike) if spike is not None else None,
            fair_at_entry=action.get("fair"),
            market_at_entry=action.get("market"),
            t_zero=market_ctx.t_zero,
            order_id=_get_order_id(resp),
            token_id=token_id,
            fill_details=fill_details,
        )

    # ── Exit (TP / SL) ───────────────────────────────────────────────────

    def exit(
        self,
        position: dict[str, Any],
        action: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        btc_price: float,
    ) -> ExitResult | None:
        token_id = position.get("token_id") or _token_for_side(
            position["side"], market_ctx,
        )
        if token_id is None:
            logger.error(
                "cannot exit %s %s: no token id",
                position["side"], position["slug"],
            )
            return None

        size_shares = float(position["size_shares"])
        if size_shares <= 0:
            return None

        resp = None
        for attempt in range(1, self._exit_retry + 1):
            resp = self._place_market_order(
                token_id=token_id,
                side=SIDE_SELL,
                amount=size_shares,
            )
            if resp is not None:
                filled, _ = _extract_fills(resp, side=SIDE_SELL)
                if filled > 0:
                    break
            logger.warning(
                "exit attempt %d/%d unfilled for %s %s",
                attempt, self._exit_retry, position["side"], position["slug"],
            )
            if attempt < self._exit_retry:
                time.sleep(self._exit_retry_sleep_s)

        if resp is None:
            return None
        filled_shares, avg_price = _extract_fills(resp, side=SIDE_SELL)
        if filled_shares <= 0:
            # No fills after retries. Leave position open; reconciler will
            # pick up auto-settlement at T+300.
            logger.error(
                "EXIT FAILED %s %s — no fills after %d attempts",
                position["side"], position["slug"], self._exit_retry,
            )
            return None

        exit_type = "TP" if action["action"] == "EXIT_TP" else "SL"
        entry_price = float(position["entry_price"])
        pnl = (avg_price - entry_price) * filled_shares
        hold = now - float(position.get("entry_time", now))

        logger.info(
            "EXIT %s %s %s @%.4f shares=%.2f pnl=%+.2f hold=%.0fs",
            exit_type, position["side"], position["slug"],
            avg_price, filled_shares, pnl, hold,
        )

        fill_details = _build_fill_details(
            requested_shares=size_shares,
            filled_shares=filled_shares,
            avg_price=avg_price,
        )

        return ExitResult(
            slug=position["slug"],
            side=position["side"],
            entry_price=entry_price,
            exit_price=avg_price,
            pnl=pnl,
            edge=position.get("edge", 0.0),
            size_usdc=position["size_usdc"],
            size_shares=filled_shares,
            won=(pnl > 0),
            resolved_time=now,
            strike=position["strike"],
            final_btc=btc_price,
            exit_type=exit_type,
            source=position.get("source"),
            time_zone=position.get("time_zone"),
            spike_score=position.get("spike_score"),
            hold_time_s=hold,
            fill_details=fill_details,
        )

    # ── Resolve ──────────────────────────────────────────────────────────

    def resolve(
        self,
        position: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        btc_price: float,
    ) -> ExitResult | None:
        """Record the position as resolved on our books without placing a trade.

        On-chain settlement happens automatically shortly after T+300. We log
        the expected outcome now so the daemon state reflects a closed position
        immediately, and let Reconciler correct the numbers if they diverge
        from the actual settlement (rare — settlement is deterministic).
        """
        strike = float(position["strike"])
        side = position["side"]
        up_wins = btc_price > strike
        won = up_wins if side == "Up" else not up_wins

        entry_price = float(position["entry_price"])
        size_shares = float(position["size_shares"])
        exit_price = 1.0 if won else 0.0
        pnl = (exit_price - entry_price) * size_shares
        hold = now - float(position.get("entry_time", now))

        logger.info(
            "RESOLVE (on-chain settlement pending) %s %s won=%s expected_pnl=%+.2f",
            side, position["slug"], won, pnl,
        )

        return ExitResult(
            slug=position["slug"],
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            edge=position.get("edge", 0.0),
            size_usdc=position["size_usdc"],
            size_shares=size_shares,
            won=won,
            resolved_time=now,
            strike=strike,
            final_btc=btc_price,
            exit_type="RESOLUTION",
            source=position.get("source"),
            time_zone=position.get("time_zone"),
            spike_score=position.get("spike_score"),
            hold_time_s=hold,
        )

    # ── Reconcile ─────────────────────────────────────────────────────────

    def reconcile(self, now: float) -> None:
        if self._reconciler is None:
            return None
        self._reconciler.poll(now)
        return None

    # ── Internals ────────────────────────────────────────────────────────

    def _place_market_order(
        self, *, token_id: str, side: str, amount: float,
    ) -> dict | None:
        """Build + sign + post a FAK market order.

        amount is USD for BUY, shares for SELL (py-clob-client convention).
        """
        try:
            from py_clob_client.clob_types import (  # type: ignore
                MarketOrderArgs,
                OrderType,
            )
            from py_clob_client.order_builder.constants import (  # type: ignore
                BUY, SELL,
            )
        except ImportError:
            logger.error("py-clob-client not installed; cannot place live orders")
            return None

        side_const = BUY if side == SIDE_BUY else SELL
        args = MarketOrderArgs(
            token_id=token_id,
            amount=float(amount),
            side=side_const,
            order_type=OrderType.FAK,
        )

        if self._dry_run:
            logger.info(
                "DRY_RUN market_order token=%s… side=%s amount=%.4f type=FAK",
                token_id[:10], side, amount,
            )
            return _fake_fill_response(amount=amount, side=side)

        try:
            signed = self._client.create_market_order(args)
            resp = self._client.post_order(signed, orderType=OrderType.FAK)
            return resp if isinstance(resp, dict) else None
        except Exception as e:  # pylint: disable=broad-except
            # SELL dust: Polymarket takes a fee out of filled share count, so
            # the wallet balance after a BUY is slightly less than the reported
            # fill size. On "not enough balance" the error payload reports the
            # actual on-chain balance — retry once with that exact amount.
            retry_amount = _dust_retry_amount(side, amount, e)
            if retry_amount is not None:
                logger.warning(
                    "sell dust adjust token=%s… wanted %.6f, actual %.6f; retrying",
                    token_id[:10], amount, retry_amount,
                )
                args2 = MarketOrderArgs(
                    token_id=token_id,
                    amount=retry_amount,
                    side=side_const,
                    order_type=OrderType.FAK,
                )
                try:
                    signed2 = self._client.create_market_order(args2)
                    resp2 = self._client.post_order(signed2, orderType=OrderType.FAK)
                    return resp2 if isinstance(resp2, dict) else None
                except Exception as e2:  # pylint: disable=broad-except
                    logger.error(
                        "post_order retry failed token=%s… side=%s amount=%.6f: %s",
                        token_id[:10], side, retry_amount, e2,
                    )
                    return None
            logger.error(
                "post_order failed token=%s… side=%s amount=%.4f: %s",
                token_id[:10], side, amount, e,
            )
            return None


# ── Helpers ─────────────────────────────────────────────────────────────

_DUST_RE = re.compile(r"balance:\s*(\d+)")
_MICROS_PER_UNIT = 1_000_000.0


def _dust_retry_amount(side: str, amount: float, exc: Exception) -> float | None:
    """If the SELL failed with 'not enough balance', return the actual
    reported balance (in shares) so the caller can retry once.

    Polymarket error string example:
        "not enough balance / allowance: the balance is not enough ->
         balance: 3838704, order amount: 3960000"

    The numbers are in 1e-6 micro-shares (same convention as USDC).
    Only applies to SELL; BUY with insufficient USDC is a real problem
    the daemon shouldn't paper over.
    """
    if side != SIDE_SELL:
        return None
    msg = str(exc)
    if "not enough balance" not in msg:
        return None
    m = _DUST_RE.search(msg)
    if not m:
        return None
    try:
        actual = int(m.group(1)) / _MICROS_PER_UNIT
    except ValueError:
        return None
    if actual <= 0 or actual >= amount:
        return None
    return actual


def _token_for_side(side: str, ctx: MarketCtx) -> str | None:
    if side == "Up":
        return ctx.yes_token_id
    if side == "Down":
        return ctx.no_token_id
    return None


def _extract_fills(resp: dict, *, side: str) -> tuple[float, float]:
    """Return (filled_shares, avg_price) from a post_order response.

    Polymarket post_order response shape (observed):
      { "success": bool, "orderID": str,
        "makingAmount": str,  # what WE gave up
        "takingAmount": str,  # what WE received
        "status": str, ... }

    For a BUY  we gave USD and received shares → shares=takingAmount,
                                                  price=makingAmount/shares
    For a SELL we gave shares and received USD → shares=makingAmount,
                                                  price=takingAmount/shares
    """
    if not resp:
        return 0.0, 0.0

    # Dry-run / internal fake format carries explicit fields.
    if "filled_shares" in resp and "avg_price" in resp:
        try:
            return float(resp["filled_shares"]), float(resp["avg_price"])
        except (TypeError, ValueError):
            pass

    status = (resp.get("status") or "").lower()
    if status in ("unmatched", "cancelled", "rejected"):
        return 0.0, 0.0

    making = _to_float(resp.get("makingAmount"))
    taking = _to_float(resp.get("takingAmount"))
    if making <= 0 or taking <= 0:
        return 0.0, 0.0

    if side == SIDE_BUY:
        shares = taking
        price = making / shares
    else:  # SELL
        shares = making
        price = taking / shares
    return shares, price


def _get_order_id(resp: dict) -> str | None:
    if not resp:
        return None
    return resp.get("orderID") or resp.get("order_id") or resp.get("id")


def _redact(resp: dict | None) -> dict:
    """Compact a CLOB response for logging — keep the audit fields, drop
    anything bulky. The CLOB response carries no secrets, but full-dump
    log lines bloat the journal.
    """
    if not resp:
        return {}
    keep = (
        "success", "status", "orderID", "order_id", "errorMsg",
        "makingAmount", "takingAmount",
    )
    return {k: resp[k] for k in keep if k in resp}


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


_PARTIAL_TOLERANCE = 1e-6  # share-count epsilon for full vs partial


def _build_fill_details(
    *,
    requested_shares: float,
    filled_shares: float,
    avg_price: float,
) -> dict[str, Any]:
    """Populate the fill-outcome subset of FILL_DETAILS_KEYS.

    Book-context fields and per-level breakdown stay None — see this
    module's docstring for why. ``classification`` is:
      - "full"      if residual <= epsilon
      - "partial"   if 0 < filled < requested
      - "unfilled"  if filled <= 0  (caller normally returns None,
                    but we keep this branch null-safe)
    """
    details = empty_fill_details()
    requested = max(0.0, float(requested_shares or 0.0))
    filled = max(0.0, float(filled_shares or 0.0))
    residual = max(0.0, requested - filled)

    if filled <= 0:
        classification = "unfilled"
    elif residual <= _PARTIAL_TOLERANCE:
        classification = "full"
    else:
        classification = "partial"

    details["requested_size_shares"] = requested
    details["filled_size_shares"] = filled
    details["residual_size_shares"] = residual
    details["classification"] = classification
    details["fill_vwap"] = float(avg_price) if filled > 0 and avg_price > 0 else None
    return details


def _fake_fill_response(*, amount: float, side: str) -> dict:
    """Dry-run placeholder: pretend we filled at price=0.50 (mid-book)."""
    if side == SIDE_BUY:
        shares = amount / 0.5
    else:
        shares = amount
    return {
        "success": True,
        "orderID": f"dry-run-{int(time.time()*1000)}",
        "status": "matched",
        "filled_shares": shares,
        "avg_price": 0.5,
        "dry_run": True,
    }


__all__ = ["LiveExecutor"]
