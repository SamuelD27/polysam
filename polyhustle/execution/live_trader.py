"""LiveTrader — real CLOB orders.

Wraps ``active_bots.execution.live_executor.LiveExecutor`` in the
``Trader`` ABC. The wrapper is a pure pass-through; the protected
``live_executor.py`` is not modified.

Constructing a ``LiveTrader`` requires a built CLOB client and a
``TokenResolver``. The CLI builds them via the daemon's
``build_executor()`` helper; tests skip ``LiveTrader`` (or supply a
fake client) since the real CLOB requires a wallet + VPN netns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from active_bots.execution.live_executor import LiveExecutor
from polyhustle.execution._executor_trader import _ExecutorTrader

if TYPE_CHECKING:
    from active_bots.execution.reconciler import Reconciler
    from active_bots.execution.risk_manager import RiskManager
    from active_bots.execution.token_resolver import TokenResolver


class LiveTrader(_ExecutorTrader):
    """Live-mode trader — places real orders against the Polymarket CLOB."""

    def __init__(
        self,
        client,
        resolver: TokenResolver,
        risk: RiskManager,
        reconciler: Reconciler | None = None,
        *,
        dry_run: bool = False,
    ) -> None:
        super().__init__(
            LiveExecutor(client, resolver, risk, reconciler=reconciler, dry_run=dry_run)
        )
