"""Strategy ABC — the contract every strategy class implements.

Why an ABC.
-----------
Today's strategy classes (``BaseStrategy``, ``EnhancedStrategy``,
``RefinedStrategy``, ``WalkedVWAPStrategy``) already share a common
shape — same ``on_tick`` / ``reset`` / ``has_position`` surface. The
ABC formalises that contract so the Orchestrator can dispatch ticks
through a typed interface and so anyone adding a new strategy has a
single declaration to follow.

Findings — signature divergences (surfaced, not fixed).
-------------------------------------------------------
Python's ``ABC`` only enforces method NAMES, not signatures. The
classes inherit from ``Strategy`` cleanly today, but their
``on_tick`` signatures are not identical:

- ``BaseStrategy.on_tick(btc_price, market_price_up, sigma, t_zero)``
  — no ``market_price_ts``, no ``books``. The legacy daemon does NOT
  call ``BaseStrategy.on_tick`` (it inlines the entry math at
  ``daemon_base_v1.py:1695``); so this divergence is latent, not
  active.
- ``EnhancedStrategy.on_tick(btc_price, market_price_up, sigma,
  t_zero, market_price_ts=0.0)`` — adds ``market_price_ts`` as a
  positional arg with default. No ``books``.
- ``WalkedVWAPStrategy.on_tick(btc_price, market_price_up, sigma,
  t_zero, market_price_ts=0.0, *, books=None)`` — superset of all the
  above.

The ABC's ``on_tick`` documents the canonical signature
(``market_price_ts`` as a positional default + ``books`` as a kwarg
default) — i.e. the ``WalkedVWAPStrategy`` shape, which is the only
fully-walked production caller. Strategies that don't use
``market_price_ts`` or ``books`` simply ignore them; the daemon
already passes both to walked-vwap and only ``market_price_ts`` to
enhanced/refined.

The Orchestrator (Commit 7) passes both to every strategy uniformly.
The legacy ``daemon_base_v1.strategy_loop`` is unchanged.

Contract.
---------
- ``on_tick`` is pure compute. No I/O, no executor calls, no
  side-effects beyond the strategy's own ``self`` state. Returns an
  action dict consumed by the Orchestrator/Trader.
- ``reset`` is called on market rollover to clear the strategy's
  per-market state.
- ``has_position`` is True iff an open position is held in the
  current market.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from active_bots.execution.live_book_state import MarketBooks


class Strategy(ABC):
    """Abstract base class for all trading strategies.

    Concrete classes ship in ``active_bots/`` today; the shim modules in
    ``polyhustle.strategies`` re-export them.
    """

    @abstractmethod
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
        """Process one MarketTick and return an action dict (or None).

        Action dict shape (when non-None):
            ``{"action": "ENTER", "side": "Up" | "Down",
               "entry_price": float, "size_shares": float,
               "size_usdc": float, "edge": float, ...}``
            ``{"action": "EXIT_TP" | "EXIT_SL", ...}``
            ``{"action": "RESOLVE", "won": bool, "pnl": float, ...}``
            ``{"action": "WALKED_VWAP_REJECT", "reject_reason": str, ...}``

        Strategies may include additional diagnostic keys; the
        Orchestrator/Trader read the ``"action"`` discriminator and
        otherwise pass the dict through.
        """

    @abstractmethod
    def reset(
        self,
        t_zero: float | None = None,
        strike: float | None = None,
    ) -> None:
        """Reset per-market state for a new 5-minute cycle.

        Called by the Orchestrator on market rollover. Strategies clear
        any open position they hold and update their internal cycle
        markers (``t_zero``, ``strike``).
        """

    @property
    @abstractmethod
    def has_position(self) -> bool:
        """True iff an open position is currently held on this market."""
