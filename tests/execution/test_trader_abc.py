"""Tests for the Trader ABC + Decision dataclass.

Two contracts:

1. ``Decision.from_action`` / ``to_action`` round-trip preserves the
   strategy's emitted action dict modulo key ordering.
2. ``PaperTrader`` (the simplest concrete Trader) dispatches the
   correct executor method per action discriminator.

LiveTrader is not exercised here — instantiating it would require a
real CLOB client. The dispatch logic is shared via ``_ExecutorTrader``
and tested through PaperTrader.
"""

from __future__ import annotations

from typing import Any

from active_bots.execution.executor import EntryResult, ExitResult, MarketCtx
from polyhustle.execution.paper_trader import PaperTrader
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


def test_decision_roundtrips_action_dict():
    raw: dict[str, Any] = {
        "action": "ENTER",
        "side": "Up",
        "entry_price": 0.42,
        "size_shares": 100.0,
        "size_usdc": 42.0,
        "edge": 0.12,
        "fair": 0.50,
        "market": 0.38,
        "time_zone": "mid",
    }
    d = Decision.from_action(raw)
    assert d.action == "ENTER"
    assert d.side == "Up"
    assert d.entry_price == 0.42
    assert d.size_shares == 100.0
    assert d.size_usdc == 42.0
    assert d.edge == 0.12
    assert d.meta == {"fair": 0.50, "market": 0.38, "time_zone": "mid"}
    out = d.to_action()
    assert out == raw


def test_paper_trader_satisfies_abc():
    t = PaperTrader()
    assert isinstance(t, Trader)
    assert t.mode == "paper"


def test_paper_trader_enter_returns_entry_result():
    # gate_passed=True signals the walked-VWAP gate already produced the
    # fill upstream — pass-through avoids the wrapper's book-walk
    # (which would otherwise reject with paper_no_fill since no MarketBooks
    # is provided here). This test exercises pure dispatch, not the fill
    # realism path; the latter is covered by test_paper_trader_fill_realism.
    t = PaperTrader()
    ctx = MarketCtx(slug="btc-updown-5m-1", t_zero=1, strike=110_000.0)
    decision = Decision(
        action=ACTION_ENTER,
        side="Up",
        entry_price=0.42,
        size_shares=10.0,
        size_usdc=4.20,
        edge=0.15,
        meta={"fair": 0.50, "market": 0.38, "gate_passed": True},
    )
    res = t.execute(decision, ctx, source="edge", now=100.0)
    assert isinstance(res, ExecutionResult)
    assert res.action == ACTION_ENTER
    assert res.rejected is False
    assert isinstance(res.entry, EntryResult)
    assert res.entry.side == "Up"


def test_paper_trader_exit_requires_position():
    t = PaperTrader()
    ctx = MarketCtx(slug="btc-updown-5m-1", t_zero=1, strike=110_000.0)
    decision = Decision(action=ACTION_EXIT_TP, meta={"exit_price": 0.55})
    res = t.execute(decision, ctx, position=None, btc_price=110_000.0, now=200.0)
    assert res.rejected is True
    assert res.reject_reason == "no_open_position"


def test_paper_trader_resolve_returns_exit_result():
    # gate_passed=True: pass-through entry without book-walking. See
    # test_paper_trader_enter_returns_entry_result for rationale.
    t = PaperTrader()
    ctx = MarketCtx(slug="btc-updown-5m-1", t_zero=1, strike=110_000.0)
    enter = t.execute(
        Decision(
            action=ACTION_ENTER,
            side="Up",
            entry_price=0.42,
            size_shares=10.0,
            size_usdc=4.20,
            edge=0.15,
            meta={"gate_passed": True},
        ),
        ctx,
        source="edge",
        now=100.0,
    )
    assert enter.entry is not None
    pos = enter.entry.to_position_dict()

    res = t.execute(
        Decision(action=ACTION_RESOLVE, meta={}),
        ctx,
        position=pos,
        btc_price=111_000.0,  # finished above strike → Up wins
        now=400.0,
    )
    assert res.action == ACTION_RESOLVE
    assert res.exit_ is not None
    assert isinstance(res.exit_, ExitResult)
    assert res.exit_.exit_type == "RESOLUTION"


def test_paper_trader_walked_vwap_reject_returns_rejected():
    t = PaperTrader()
    ctx = MarketCtx(slug="btc-updown-5m-1", t_zero=1, strike=110_000.0)
    decision = Decision(
        action=ACTION_REJECT,
        meta={"reject_reason": "insufficient_walked_edge"},
    )
    res = t.execute(decision, ctx)
    assert res.rejected is True
    assert res.reject_reason == "insufficient_walked_edge"


def test_paper_trader_exit_sl_with_position():
    """EXIT_SL with a position dispatches to executor.exit."""
    # gate_passed=True: pass-through entry without book-walking. See
    # test_paper_trader_enter_returns_entry_result for rationale.
    t = PaperTrader()
    ctx = MarketCtx(slug="btc-updown-5m-1", t_zero=1, strike=110_000.0)
    enter = t.execute(
        Decision(
            action=ACTION_ENTER,
            side="Up",
            entry_price=0.42,
            size_shares=10.0,
            size_usdc=4.20,
            edge=0.15,
            meta={"gate_passed": True},
        ),
        ctx,
        source="edge",
        now=100.0,
    )
    assert enter.entry is not None
    pos = enter.entry.to_position_dict()

    res = t.execute(
        Decision(
            action=ACTION_EXIT_SL,
            meta={"exit_price": 0.30, "exit_type": "SL"},
        ),
        ctx,
        position=pos,
        btc_price=109_000.0,
        now=200.0,
    )
    assert res.action == ACTION_EXIT_SL
    assert res.exit_ is not None


def test_trader_execute_accepts_books_kwarg():
    """Trader.execute must accept a `books` kwarg even when ignored."""
    from active_bots.execution.live_book_state import MarketBooks
    from active_bots.execution.paper_executor import PaperExecutor
    from polyhustle.execution._executor_trader import _ExecutorTrader

    trader = _ExecutorTrader(PaperExecutor())
    ctx = MarketCtx(slug="x", t_zero=1, strike=100.0)
    decision = Decision(action="UNKNOWN")  # no-op, just verifying signature
    # Should not raise; books is ignored by _ExecutorTrader.
    result = trader.execute(decision, ctx, books=MarketBooks(yes=None, no=None))
    assert result.rejected is True  # unknown_action path
