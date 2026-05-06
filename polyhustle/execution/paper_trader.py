"""PaperTrader — no real orders posted.

Wraps ``active_bots.execution.paper_executor.PaperExecutor`` in the
``Trader`` ABC. Uses Polymarket-realistic fill math but never touches
the network. Each instance owns its own ``PaperExecutor`` and therefore
its own simulated wallet — which is what makes Session B's "comparison
mode" work: N strategies, N PaperTraders, N independent wallets.
"""

from __future__ import annotations

from active_bots.execution.paper_executor import PaperExecutor
from polyhustle.execution._executor_trader import _ExecutorTrader


class PaperTrader(_ExecutorTrader):
    """Paper-mode trader. Each instance owns its own simulated wallet."""

    def __init__(self) -> None:
        super().__init__(PaperExecutor())
