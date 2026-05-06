"""DataProvider ABC and the canonical ``MarketTick`` payload.

A ``DataProvider`` produces an asynchronous stream of ``MarketTick`` events.
Each tick carries everything the strategies need on a single tick:
the BTC spot, the Polymarket up-token mid, the EWMA-published sigma,
the current market's start time and strike, the slug, the per-slug book
snapshot, and the wall-clock timestamp on the mid quote (used by the
walked-VWAP gate's stale-quote check).

Three implementations:

- ``LiveDataProvider`` (``polyhustle.data.live``) — real websockets +
  state stitching, today's daemon I/O.
- ``PaperDataProvider`` (``polyhustle.data.paper``) — same data path as
  live; differs only in trader wiring (paper executors instead of live).
- ``ReplayDataProvider`` (``polyhustle.data.replay``) — replays a
  captured session from disk (Session C).

The ABC is intentionally narrow (``stream`` + ``shutdown``) so that the
Orchestrator does not couple to provider-specific lifecycle details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from active_bots.execution.live_book_state import MarketBooks


@dataclass(frozen=True)
class MarketTick:
    """One tick of market state — the input contract every Strategy reads.

    Attributes:
        timestamp: wall-clock seconds when this tick was assembled.
        btc_price: latest BTC spot from Binance (USD).
        market_price_up: latest Polymarket mid for the YES token (0..1),
            or None if a mid is not yet known for this market cycle.
        sigma: EWMA-published annualised volatility used by ``compute_fair_price``.
        t_zero: epoch-seconds market start of the current 5-minute cycle.
        strike: BTC strike for the current cycle (the spot at t_zero).
        slug: Polymarket slug, e.g. ``btc-updown-5m-1777985400``.
        books: per-slug ``MarketBooks`` snapshot used by the walked-VWAP
            entry+exit gates; None when the CLOB book WS hasn't delivered
            a baseline for this slug yet.
        market_price_ts: wall-clock seconds when ``market_price_up`` was
            last refreshed; the walked-VWAP gate rejects entries whose
            ``now - market_price_ts > WALKED_VWAP_MARKET_STALENESS_S``.

    Frozen: ticks are immutable once handed to the Orchestrator. A
    Strategy must not mutate a tick — make a copy first if you need to.
    """

    timestamp: float
    btc_price: float
    market_price_up: float | None
    sigma: float
    t_zero: float
    strike: float
    slug: str
    books: MarketBooks | None
    market_price_ts: float


class DataProvider(ABC):
    """Produces an async stream of ``MarketTick``.

    Implementations must be safe to ``await provider.shutdown()`` from a
    different task than the one consuming ``stream()``. ``shutdown()``
    causes any in-flight ``stream()`` to terminate cleanly (the async
    iterator stops yielding, no exception raised).
    """

    @abstractmethod
    def stream(self) -> AsyncIterator[MarketTick]:
        """Return an async iterator of ticks.

        Implementations may yield at any cadence (live: roughly 1 Hz —
        the daemon's strategy-loop sleep; replay: as fast as the
        captured event timestamps allow).
        """

    @abstractmethod
    async def shutdown(self) -> None:
        """Stop the underlying I/O sources and let ``stream()`` exit cleanly."""
