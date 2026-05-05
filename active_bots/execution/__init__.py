"""Execution layer — converts strategy actions into fills.

Paper mode keeps the legacy in-memory simulation exactly as daemon_base_v1
used to do. Live mode sends real CLOB orders via py-clob-client.

The strategy (EnhancedStrategy) and the daemon's state/web layer are both
agnostic to which executor is active.
"""

from .executor import EntryResult, Executor, ExitResult, MarketCtx

__all__ = ["Executor", "MarketCtx", "EntryResult", "ExitResult"]
