"""Trader ABC + Decision / ExecutionResult dataclasses.

The Strategy emits action dicts (``{"action": "ENTER", ...}``,
``"EXIT_TP"``, ``"EXIT_SL"``, ``"RESOLVE"``, ``"WALKED_VWAP_REJECT"``).
The Trader consumes those decisions and places orders.

Why a thin wrapper.
-------------------
Today's executors (``PaperExecutor``, ``LiveExecutor``, ``DryRunExecutor``)
have a richer surface: separate ``enter``, ``exit``, ``resolve``, and
``reconcile`` methods. That surface works and is well tested. The Trader
ABC does NOT replace it — it exposes a single ``execute(decision, ctx)``
entry point and dispatches internally to the right executor method.

This keeps the Orchestrator's loop uniform (``decision in → result out``)
without disturbing the working execution layer.

Decision shape.
---------------
Decisions carry the strategy-emitted ``action`` string plus the canonical
fields (``side``, ``entry_price``, ``size_shares``, ``size_usdc``,
``edge``) and a free-form ``meta`` dict for everything else (fair price,
time zone, walked-VWAP diagnostics, exit-type, …). ``Decision.from_action``
converts a strategy action dict into a ``Decision`` non-destructively.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from active_bots.execution.executor import EntryResult, ExitResult, MarketCtx


# Action discriminators recognised by Trader.execute. The Strategy ABC
# emits these strings on action dicts; everything else is ignored or
# treated as a no-op by the Trader.
ACTION_ENTER = "ENTER"
ACTION_EXIT_TP = "EXIT_TP"
ACTION_EXIT_SL = "EXIT_SL"
ACTION_RESOLVE = "RESOLVE"
ACTION_REJECT = "WALKED_VWAP_REJECT"


@dataclass
class Decision:
    """One trader-bound decision derived from a strategy action.

    Attributes:
        action: discriminator — ``"ENTER"`` / ``"EXIT_TP"`` / ``"EXIT_SL"``
            / ``"RESOLVE"`` / ``"WALKED_VWAP_REJECT"``.
        side: ``"Up"`` / ``"Down"`` for entries; copied through on exits.
        entry_price: post-fee, post-walk per-share price the strategy
            wants to fill at. None on exits/resolves.
        size_shares: requested share count. None on exits/resolves.
        size_usdc: requested USDC notional (size_shares × entry_price).
        edge: gate-time edge (mid or walked, depending on strategy).
        meta: catch-all for diagnostics — fair, market, time_zone,
            walked_edge, effective_VWAP, reject_reason, etc. The Trader
            forwards meta into the executor's ``action`` arg unchanged.
    """

    action: str
    side: str | None = None
    entry_price: float | None = None
    size_shares: float | None = None
    size_usdc: float | None = None
    edge: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_action(cls, action: dict[str, Any]) -> Decision:
        """Build a Decision from a strategy action dict.

        Pulls the canonical keys into named attributes and stashes the
        remainder (including the original ``"action"`` key) into ``meta``
        so the executor sees the full original dict on dispatch.
        """
        canonical = {
            "action", "side", "entry_price", "size_shares", "size_usdc", "edge",
        }
        return cls(
            action=action.get("action", ""),
            side=action.get("side"),
            entry_price=action.get("entry_price"),
            size_shares=action.get("size_shares"),
            size_usdc=action.get("size_usdc"),
            edge=action.get("edge"),
            meta={k: v for k, v in action.items() if k not in canonical},
        )

    def to_action(self) -> dict[str, Any]:
        """Reverse ``from_action`` — produce the dict the executor expects."""
        out: dict[str, Any] = dict(self.meta)
        out["action"] = self.action
        if self.side is not None:
            out["side"] = self.side
        if self.entry_price is not None:
            out["entry_price"] = self.entry_price
        if self.size_shares is not None:
            out["size_shares"] = self.size_shares
        if self.size_usdc is not None:
            out["size_usdc"] = self.size_usdc
        if self.edge is not None:
            out["edge"] = self.edge
        return out


@dataclass
class ExecutionResult:
    """Outcome of one ``Trader.execute`` call.

    Exactly one of ``entry`` / ``exit_`` is populated for the success
    path of the matching action; both are None on rejects, no-ops, and
    the ``WALKED_VWAP_REJECT`` action.
    """

    action: str
    entry: EntryResult | None = None
    exit_: ExitResult | None = None
    rejected: bool = False
    reject_reason: str | None = None


class Trader(ABC):
    """Consumes decisions and places orders.

    Implementations wrap the existing ``Executor`` types in
    ``active_bots.execution`` — see ``live_trader.py``,
    ``paper_trader.py``, ``dryrun_trader.py``.
    """

    mode: str  # "paper" | "live"

    @abstractmethod
    def execute(
        self,
        decision: Decision,
        ctx: MarketCtx,
        *,
        position: dict[str, Any] | None = None,
        now: float | None = None,
        btc_price: float | None = None,
        source: str = "edge",
        books: Any = None,
    ) -> ExecutionResult:
        """Translate a Decision into an order or settlement.

        Args:
            decision: strategy-emitted decision.
            ctx: per-market context (slug, t_zero, strike, token ids).
            position: open position dict for ``EXIT_*`` / ``RESOLVE``;
                None for ``ENTER``.
            now: epoch-seconds wall clock. The Orchestrator passes the
                tick timestamp; trader-internal default is ``time.time()``.
            btc_price: spot needed for ``EXIT_*`` / ``RESOLVE`` PnL math.
            source: provenance label forwarded into the executor's
                ``enter`` (``"edge"`` / ``"squeeze"`` / ``"base"``).
            books: per-market book snapshot at decision time
                (``active_bots.execution.live_book_state.MarketBooks``).
                Consumed by ``PaperTrader`` for the realistic walked-VWAP
                fill simulator; ignored by ``LiveTrader`` /
                ``DryrunTrader`` / ``_ExecutorTrader`` (the executor sees
                the live CLOB book directly).
        """

    @abstractmethod
    def reconcile(self, now: float) -> None:
        """Per-tick housekeeping. Live: poll pending fills. Paper: no-op."""
