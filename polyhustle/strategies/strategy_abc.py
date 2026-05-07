"""Strategy ABC — the contract every strategy class implements.

Why an ABC.
-----------
Every strategy class (``BaseStrategy``, ``EnhancedStrategy``,
``RefinedStrategy``, ``WalkedVWAPStrategy``) shares a common shape —
same ``on_tick`` / ``reset`` / ``has_position`` surface. The ABC
formalises that contract so the Orchestrator can dispatch ticks
through a typed interface and so anyone adding a new strategy has a
single declaration to follow.

Canonical signature.
--------------------
All concrete strategies accept the canonical shape
``(btc_price, market_price_up, sigma, t_zero,
market_price_ts=None, *, books=None, now=None, **kwargs)``. The
``**kwargs`` slot is forward-compat for future kwargs the
Orchestrator might pass — strategies that don't use them ignore them.

The ``now`` kwarg threads the tick's own timestamp into the
strategy. Live-mode callers omit ``now`` (it defaults to ``None``)
and strategies fall back to ``time.time()`` for elapsed-time math.
Replay-mode callers pass ``now=tick.timestamp`` so elapsed is
computed against the tick's historical time, not wall-clock — the
fix for the replay-mode dead loop diagnosed in
``reports/r4_replay_wiring_diag.md``. The Orchestrator passes
``now=tick.timestamp`` unconditionally; in live mode the tick's
timestamp IS wall-clock (modulo data-provider construction
microseconds) so behaviour is preserved.

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
        market_price_ts: float | None = None,
        *,
        books: MarketBooks | None = None,
        now: float | None = None,
        **kwargs: Any,
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
