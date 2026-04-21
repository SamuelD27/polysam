"""Enhanced trading strategies for BTC 5m binary options.

Three composable enhancements over the base fair-price edge strategy:
1. Time-based entry scaling -- adaptive edge thresholds per time zone
2. Profit grabber -- active TP/SL instead of hold-to-resolution
3. Mean reversion squeeze detector -- exploits early spike reversals
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from .base_strategy import (
    EDGE_MAX,
    EDGE_MIN,
    MAX_RISK,
    MARKET_DURATION,
    SPREAD_COST,
    BaseStrategy,
    compute_edge,
    compute_fair_price,
    compute_position_size,
    resolve_trade,
)
from .pricing.constants import SECONDS_PER_YEAR

# ── Time zone definitions ──────────────────────────────────────────────────

TIME_ZONES: list[tuple[int, int, float, str]] = [
    (60, 120, 0.20, "early"),
    (120, 210, 0.12, "mid"),
    (210, 260, 0.08, "sweet_spot"),
    (260, 285, 0.15, "late_gamma"),
]

# Minimum entry price — below this, fills are unrealistically thin on the
# real CLOB (a $50 stake at 0.02 implies 2,500 shares which would move the
# market). Both edge and squeeze entries are gated by this floor.
MIN_ENTRY_PRICE = 0.05
MAX_ENTRY_PRICE = 0.95  # symmetric: avoid near-certain outcomes too


# ── Enhancement 1: Time-Based Entry ─────────────────────────────���──────────

class TimeBasedStrategy:
    """Edge entry strategy with time-zone-adaptive thresholds.

    Instead of a single fixed entry window and edge_min, iterates over
    TIME_ZONES so that the required edge varies with elapsed time.
    """

    def __init__(
        self,
        edge_max: float = EDGE_MAX,
        max_risk: float = MAX_RISK,
    ):
        self.edge_max = edge_max
        self.max_risk = max_risk

        self._t_zero = None
        self._strike = None
        self._open_position = None
        self._resolved = False

    def reset(self, t_zero: float | None = None, strike: float | None = None) -> None:
        """Reset state for a new market."""
        self._t_zero = t_zero
        self._strike = strike
        self._open_position = None
        self._resolved = False

    @property
    def has_position(self) -> bool:
        return self._open_position is not None

    @property
    def is_resolved(self) -> bool:
        return self._resolved

    def on_tick(
        self,
        btc_price: float,
        market_price_up: float,
        sigma: float,
        t_zero: float,
    ) -> dict[str, Any] | None:
        """Process a price tick. Returns an action dict or None.

        Returns:
            None or dict with keys:
            - {"action": "ENTER", "side", "entry_price", "edge",
               "size_usdc", "size_shares", "time_zone"}
            - {"action": "RESOLVE", ...}
        """
        now = time.time()

        # Detect new market
        if self._t_zero != t_zero:
            self.reset(t_zero=t_zero, strike=btc_price)

        elapsed = now - self._t_zero
        time_remaining = MARKET_DURATION - elapsed

        # ── Resolution check ──
        if self._open_position is not None and not self._resolved:
            if elapsed >= MARKET_DURATION:
                result = resolve_trade(self._open_position, btc_price)
                self._resolved = True
                result["action"] = "RESOLVE"
                return result

        # ── Entry check: iterate time zones ──
        if not self.has_position:
            for offset_min, offset_max, zone_edge_min, label in TIME_ZONES:
                if offset_min <= elapsed <= offset_max:
                    fair = compute_fair_price(btc_price, self._strike, sigma, time_remaining)
                    edge, side, entry_price = compute_edge(fair, market_price_up)

                    if edge >= zone_edge_min and MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:
                        size_usdc, size_shares = compute_position_size(
                            edge, entry_price,
                            edge_min=zone_edge_min,
                            edge_max=self.edge_max,
                            max_risk=self.max_risk,
                        )
                        self._open_position = {
                            "side": side,
                            "entry_price": entry_price,
                            "size_usdc": size_usdc,
                            "size_shares": size_shares,
                            "strike": self._strike,
                            "entry_time": now,
                            "entry_elapsed": elapsed,
                            "edge": edge,
                            "fair_price": fair,
                            "market_price_up": market_price_up,
                        }
                        return {
                            "action": "ENTER",
                            "side": side,
                            "entry_price": entry_price,
                            "edge": edge,
                            "size_usdc": size_usdc,
                            "size_shares": size_shares,
                            "time_zone": label,
                        }
                    break  # only try the first matching zone

        return None


# ── Enhancement 2: Profit Grabber ──────────────────────────────────────────

# Floor / cap for the confidence-adaptive TP threshold. Lower edge =
# less conviction, take profits quickly; higher edge = ride the move further.
TP_DELTA_MIN = 0.05
TP_DELTA_MAX = 0.30
# SL is symmetric: tight stop when conviction is low, wider when we have edge
# (we expect noise around our model price — don't get shaken out).
SL_DELTA_MIN = 0.05
SL_DELTA_MAX = 0.20


def adaptive_tp(edge: float) -> float:
    """Map entry edge to TP threshold. Edge 0.10 → 0.10, 0.20 → 0.20, capped."""
    return max(TP_DELTA_MIN, min(TP_DELTA_MAX, edge))


def adaptive_sl(edge: float) -> float:
    """SL grows with conviction so noise doesn't shake us out of high-edge trades."""
    return max(SL_DELTA_MIN, min(SL_DELTA_MAX, 0.5 * edge + 0.05))


class ProfitGrabber:
    """Monitors open positions and triggers TP/SL exits before resolution."""

    def __init__(self):
        pass

    def check_exit(
        self,
        position: dict[str, Any],
        btc_price: float,
        sigma: float,
        t_zero: float,
        market_price_up: float | None = None,
        market_price_ts: float = 0.0,
    ) -> dict[str, Any] | None:
        """Check whether to TP or SL an open position.

        Triggers on the *realizable* (market-based) exit price, not the
        model fair price — fair-based triggering exits every edge entry on
        the same tick (fair − entry == edge ≈ tp_delta by design).

        TP / SL thresholds scale with the entry edge: a high-conviction
        position holds longer for a bigger move; a low-conviction position
        cashes out at the first whiff of profit.
        """
        now = time.time()
        elapsed = now - t_zero
        time_remaining = MARKET_DURATION - elapsed

        if time_remaining <= 0:
            return None

        strike = position["strike"]
        entry_price = position["entry_price"]
        side = position["side"]
        entry_edge = float(position.get("edge", 0.10))

        fair_up = compute_fair_price(btc_price, strike, sigma, time_remaining)
        current_fair = fair_up if side == "Up" else 1.0 - fair_up

        # Without a fresh market quote we cannot honestly TP/SL — hold to
        # resolution rather than booking phantom fills against the model.
        market_fresh = (
            market_price_up is not None
            and market_price_ts > 0.0
            and (now - market_price_ts) < 10.0
        )
        if not market_fresh:
            return None

        realizable = market_price_up if side == "Up" else 1.0 - market_price_up

        delta_favor = realizable - entry_price
        delta_against = entry_price - realizable

        tp_delta = adaptive_tp(entry_edge)
        sl_delta = adaptive_sl(entry_edge)

        if delta_favor >= tp_delta:
            action = "EXIT_TP"
        elif delta_against >= sl_delta:
            action = "EXIT_SL"
        else:
            return None

        exit_price = max(0.0, min(1.0, realizable))
        size_shares = position["size_shares"]
        pnl = (exit_price - entry_price) * size_shares - SPREAD_COST * size_shares
        hold_time_s = now - position["entry_time"]

        return {
            "action": action,
            "exit_price": exit_price,
            "pnl": pnl,
            "hold_time_s": hold_time_s,
            "current_fair": current_fair,
            "side": side,
            "tp_delta": tp_delta,
            "sl_delta": sl_delta,
        }


# ── Enhancement 3: Squeeze Detector ───────────────────────────────────────

SPIKE_THRESHOLD = 2.0
SPIKE_WINDOW = 60
SQUEEZE_ENTRY_MIN = 60
SQUEEZE_ENTRY_MAX = 150
SQUEEZE_REVERSION_FRAC = 0.30


class SqueezeDetector:
    """Detects early BTC spikes and trades mean reversion.

    Detection phase (T+0 to T+SPIKE_WINDOW):
        Track max |ln(S/K)| and direction. If spike_score >= threshold,
        flag as squeeze candidate.

    Entry phase (T+SQUEEZE_ENTRY_MIN to T+SQUEEZE_ENTRY_MAX):
        Wait for partial reversion, then enter counter-trend.
    """

    def __init__(
        self,
        spike_threshold: float = SPIKE_THRESHOLD,
        spike_window: float = SPIKE_WINDOW,
        entry_min: float = SQUEEZE_ENTRY_MIN,
        entry_max: float = SQUEEZE_ENTRY_MAX,
        reversion_frac: float = SQUEEZE_REVERSION_FRAC,
        edge_max: float = EDGE_MAX,
        max_risk: float = MAX_RISK,
    ):
        self.spike_threshold = spike_threshold
        self.spike_window = spike_window
        self.entry_min = entry_min
        self.entry_max = entry_max
        self.reversion_frac = reversion_frac
        self.edge_max = edge_max
        self.max_risk = max_risk

        self._max_deviation = 0.0
        self._spike_direction = None
        self._spike_score = 0.0
        self._is_squeeze_candidate = False
        self._entered = False

    def reset(self) -> None:
        """Clear state for a new market."""
        self._max_deviation = 0.0
        self._spike_direction = None
        self._spike_score = 0.0
        self._is_squeeze_candidate = False
        self._entered = False

    @property
    def is_squeeze_candidate(self) -> bool:
        return self._is_squeeze_candidate

    @property
    def spike_score(self) -> float:
        return self._spike_score

    @property
    def spike_direction(self) -> str | None:
        return self._spike_direction

    def on_tick(
        self,
        btc_price: float,
        strike: float,
        sigma: float,
        elapsed_s: float,
    ) -> dict[str, Any] | None:
        """Process a tick. Returns squeeze entry action or None.

        Args:
            btc_price: current BTC spot
            strike: market strike price
            sigma: current vol estimate
            elapsed_s: seconds since market open

        Returns:
            None or {"action": "SQUEEZE_ENTER", "side", "entry_price",
                      "spike_score", "size_usdc", "size_shares"}
        """
        if self._entered:
            return None

        # Detection phase: track max deviation
        deviation = abs(math.log(btc_price / strike)) if strike > 0 else 0
        if elapsed_s <= self.spike_window:
            if deviation > self._max_deviation:
                self._max_deviation = deviation
                if btc_price > strike:
                    self._spike_direction = "up"
                else:
                    self._spike_direction = "down"

            # Compute spike score
            if sigma > 1e-12:
                expected_std = sigma * math.sqrt(elapsed_s / SECONDS_PER_YEAR)
                self._spike_score = self._max_deviation / expected_std if expected_std > 0 else 0
            else:
                self._spike_score = 0

            if self._spike_score >= self.spike_threshold:
                self._is_squeeze_candidate = True

        # Entry phase
        if not self._is_squeeze_candidate:
            return None

        if not (self.entry_min <= elapsed_s <= self.entry_max):
            return None

        # Check for reversion
        reversion_ratio = 1.0 - (deviation / self._max_deviation) if self._max_deviation > 0 else 0
        if reversion_ratio < self.reversion_frac:
            return None

        # Enter counter-trend
        if self._spike_direction == "up":
            side = "Down"
        else:
            side = "Up"

        self._entered = True

        time_remaining = MARKET_DURATION - elapsed_s
        fair_up = compute_fair_price(btc_price, strike, sigma, time_remaining)

        if side == "Up":
            entry_price = fair_up
        else:
            entry_price = 1.0 - fair_up

        # Skip thin / near-resolved books for the same reason as TimeBasedStrategy.
        if not (MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE):
            return None

        # Size based on spike score (clamped)
        edge_equiv = min((self._spike_score - self.spike_threshold) / (4.0 - self.spike_threshold), 1.0) * 0.25
        if edge_equiv <= 0.0:
            edge_equiv = EDGE_MIN

        size_usdc, size_shares = compute_position_size(
            edge_equiv, entry_price,
            edge_min=EDGE_MIN,
            edge_max=self.edge_max,
            max_risk=self.max_risk,
        )

        return {
            "action": "SQUEEZE_ENTER",
            "side": side,
            "entry_price": entry_price,
            "spike_score": self._spike_score,
            "size_usdc": size_usdc,
            "size_shares": size_shares,
            "spike_direction": self._spike_direction,
            "reversion_ratio": reversion_ratio,
        }


# ── Combined Strategy ───────────────────────────────���──────────────────────

class EnhancedStrategy:
    """Combined strategy with all 3 enhancements.

    Priority on each tick:
    1. Check profit grabber on existing position (TP/SL)
    2. Check resolution on existing position
    3. Check squeeze entry (independent signal)
    4. Check time-based edge entry
    """

    def __init__(
        self,
        enable_time_based: bool = True,
        enable_profit_grabber: bool = True,
        enable_squeeze: bool = True,
        max_risk: float = MAX_RISK,
    ):
        if enable_time_based:
            self.time_strategy = TimeBasedStrategy(max_risk=max_risk)
        else:
            self.time_strategy = BaseStrategy(max_risk=max_risk)

        if enable_profit_grabber:
            self.profit_grabber = ProfitGrabber()
        else:
            self.profit_grabber = None

        if enable_squeeze:
            self.squeeze = SqueezeDetector(max_risk=max_risk)
        else:
            self.squeeze = None

        self._t_zero = None
        self._strike = None
        self._open_position = None
        self._position_source = None
        self._resolved = False

    def reset(self, t_zero: float | None = None, strike: float | None = None) -> None:
        """Reset state for a new market."""
        self._t_zero = t_zero
        self._strike = strike
        self._open_position = None
        self._position_source = None
        self._resolved = False

        self.time_strategy.reset(t_zero=t_zero, strike=strike)
        if self.squeeze is not None:
            self.squeeze.reset()

    @property
    def has_position(self) -> bool:
        return self._open_position is not None

    @property
    def is_resolved(self) -> bool:
        return self._resolved

    def on_tick(
        self,
        btc_price: float,
        market_price_up: float,
        sigma: float,
        t_zero: float,
        market_price_ts: float = 0.0,
    ) -> dict[str, Any] | None:
        """Process tick. Returns action dict or None.

        Priority:
        1. Check profit grabber on existing position
        2. Check resolution on existing position
        3. Check squeeze entry (independent signal)
        4. Check time-based edge entry
        """
        now = time.time()

        # Detect new market
        if self._t_zero != t_zero:
            self.reset(t_zero=t_zero, strike=btc_price)

        elapsed = now - self._t_zero
        time_remaining = MARKET_DURATION - elapsed

        # 1. Profit grabber on existing position
        if self.profit_grabber is not None and self._open_position is not None and not self._resolved:
            exit_action = self.profit_grabber.check_exit(
                self._open_position, btc_price, sigma, self._t_zero,
                market_price_up=market_price_up,
                market_price_ts=market_price_ts,
            )
            if exit_action is not None:
                exit_action["position_source"] = self._position_source
                exit_action["strike"] = self._open_position["strike"]
                exit_action["size_usdc"] = self._open_position["size_usdc"]
                exit_action["size_shares"] = self._open_position["size_shares"]
                self._open_position = None
                self._resolved = True
                return exit_action

        # 2. Resolution check
        if self._open_position is not None and not self._resolved:
            if elapsed >= MARKET_DURATION:
                result = resolve_trade(self._open_position, btc_price)
                self._resolved = True
                result["action"] = "RESOLVE"
                return result

        # 3. Squeeze entry
        if self.squeeze is not None and not self.has_position:
            sq_action = self.squeeze.on_tick(btc_price, self._strike, sigma, elapsed)
            if sq_action is not None:
                self._position_source = "squeeze"
                self._open_position = {
                    "side": sq_action["side"],
                    "entry_price": sq_action["entry_price"],
                    "size_usdc": sq_action["size_usdc"],
                    "size_shares": sq_action["size_shares"],
                    "strike": self._strike,
                    "entry_time": now,
                    "entry_elapsed": elapsed,
                    "edge": sq_action.get("spike_score", 0.0),
                    "fair_price": compute_fair_price(btc_price, self._strike, sigma, time_remaining),
                    "market_price_up": market_price_up,
                    "market_price_ts": 0.0,
                }
                sq_action["action"] = "ENTER"
                return sq_action

        # 4. Time-based edge entry
        if not self.has_position:
            entry = self.time_strategy.on_tick(btc_price, market_price_up, sigma, t_zero)
            if entry is not None and entry.get("action") == "ENTER":
                self._position_source = "edge"
                self._open_position = {
                    "side": entry["side"],
                    "entry_price": entry["entry_price"],
                    "size_usdc": entry["size_usdc"],
                    "size_shares": entry["size_shares"],
                    "strike": self._strike,
                    "entry_time": now,
                    "entry_elapsed": elapsed,
                    "edge": entry["edge"],
                    "fair_price": compute_fair_price(btc_price, self._strike, sigma, time_remaining),
                    "market_price_up": market_price_up,
                    "market_price_ts": 0.0,
                }
                return entry

        return None
