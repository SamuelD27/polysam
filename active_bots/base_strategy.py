"""Base fair-price edge strategy for BTC 5m binary options.

Strategy: compute GBM fair price, compare vs market price, buy underpriced
tokens when edge exceeds threshold, hold to resolution.
"""

from __future__ import annotations

import math
import time
from typing import Any

from .pricing.constants import SECONDS_PER_YEAR

# ── Constants ──────────────────────────────────────────────────────────────

EDGE_MIN = 0.10
EDGE_MAX = 0.25
MAX_RISK = 100.0
SPREAD_COST = 0.01
MARKET_DURATION = 300


# ── Helper functions ───────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    """Standard normal CDF via erfc -- no scipy needed."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def compute_fair_price(
    spot: float,
    strike: float,
    sigma: float,
    time_remaining_s: float,
) -> float:
    """Return P(Up) using Phi(d2) where d2 = ln(S/K) / (sigma * sqrt(tau)).

    Edge cases:
        - expired (time_remaining_s <= 0): deterministic outcome
        - zero vol (sigma ~ 0): deterministic outcome
        - S == K: 0.5 (no drift assumption)
    """
    if time_remaining_s <= 0:
        if spot > strike:
            return 1.0
        elif spot < strike:
            return 0.0
        else:
            return 0.5

    if sigma <= 1e-12:
        if spot > strike:
            return 1.0
        elif spot < strike:
            return 0.0
        else:
            return 0.5

    if spot == strike:
        return 0.5

    tau = time_remaining_s / SECONDS_PER_YEAR
    d2 = math.log(spot / strike) / (sigma * math.sqrt(tau))
    return norm_cdf(d2)


def compute_edge(
    fair_price: float,
    market_price: float,
) -> tuple[float, str, float]:
    """Compute edge between fair and market price.

    Returns:
        (edge, side, entry_price) where:
        - edge = abs(fair - market)
        - side = "Up" if fair > market, else "Down"
        - entry_price = market if buying Up, 1.0 - market if buying Down
    """
    edge = abs(fair_price - market_price)
    if fair_price > market_price:
        side = "Up"
        entry_price = market_price
    else:
        side = "Down"
        entry_price = 1.0 - market_price
    return edge, side, entry_price


def compute_position_size(
    edge: float,
    entry_price: float,
    edge_min: float = EDGE_MIN,
    edge_max: float = EDGE_MAX,
    max_risk: float = MAX_RISK,
) -> tuple[float, float]:
    """Linear interpolation of position size between edge_min and edge_max.

    Returns:
        (size_usdc, size_shares)
        - size_usdc: dollar amount to risk
        - size_shares: number of shares (= size_usdc / entry_price)
    """
    if edge < edge_min:
        return (0.0, 0.0)

    fraction = min((edge - edge_min) / (edge_max - edge_min), 1.0)
    size_usdc = fraction * max_risk
    size_shares = size_usdc / entry_price
    return size_usdc, size_shares


def resolve_trade(
    position: dict[str, Any],
    final_btc_price: float,
) -> dict[str, Any]:
    """Resolve a trade given the final BTC price at expiry.

    Args:
        position: dict with keys: side, entry_price, size_shares, strike
        final_btc_price: BTC price at market resolution (T+300)

    Returns:
        dict with: won (bool), exit_price (1.0 or 0.0), pnl (float)
    """
    side = position["side"]
    entry_price = position["entry_price"]
    size_shares = position["size_shares"]
    strike = position["strike"]

    btc_up = final_btc_price > strike

    if side == "Up":
        won = btc_up
    else:
        won = not btc_up

    if won:
        exit_price = 1.0
    else:
        exit_price = 0.0

    pnl = (exit_price - entry_price) * size_shares

    return {
        "won": won,
        "exit_price": exit_price,
        "pnl": pnl,
        "side": side,
        "entry_price": entry_price,
        "size_shares": size_shares,
        "strike": strike,
        "final_btc_price": final_btc_price,
    }


# ── BaseStrategy ───────────────────────────────────────────────────────────

class BaseStrategy:
    """Stateful base strategy for a single BTC 5m binary option market.

    Call on_tick() with each price update. The strategy will:
    1. Wait until the entry window (T+entry_offset_min to T+entry_offset_max)
    2. Compute fair price and edge
    3. Enter if edge >= edge_min
    4. Hold to resolution (T+300)

    Attributes:
        edge_min: minimum edge to enter a trade
        edge_max: edge at which position is max-sized
        max_risk: maximum USDC per trade
        entry_offset_min: earliest entry offset from market start (seconds)
        entry_offset_max: latest entry offset from market start (seconds)
    """

    def __init__(
        self,
        edge_min: float = EDGE_MIN,
        edge_max: float = EDGE_MAX,
        max_risk: float = MAX_RISK,
        entry_offset_min: int = 120,
        entry_offset_max: int = 150,
        role: str = "observer",
    ):
        if role not in ("trader", "observer"):
            raise ValueError(
                f"role must be 'trader' or 'observer', got {role!r}"
            )
        self.edge_min = edge_min
        self.edge_max = edge_max
        self.max_risk = max_risk
        self.entry_offset_min = entry_offset_min
        self.entry_offset_max = entry_offset_max
        self.role = role

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

        Args:
            btc_price: current BTC spot price
            market_price_up: current market price for the Up token (0..1)
            sigma: current annualized volatility estimate
            t_zero: market start timestamp (epoch seconds)

        Returns:
            None if no action, or a dict:
            - {"action": "ENTER", "side": ..., "entry_price": ..., "edge": ...,
               "size_usdc": ..., "size_shares": ...}
            - {"action": "RESOLVE", "won": ..., "pnl": ..., ...}
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

        # ── Entry check ──
        if not self.has_position:
            if self.entry_offset_min <= elapsed <= self.entry_offset_max:
                fair = compute_fair_price(btc_price, self._strike, sigma, time_remaining)
                edge, side, entry_price = compute_edge(fair, market_price_up)

                if edge >= self.edge_min:
                    size_usdc, size_shares = compute_position_size(
                        edge, entry_price,
                        edge_min=self.edge_min,
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
                    }

        return None
