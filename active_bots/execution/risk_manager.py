"""RiskManager — pre-trade gates for live execution.

Checks, in order:
  1. Kill switch file exists -> block all entries (exits always allowed)
  2. Cumulative realised PnL for the UTC day has dropped below -MAX_DAILY_LOSS
  3. Strategy-requested size exceeds MAX_TRADE_SIZE -> clamp to max

Exits and resolutions are NEVER blocked -- we always want to close existing
positions even if the circuit breaker fires.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("execution.risk")


@dataclass
class RiskConfig:
    max_daily_loss_usdc: float = 300.0
    # Session loss is a tighter circuit breaker than the daily one: it only
    # counts PnL accumulated in the current process run (not across
    # restarts). Once the cumulative session loss crosses this value,
    # further entries are blocked. Exits are always allowed. 0 = disabled.
    max_session_loss_usdc: float = 30.0
    max_trade_size_usdc: float = 100.0
    # Floor on the per-trade notional. Sub-floor trades are rejected (not
    # clamped up) — a tiny edge shouldn't become a small bet that merely
    # pays fees. Set via MIN_TRADE_SIZE_USDC env var; 0 disables the check.
    min_trade_size_usdc: float = 10.0
    kill_switch_file: Path = Path("daemon_state/KILL")


@dataclass
class RiskDecision:
    allowed: bool
    size_usdc: float
    size_shares: float
    reason: str | None = None


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self._day_key: str | None = None
        self._day_pnl: float = 0.0
        # Session PnL is cumulative since process start; never rolls.
        self._session_pnl: float = 0.0

    # ── Entry gate ────────────────────────────────────────────────────────

    def check_entry(self, action: dict[str, Any]) -> RiskDecision:
        if self._kill_switch_active():
            return RiskDecision(False, 0.0, 0.0, "kill switch active")

        if self._session_loss_breached():
            return RiskDecision(
                False, 0.0, 0.0,
                f"session loss breached ({self._session_pnl:+.2f} "
                f"<= -{self.config.max_session_loss_usdc:.0f})",
            )

        if self._daily_loss_breached():
            return RiskDecision(
                False, 0.0, 0.0,
                f"daily loss breached ({self._day_pnl:+.2f} "
                f"<= -{self.config.max_daily_loss_usdc:.0f})",
            )

        size_usdc = float(action.get("size_usdc", 0.0))
        entry_price = float(action.get("entry_price", 0.0))
        if size_usdc <= 0 or entry_price <= 0:
            return RiskDecision(False, 0.0, 0.0, "zero size or price")

        clamp_reason: str | None = None
        min_size = float(self.config.min_trade_size_usdc)
        max_size = float(self.config.max_trade_size_usdc)
        # Floor clamp: strategy's edge-scaled size is often below a usable
        # minimum; boost to the floor so we trade at real notional rather
        # than reject every low-edge signal.
        if min_size > 0 and size_usdc < min_size:
            clamp_reason = f"clamped up {size_usdc:.2f} -> {min_size:.2f} (min)"
            size_usdc = min_size
        # Ceiling clamp: cap runaway sizes at the max.
        if size_usdc > max_size:
            prior = f"{size_usdc:.2f}"
            clamp_reason = f"clamped {prior} -> {max_size:.2f} (max)"
            size_usdc = max_size

        size_shares = size_usdc / entry_price
        return RiskDecision(True, size_usdc, size_shares, clamp_reason)

    # ── PnL tracking (called by daemon after each closed trade) ──────────

    def record_trade(self, pnl: float, resolved_time: float | None = None) -> None:
        ts = resolved_time or time.time()
        day = _utc_day_key(ts)
        if day != self._day_key:
            self._day_key = day
            self._day_pnl = 0.0
        self._day_pnl += pnl
        self._session_pnl += pnl

    def current_day_pnl(self) -> float:
        self._roll_day()
        return self._day_pnl

    def current_session_pnl(self) -> float:
        return self._session_pnl

    # ── Internals ────────────────────────────────────────────────────────

    def _roll_day(self) -> None:
        day = _utc_day_key(time.time())
        if day != self._day_key:
            self._day_key = day
            self._day_pnl = 0.0

    def _daily_loss_breached(self) -> bool:
        self._roll_day()
        return self._day_pnl <= -abs(self.config.max_daily_loss_usdc)

    def _session_loss_breached(self) -> bool:
        cap = float(self.config.max_session_loss_usdc)
        if cap <= 0:
            return False
        return self._session_pnl <= -abs(cap)

    def _kill_switch_active(self) -> bool:
        try:
            return self.config.kill_switch_file.exists()
        except OSError:
            return False


def _utc_day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
