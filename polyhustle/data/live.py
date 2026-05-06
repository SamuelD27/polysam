"""LiveDataProvider — wraps the daemon's websocket feeds + state stitching.

This is a behaviour-preserving extraction: the daemon's existing
``DaemonState``, ``EWMA``, ``binance_feed``, ``rtds_feed``,
``clob_book_feed``, and ``warm_start_market_price`` are reused as-is.
The provider's job is to start them as background tasks and expose
the resulting state as an async stream of ``MarketTick`` objects.

The data fields exposed on each tick come straight off ``DaemonState``
— every field today's strategies see in their ``on_tick`` arguments
remains available. Strategy-specific state (open positions, per-strategy
trade history) stays inside the daemon's container; that is not data-layer
concern and is unchanged here.

When ``polyhustle.orchestrator.Orchestrator`` runs, it will own a
``LiveDataProvider`` and consume the tick stream. The legacy daemon's
``run()`` continues to work in parallel — the two paths share no state
unless the operator explicitly wires them together.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

# Imports from the legacy daemon. These are intentional: the modular
# refactor reuses today's I/O coroutines verbatim to guarantee
# behaviour preservation. When the legacy daemon entry point is
# eventually retired, these helpers move into this package.
import daemon_base_v1 as _daemon  # noqa: E402  — repo-root sibling import
from polyhustle.data.provider import DataProvider, MarketTick


class LiveDataProvider(DataProvider):
    """Live-mode data provider: real Binance + Polymarket websockets.

    Holds a ``DaemonState`` + ``EWMA`` and runs the daemon's feed
    coroutines as background tasks. ``stream()`` yields one
    ``MarketTick`` per second (matching the daemon's strategy-loop
    cadence) until ``shutdown()`` is called.

    Args:
        tick_interval_s: seconds between yielded ticks. 1.0 matches the
            daemon's strategy-loop cadence; tests may pass a smaller value.
        warm_start: if True, run ``warm_start_market_price`` once at boot
            so the first tick has a non-None ``market_price_up``.
    """

    def __init__(
        self,
        *,
        tick_interval_s: float = 1.0,
        warm_start: bool = True,
    ) -> None:
        self._state = _daemon.DaemonState()
        self._ewma = _daemon.EWMA()
        self._tick_interval_s = tick_interval_s
        self._warm_start = warm_start
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._started = False

    @property
    def state(self) -> _daemon.DaemonState:
        """Underlying mutable state. Strategies must NOT mutate this directly."""
        return self._state

    def _start_feeds(self) -> None:
        """Start the websocket feed coroutines as background tasks."""
        if self._started:
            return
        self._state.detect_market()
        self._tasks = [
            asyncio.create_task(_daemon.binance_feed(self._state, self._ewma)),
            asyncio.create_task(_daemon.rtds_feed(self._state)),
            asyncio.create_task(_daemon.clob_book_feed(self._state)),
        ]
        if self._warm_start and self._state.slug:
            self._tasks.append(
                asyncio.create_task(
                    _daemon.warm_start_market_price(self._state, self._state.slug)
                )
            )
        self._started = True

    def _snapshot(self) -> MarketTick | None:
        """Build a ``MarketTick`` from current state, or None if essential fields are missing."""
        s = self._state
        if s.btc_price <= 0 or s.t_zero is None or s.strike is None or s.slug is None:
            return None
        # Recompute the fair-price scalars (the daemon does this once per
        # strategy_loop iteration; we mirror that so the sigma seen by the
        # tick stream is the same EWMA-published value the legacy strategies see).
        s.recompute_fair()
        books = s.books_by_slug.get(s.slug)
        return MarketTick(
            timestamp=time.time(),
            btc_price=s.btc_price,
            market_price_up=s.market_price_up,
            sigma=s.sigma if s.sigma is not None else 0.0,
            t_zero=float(s.t_zero),
            strike=float(s.strike),
            slug=s.slug,
            books=books,
            market_price_ts=s.market_price_ts,
        )

    async def stream(self) -> AsyncIterator[MarketTick]:
        """Yield one MarketTick every ``tick_interval_s`` until shutdown."""
        self._start_feeds()
        try:
            while not self._stop.is_set():
                tick = self._snapshot()
                if tick is not None:
                    yield tick
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._tick_interval_s
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def shutdown(self) -> None:
        """Stop the feed tasks and let any in-flight ``stream()`` exit cleanly."""
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
