"""Tests for DryRunExecutor (R2.1 Option C).

DryRunExecutor wraps PaperExecutor so fills land at the real strategy-
requested price and stamps live-shaped metadata (order_id, token_id) on
the result. ack_ts is stamped by the daemon caller, not by this wrapper,
so it is not tested here.
"""

from __future__ import annotations

from active_bots.execution.dry_run_executor import DryRunExecutor
from active_bots.execution.executor import MarketCtx


def _ctx(slug="btc-updown-5m-9999999000") -> MarketCtx:
    return MarketCtx(
        slug=slug,
        t_zero=9999999000,
        strike=60000.0,
        yes_token_id="YES_TOKEN_1",
        no_token_id="NO_TOKEN_1",
        tick_size=0.01,
    )


def test_enter_up_fills_at_action_price_and_stamps_yes_token() -> None:
    ex = DryRunExecutor()
    action = {
        "side": "Up",
        "entry_price": 0.42,
        "size_usdc": 10.0,
        "size_shares": 10.0 / 0.42,
        "edge": 0.07,
        "fair": 0.49,
        "market": 0.42,
    }
    r = ex.enter(action, _ctx(), now=1776860000.0)
    assert r is not None
    # Realistic fill: entry_price equals the strategy's action price, NOT 0.5.
    assert r.entry_price == 0.42
    assert r.side == "Up"
    assert r.size_usdc == 10.0
    assert r.order_id is not None and r.order_id.startswith("dry-run-")
    assert r.token_id == "YES_TOKEN_1"


def test_enter_down_stamps_no_token() -> None:
    ex = DryRunExecutor()
    action = {
        "side": "Down",
        "entry_price": 0.63,
        "size_usdc": 8.0,
        "size_shares": 8.0 / 0.63,
        "edge": 0.10,
        "fair": 0.53,
        "market": 0.63,
    }
    r = ex.enter(action, _ctx(), now=1776860000.0)
    assert r is not None
    assert r.entry_price == 0.63
    assert r.token_id == "NO_TOKEN_1"


def test_enter_rejected_returns_none() -> None:
    ex = DryRunExecutor()
    # PaperExecutor rejects on size_usdc <= 0
    action = {"side": "Up", "entry_price": 0.42, "size_usdc": 0, "size_shares": 0}
    r = ex.enter(action, _ctx(), now=1776860000.0)
    assert r is None


def test_to_position_dict_includes_live_shape_fields() -> None:
    ex = DryRunExecutor()
    action = {
        "side": "Up", "entry_price": 0.42, "size_usdc": 10.0,
        "size_shares": 10.0 / 0.42, "edge": 0.07, "fair": 0.49, "market": 0.42,
    }
    r = ex.enter(action, _ctx(), now=1776860000.0)
    assert r is not None
    # Simulate daemon caller stamping ack_ts.
    r.ack_ts = 1776860000.125
    d = r.to_position_dict()
    assert d["order_id"].startswith("dry-run-")
    assert d["token_id"] == "YES_TOKEN_1"
    assert d["ack_ts"] == 1776860000.125
    assert d["entry_price"] == 0.42  # realistic fill, not 0.5


def test_exit_routes_to_paper_executor() -> None:
    ex = DryRunExecutor()
    pos = {
        "slug": "btc-updown-5m-9999999000",
        "side": "Up",
        "entry_price": 0.42,
        "size_usdc": 10.0,
        "size_shares": 10.0 / 0.42,
        "strike": 60000.0,
        "entry_time": 1776860000.0,
        "edge": 0.07,
    }
    action = {"action": "EXIT_TP", "exit_price": 0.55, "pnl": 3.10, "hold_time_s": 30.0}
    r = ex.exit(pos, action, _ctx(), now=1776860030.0, btc_price=60500.0)
    assert r is not None
    assert r.exit_type == "TP"
    assert r.exit_price == 0.55
    assert r.pnl == 3.10


def test_mode_label_is_live() -> None:
    # So the dashboard / downstream components that branch on .mode
    # treat this executor as live-flavoured.
    ex = DryRunExecutor()
    assert ex.mode == "live"
