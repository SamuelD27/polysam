"""PaperExecutor — in-memory simulation matching legacy daemon_base_v1 behaviour.

Does not send any network requests. Fill price equals the strategy's quoted
entry_price; fill size equals the strategy's quoted size. Resolution PnL is
computed against BTC spot at T+300 with a SPREAD_COST haircut.
"""

from __future__ import annotations

from typing import Any

from .executor import EntryResult, ExitResult, MarketCtx, empty_fill_details

SPREAD_COST = 0.01  # matches daemon_base_v1 constant


def _paper_fill_details(
    requested_shares: float, fill_price: float | None,
) -> dict[str, Any]:
    """Paper fills are always full at the quoted price; book-context
    fields stay None for parity with LiveExecutor (see executor.py
    FILL_DETAILS_KEYS docstring)."""
    details = empty_fill_details()
    shares = max(0.0, float(requested_shares or 0.0))
    details["requested_size_shares"] = shares
    details["filled_size_shares"] = shares
    details["residual_size_shares"] = 0.0
    details["classification"] = "full" if shares > 0 else "unfilled"
    if fill_price is not None and fill_price > 0 and shares > 0:
        details["fill_vwap"] = float(fill_price)
    return details


class PaperExecutor:
    mode = "paper"

    def enter(
        self,
        action: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        source: str = "edge",
    ) -> EntryResult | None:
        entry_price = float(action["entry_price"])
        if entry_price <= 0:
            return None
        size_usdc = float(action.get("size_usdc", 0.0))
        if size_usdc <= 0:
            return None
        size_shares = float(
            action.get("size_shares", size_usdc / entry_price) or 0.0
        )
        if size_shares <= 0:
            return None

        spike = action.get("spike_score")
        return EntryResult(
            slug=market_ctx.slug,
            side=action["side"],
            entry_price=entry_price,
            size_usdc=size_usdc,
            size_shares=size_shares,
            strike=market_ctx.strike,
            entry_time=now,
            edge=float(action.get("edge", 0.0)),
            source=source,
            time_zone=action.get("time_zone"),
            spike_score=float(spike) if spike is not None else None,
            fair_at_entry=action.get("fair"),
            market_at_entry=action.get("market"),
            t_zero=market_ctx.t_zero,
            fill_details=_paper_fill_details(size_shares, entry_price),
        )

    def exit(
        self,
        position: dict[str, Any],
        action: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        btc_price: float,
    ) -> ExitResult | None:
        """TP / SL exit driven by strategy action. Price + PnL come from the
        strategy itself (ProfitGrabber already computed them against spot)."""
        exit_type = "TP" if action["action"] == "EXIT_TP" else "SL"
        exit_price = float(action.get("exit_price", 0.0))
        pnl = float(action.get("pnl", 0.0))
        hold = float(action.get("hold_time_s", now - position.get("entry_time", now)))

        return ExitResult(
            slug=position["slug"],
            side=position["side"],
            entry_price=position["entry_price"],
            exit_price=exit_price,
            pnl=pnl,
            edge=position.get("edge", 0.0),
            size_usdc=position["size_usdc"],
            size_shares=position["size_shares"],
            won=(exit_type == "TP" and pnl > 0),
            resolved_time=now,
            strike=position["strike"],
            final_btc=btc_price,
            exit_type=exit_type,
            source=position.get("source"),
            time_zone=position.get("time_zone"),
            spike_score=position.get("spike_score"),
            hold_time_s=hold,
            fill_details=_paper_fill_details(
                float(position["size_shares"]), exit_price,
            ),
        )

    def resolve(
        self,
        position: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        btc_price: float,
    ) -> ExitResult | None:
        """Simulate resolution at T+300: Up wins iff BTC > strike."""
        strike = position["strike"]
        side = position["side"]
        up_wins = btc_price > strike
        won = up_wins if side == "Up" else not up_wins

        entry_price = position["entry_price"]
        size_shares = position["size_shares"]
        spread_total = SPREAD_COST * size_shares
        exit_price = 1.0 if won else 0.0
        pnl = (exit_price - entry_price) * size_shares - spread_total

        hold = now - position.get("entry_time", now)

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
            fill_details=_paper_fill_details(size_shares, exit_price),
        )

    def reconcile(self, now: float) -> None:
        return None
