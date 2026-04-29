"""Tests for LiveExecutor fill-outcome + schema-parity fields.

Covers the executor-side half of the R2.2 predicate-shape work:

  - Fill outcome: requested_size_shares, filled_size_shares,
    residual_size_shares, classification, fill_vwap
  - Schema parity placeholders (null in this phase, populated once a
    book subscription lands): best_bid_at_{decision,ack},
    best_ask_at_{decision,ack}, top_of_book_size_{bid,ask}_at_ack,
    book_staleness_ms_at_{decision,ack}, fill_levels, levels_consumed

A parity invariant at the bottom asserts LiveExecutor, PaperExecutor,
and DryRunExecutor all return identical ``fill_details`` key sets.
"""

from __future__ import annotations

from typing import Any

import pytest

from active_bots.execution.dry_run_executor import DryRunExecutor
from active_bots.execution.executor import FILL_DETAILS_KEYS, MarketCtx
from active_bots.execution.live_executor import LiveExecutor
from active_bots.execution.paper_executor import PaperExecutor
from active_bots.execution.risk_manager import RiskConfig, RiskManager
from active_bots.execution.token_resolver import TokenResolver


def _ctx(slug: str = "btc-updown-5m-9999999000") -> MarketCtx:
    return MarketCtx(
        slug=slug,
        t_zero=9999999000,
        strike=60000.0,
        yes_token_id="YES_TOKEN_1",
        no_token_id="NO_TOKEN_1",
        tick_size=0.01,
    )


class _FakeClient:
    """Minimal stand-in for py-clob-client-v2.ClobClient.

    ``post_order_returns`` is a list of dicts; each call to
    ``create_and_post_market_order`` pops the next one (V2 collapsed the
    V1 two-call sign+post pattern into a single method).

    Response dicts use the V1 CamelCase shape (``makingAmount`` /
    ``takingAmount`` / ``orderID``); ``_extract_fills`` accepts both V1
    and V2 keys, so V1-shaped fixtures keep these tests valid until the
    live probe captures the actual V2 response shape.
    """

    def __init__(self, post_order_returns: list[dict[str, Any] | Exception]):
        self._returns = list(post_order_returns)
        self.post_calls: list[dict[str, Any]] = []

    def create_and_post_market_order(
        self, order_args: Any, options: Any = None, order_type: Any = None,
    ) -> Any:
        self.post_calls.append({
            "order_args": order_args,
            "options": options,
            "order_type": order_type,
        })
        nxt = self._returns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _live(client: _FakeClient) -> LiveExecutor:
    """Build a LiveExecutor with permissive risk + a stub resolver.

    RiskManager is constructed so decision.allowed == True for the
    test inputs. TokenResolver is not invoked because we thread token
    ids via MarketCtx directly.
    """
    resolver = TokenResolver()  # unused in these tests
    risk = RiskManager(
        RiskConfig(
            max_daily_loss_usdc=10_000.0,
            max_session_loss_usdc=0.0,    # disable session gate
            max_trade_size_usdc=1_000.0,
            min_trade_size_usdc=0.0,      # don't clamp up
        ),
    )
    return LiveExecutor(client=client, token_resolver=resolver, risk=risk)


def _entry_action() -> dict[str, Any]:
    return {
        "action": "ENTER",
        "side": "Up",
        "entry_price": 0.42,
        "size_usdc": 10.0,
        "size_shares": 10.0 / 0.42,
        "edge": 0.07,
        "fair": 0.49,
        "market": 0.42,
    }


# ── LiveExecutor fill_details ────────────────────────────────────────────


def test_live_enter_full_fill_populates_fill_details() -> None:
    # BUY $10 at an avg fill price of 0.40 -> 25 shares filled.
    client = _FakeClient([
        {
            "success": True,
            "orderID": "0xLIVE",
            "status": "matched",
            "makingAmount": "10.0",    # USD we gave
            "takingAmount": "25.0",    # shares we got
        },
    ])
    ex = _live(client)
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is not None
    fd = r.fill_details
    assert set(fd.keys()) == set(FILL_DETAILS_KEYS)

    assert fd["requested_size_shares"] == pytest.approx(10.0 / 0.42)
    assert fd["filled_size_shares"] == 25.0
    # Filled > quoted requested (better-than-quoted avg price); residual
    # floors at 0 and classification is still "full".
    assert fd["residual_size_shares"] == 0.0
    assert fd["classification"] == "full"
    assert fd["fill_vwap"] == pytest.approx(0.40)

    # Schema-parity placeholders: null in this phase.
    for k in (
        "best_bid_at_decision", "best_ask_at_decision",
        "best_bid_at_ack", "best_ask_at_ack",
        "top_of_book_size_bid_at_ack", "top_of_book_size_ask_at_ack",
        "book_staleness_ms_at_decision", "book_staleness_ms_at_ack",
        "fill_levels", "levels_consumed",
    ):
        assert fd[k] is None, f"expected {k} None in this phase; got {fd[k]!r}"


def test_live_enter_partial_fill_marks_classification_partial() -> None:
    # BUY requests 10.0 USD / 0.42 ≈ 23.81 shares. Market fills 20 @ 0.45.
    # takingAmount = 20 shares, makingAmount = 9.0 USD, avg = 0.45.
    client = _FakeClient([
        {
            "success": True,
            "orderID": "0xLIVEPART",
            "status": "matched",
            "makingAmount": "9.0",
            "takingAmount": "20.0",
        },
    ])
    ex = _live(client)
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is not None
    fd = r.fill_details
    assert fd["classification"] == "partial"
    assert fd["filled_size_shares"] == 20.0
    assert fd["residual_size_shares"] > 0.0
    assert fd["fill_vwap"] == pytest.approx(0.45)


def test_live_enter_unfilled_returns_none() -> None:
    # Existing behaviour: unfilled/rejected CLOB response yields None.
    # Null-safety requirement here is that the executor does not crash
    # while computing fill_details before short-circuiting.
    client = _FakeClient([
        {
            "success": False,
            "orderID": "0xREJECT",
            "status": "unmatched",
            "makingAmount": "0",
            "takingAmount": "0",
        },
    ])
    ex = _live(client)
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is None


def test_live_enter_post_order_exception_returns_none() -> None:
    client = _FakeClient([RuntimeError("boom")])
    ex = _live(client)
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is None


def test_live_exit_full_fill_populates_fill_details() -> None:
    # SELL 30 shares, avg exit 0.55 -> takingAmount=16.5 USD.
    client = _FakeClient([
        {
            "success": True,
            "orderID": "0xEXIT",
            "status": "matched",
            "makingAmount": "30.0",    # shares we gave
            "takingAmount": "16.5",    # USD we got
        },
    ])
    ex = _live(client)
    position = {
        "slug": "btc-updown-5m-9999999000",
        "side": "Up",
        "entry_price": 0.42,
        "size_usdc": 12.6,
        "size_shares": 30.0,
        "strike": 60000.0,
        "entry_time": 1776860000.0,
        "edge": 0.07,
        "token_id": "YES_TOKEN_1",
    }
    action = {"action": "EXIT_TP"}
    r = ex.exit(position, action, _ctx(), now=1776860010.0, btc_price=60500.0)
    assert r is not None
    fd = r.fill_details
    assert set(fd.keys()) == set(FILL_DETAILS_KEYS)
    assert fd["requested_size_shares"] == 30.0
    assert fd["filled_size_shares"] == 30.0
    assert fd["residual_size_shares"] == 0.0
    assert fd["classification"] == "full"
    assert fd["fill_vwap"] == pytest.approx(0.55)


# ── Parity invariant across LiveExecutor / PaperExecutor / DryRunExecutor ──


def _paper_entry_keys() -> set[str]:
    ex = PaperExecutor()
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is not None
    return set(r.fill_details.keys())


def _dryrun_entry_keys() -> set[str]:
    ex = DryRunExecutor()
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is not None
    return set(r.fill_details.keys())


def _live_entry_keys() -> set[str]:
    client = _FakeClient([
        {
            "success": True,
            "orderID": "0xP",
            "status": "matched",
            "makingAmount": "10.0",
            "takingAmount": "25.0",
        },
    ])
    ex = _live(client)
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is not None
    return set(r.fill_details.keys())


def test_all_executors_return_identical_fill_details_keys() -> None:
    paper = _paper_entry_keys()
    dryrun = _dryrun_entry_keys()
    live = _live_entry_keys()
    canonical = set(FILL_DETAILS_KEYS)
    assert paper == canonical
    assert dryrun == canonical
    assert live == canonical


def test_paper_executor_fill_details_book_fields_are_null() -> None:
    # Paper mode has no book data — must emit None, not a fake value.
    ex = PaperExecutor()
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is not None
    fd = r.fill_details
    for k in (
        "best_bid_at_decision", "best_ask_at_decision",
        "best_bid_at_ack", "best_ask_at_ack",
        "top_of_book_size_bid_at_ack", "top_of_book_size_ask_at_ack",
        "book_staleness_ms_at_decision", "book_staleness_ms_at_ack",
        "fill_levels", "levels_consumed",
    ):
        assert fd[k] is None


def test_paper_executor_fill_details_outcome_fields_populated() -> None:
    ex = PaperExecutor()
    r = ex.enter(_entry_action(), _ctx(), now=1776860000.0)
    assert r is not None
    fd = r.fill_details
    expected_shares = 10.0 / 0.42
    assert fd["requested_size_shares"] == pytest.approx(expected_shares)
    assert fd["filled_size_shares"] == pytest.approx(expected_shares)
    assert fd["residual_size_shares"] == 0.0
    assert fd["classification"] == "full"
    assert fd["fill_vwap"] == pytest.approx(0.42)
