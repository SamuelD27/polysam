"""Executor protocol + shared dataclasses.

The daemon never constructs position dicts directly. It calls:
    executor.enter(action, market_ctx, now) -> EntryResult | None
    executor.exit(position, action, market_ctx, now) -> ExitResult | None
    executor.reconcile(now)  # called once per tick; live mode polls fills

enter() returns None if the order was rejected / blocked by risk manager.
exit() returns None only on unrecoverable error (the daemon then logs and
leaves the position in place so a later tick can retry).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class MarketCtx:
    """Everything the executor needs to translate a strategy action into an order.

    Populated by the daemon at market rollover. token ids may be None in paper
    mode (never needed) or when Gamma lookup has not yet completed in live mode.
    """

    slug: str
    t_zero: int
    strike: float
    yes_token_id: str | None = None
    no_token_id: str | None = None
    tick_size: float = 0.01

    @property
    def ready_for_live(self) -> bool:
        return bool(self.yes_token_id and self.no_token_id)


@dataclass
class EntryResult:
    """Outcome of a successful entry order.

    Mirrors the fields the legacy daemon wrote into state.enh_position /
    state.base_position so the state.json schema is unchanged.
    """

    slug: str
    side: str                # "Up" | "Down"
    entry_price: float       # actual avg fill price, not strategy estimate
    size_usdc: float         # actual notional filled
    size_shares: float       # actual shares filled
    strike: float
    entry_time: float
    edge: float = 0.0
    source: str = "edge"     # "edge" | "squeeze" | "base"
    time_zone: str | None = None
    spike_score: float | None = None
    fair_at_entry: float | None = None
    market_at_entry: float | None = None
    t_zero: int | None = None
    # Live-only: set by LiveExecutor so Reconciler can match fills back.
    order_id: str | None = None
    token_id: str | None = None
    # Acknowledgement timestamp (epoch seconds). Stamped by the caller in
    # daemon_base_v1.py immediately after executor.enter() returns, so the
    # moment reflects "response observed" rather than "pre-call decided".
    # Required by experiments/backtest/reconcile.py's §6.3 predicate; also
    # used by latency.fit_from_events_jsonl (ack_ts - entry_time) once live
    # fills land. None on paper-mode entries emitted pre-R2.1.
    ack_ts: float | None = None

    def to_position_dict(self) -> dict[str, Any]:
        """Dict shape matching legacy state.enh_position / state.base_position."""
        d: dict[str, Any] = {
            "slug": self.slug,
            "side": self.side,
            "entry_price": round(self.entry_price, 4),
            "edge": round(self.edge, 4),
            "size_usdc": round(self.size_usdc, 2),
            "size_shares": round(self.size_shares, 2),
            "strike": self.strike,
            "entry_time": self.entry_time,
            "source": self.source,
            "time_zone": self.time_zone,
            "spike_score": (
                round(self.spike_score, 2) if self.spike_score is not None else None
            ),
        }
        if self.fair_at_entry is not None:
            d["fair_at_entry"] = round(self.fair_at_entry, 4)
        if self.market_at_entry is not None:
            d["market_at_entry"] = round(self.market_at_entry, 4)
        if self.t_zero is not None:
            d["t_zero"] = self.t_zero
        if self.order_id is not None:
            d["order_id"] = self.order_id
        if self.token_id is not None:
            d["token_id"] = self.token_id
        if self.ack_ts is not None:
            d["ack_ts"] = self.ack_ts
        return d


@dataclass
class ExitResult:
    """Outcome of a successful exit or resolution.

    Mirrors the closed-trade dict shape written to state.{base,enh}_trades.
    """

    slug: str
    side: str
    entry_price: float
    exit_price: float
    pnl: float
    edge: float
    size_usdc: float
    size_shares: float
    won: bool
    resolved_time: float
    strike: float
    final_btc: float
    exit_type: str           # "TP" | "SL" | "RESOLUTION"
    source: str | None = None
    time_zone: str | None = None
    spike_score: float | None = None
    hold_time_s: float | None = None

    def to_trade_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "slug": self.slug,
            "side": self.side,
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "pnl": round(self.pnl, 2),
            "edge": round(self.edge, 4),
            "size_usdc": round(self.size_usdc, 2),
            "size_shares": round(self.size_shares, 2),
            "won": self.won,
            "resolved_time": self.resolved_time,
            "strike": self.strike,
            "final_btc": self.final_btc,
            "exit_type": self.exit_type,
        }
        if self.source is not None:
            d["source"] = self.source
        if self.time_zone is not None:
            d["time_zone"] = self.time_zone
        if self.spike_score is not None:
            d["spike_score"] = self.spike_score
        if self.hold_time_s is not None:
            d["hold_time_s"] = round(self.hold_time_s, 1)
        return d


@runtime_checkable
class Executor(Protocol):
    """Paper and live executors share this interface."""

    mode: str  # "paper" | "live"

    def enter(
        self,
        action: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        source: str = "edge",
    ) -> EntryResult | None: ...

    def exit(
        self,
        position: dict[str, Any],
        action: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        btc_price: float,
    ) -> ExitResult | None: ...

    def resolve(
        self,
        position: dict[str, Any],
        market_ctx: MarketCtx,
        now: float,
        *,
        btc_price: float,
    ) -> ExitResult | None: ...

    def reconcile(self, now: float) -> None: ...
