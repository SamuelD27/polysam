"""DryrunTrader — live-shaped fills with no CLOB POST.

Wraps ``active_bots.execution.dry_run_executor.DryRunExecutor`` in the
``Trader`` ABC. Behaviourally this is a "live-shape paper fill":
PaperExecutor delivers a realistic fill using the strategy's quoted
mid; the wrapper stamps a synthetic ``order_id`` (``dry-run-<ms>``)
and resolves the ``token_id`` from ``MarketCtx`` — so events.jsonl
rows match the schema reconcile.py / dashboards expect from a real
live session.

Status of "signs but does not post"
-----------------------------------
The user-facing intent for this trader is "actually exercise order
signing, just don't POST to the CLOB". Today's underlying executors
(``DryRunExecutor`` and ``LiveExecutor`` with ``dry_run=True``) both
short-circuit BEFORE py-clob-client's ``create_and_post_market_order``
runs — so signing is not exercised either.

The reason this commit doesn't add a real signing branch:
``live_executor.py`` is on the explicit protected-file list (per
CLAUDE.md and the prompt). Adding a separable "sign-only" call path
through py-clob-client-v2 lives outside the protected file but
requires a small refactor of LiveExecutor's order-submission helper
to reuse the signed args. That refactor needs the live-execution
operator's sign-off and is queued for a follow-up branch.

Until then, ``DryrunTrader`` is a contract-clean wrapper around the
existing dry-run path — usable today for end-to-end flow validation,
extensible to "actually signs" once the LiveExecutor refactor lands.
"""

from __future__ import annotations

from active_bots.execution.dry_run_executor import DryRunExecutor
from polyhustle.execution._executor_trader import _ExecutorTrader


class DryrunTrader(_ExecutorTrader):
    """Live-shaped fills + synthetic order_id; no real CLOB POST."""

    def __init__(self) -> None:
        super().__init__(DryRunExecutor())
