"""Shared base — wraps an ``Executor`` in the ``Trader`` ABC.

Used by ``live_trader`` / ``paper_trader`` / ``dryrun_trader`` so the
dispatch logic (action → enter / exit / resolve) lives in one place.
The wrapped executor is exposed as ``self._executor`` for the rare
case a caller needs the underlying object.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from polyhustle.execution.trader import (
    ACTION_ENTER,
    ACTION_EXIT_SL,
    ACTION_EXIT_TP,
    ACTION_REJECT,
    ACTION_RESOLVE,
    Decision,
    ExecutionResult,
    Trader,
)

if TYPE_CHECKING:
    from active_bots.execution.executor import Executor, MarketCtx


class _ExecutorTrader(Trader):
    """Trader implementation that delegates to an ``Executor``.

    Concrete subclasses set ``self._executor`` in ``__init__`` (e.g.
    ``self._executor = PaperExecutor()``) and inherit the dispatch
    logic from this class.
    """

    def __init__(self, executor: Executor) -> None:
        self._executor = executor
        self.mode = executor.mode

    def execute(
        self,
        decision: Decision,
        ctx: MarketCtx,
        *,
        position: dict[str, Any] | None = None,
        now: float | None = None,
        btc_price: float | None = None,
        source: str = "edge",
    ) -> ExecutionResult:
        """Dispatch one decision to the underlying executor.

        Returns an ``ExecutionResult`` with ``entry`` / ``exit_`` / both
        None depending on outcome. Rejects (executor returns None) are
        signalled via ``rejected=True`` so the Orchestrator can log a
        reject event without distinguishing them from no-ops.
        """
        when = now if now is not None else time.time()
        action_dict = decision.to_action()

        if decision.action == ACTION_ENTER:
            entry = self._executor.enter(action_dict, ctx, when, source=source)
            if entry is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True,
                    reject_reason="executor_rejected_entry",
                )
            entry.ack_ts = time.time()
            return ExecutionResult(action=decision.action, entry=entry)

        if decision.action in (ACTION_EXIT_TP, ACTION_EXIT_SL):
            if position is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True,
                    reject_reason="no_open_position",
                )
            if btc_price is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True,
                    reject_reason="missing_btc_price",
                )
            exit_ = self._executor.exit(
                position, action_dict, ctx, when, btc_price=btc_price,
            )
            if exit_ is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True,
                    reject_reason="executor_rejected_exit",
                )
            return ExecutionResult(action=decision.action, exit_=exit_)

        if decision.action == ACTION_RESOLVE:
            if position is None or btc_price is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True,
                    reject_reason="missing_position_or_btc_price",
                )
            res = self._executor.resolve(
                position, ctx, when, btc_price=btc_price,
            )
            if res is None:
                return ExecutionResult(
                    action=decision.action,
                    rejected=True,
                    reject_reason="executor_rejected_resolve",
                )
            return ExecutionResult(action=decision.action, exit_=res)

        if decision.action == ACTION_REJECT:
            return ExecutionResult(
                action=decision.action,
                rejected=True,
                reject_reason=decision.meta.get("reject_reason", "walked_vwap_reject"),
            )

        return ExecutionResult(action=decision.action, rejected=True,
                               reject_reason="unknown_action")

    def reconcile(self, now: float) -> None:
        """Per-tick housekeeping forwarded to the wrapped executor."""
        self._executor.reconcile(now)
