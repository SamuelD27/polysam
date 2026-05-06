"""Replay data provider — STUB.

Session C will fill this in: read a captured session from disk
(``daemon_state/scrapes/<session>/``) and yield ``MarketTick`` events at
the same wall-clock cadence as the original run, so backtests can drive
the production strategy code path against historical data.

Until then, calling ``stream()`` raises ``NotImplementedError`` so
callers fail loudly instead of silently producing an empty stream. The
constructor still accepts the session path so the registry / config
plumbing can reference the class today.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from polyhustle.data.provider import DataProvider, MarketTick


class ReplayDataProvider(DataProvider):
    """Reads a captured session from disk and yields its ticks. (Stub.)"""

    def __init__(self, session_path: str | Path) -> None:
        self.session_path = Path(session_path)

    async def stream(self) -> AsyncIterator[MarketTick]:  # pragma: no cover — stub
        raise NotImplementedError(
            "ReplayDataProvider is a stub; Session C provides the body. "
            f"Requested session: {self.session_path}"
        )
        yield  # makes the function an async generator

    async def shutdown(self) -> None:  # pragma: no cover — stub
        return None
