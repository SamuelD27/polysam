"""Tests for OrdersLogWidget -- fingerprint dedup + ordering."""
from collections import deque
from unittest.mock import MagicMock

import dashboard as d


def _mk_log(seen_cap: int | None = None):
    w = d.OrdersLogWidget.__new__(d.OrdersLogWidget)
    cap = seen_cap if seen_cap is not None else d.OrdersLogWidget.SEEN_CAP
    w._seen = deque(maxlen=cap)
    w._seen_set = set()
    w.write = MagicMock()
    return w


def test_orders_log_evicts_oldest_fingerprint_at_cap():
    """Past SEEN_CAP entries, the oldest fingerprint is forgotten and a
    repeated action becomes loggable again."""
    w = _mk_log(seen_cap=2)
    w.ingest([
        {"ts": 1.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.10, "size_usdc": 1.0},
    ])
    w.ingest([
        {"ts": 2.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.20, "size_usdc": 1.0},
    ])
    w.ingest([
        {"ts": 3.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.30, "size_usdc": 1.0},
    ])
    # Cap is 2: ts=1 was evicted; re-ingesting ts=1 must write again.
    w.ingest([
        {"ts": 1.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.10, "size_usdc": 1.0},
    ])
    assert w.write.call_count == 4


def test_orders_log_dedups_repeated_ingest():
    w = _mk_log()
    actions = [
        {"ts": 1.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.42, "size_usdc": 5.0},
    ]
    w.ingest(actions)
    w.ingest(actions)
    w.ingest(actions)
    assert w.write.call_count == 1, "duplicate ingests must not re-write"


def test_orders_log_writes_new_actions_only():
    w = _mk_log()
    w.ingest([
        {"ts": 1.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.42, "size_usdc": 5.0},
    ])
    w.ingest([
        {"ts": 1.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.42, "size_usdc": 5.0},
        {"ts": 2.0, "kind": "SELL", "strategy": "refined",
         "side": "Up", "price": 0.55, "pnl": 0.65, "exit_type": "TP",
         "size_usdc": 5.0},
    ])
    assert w.write.call_count == 2


def test_orders_log_renders_buy_sell_res_lines():
    """Spot-check the line renderer integrates with the kinds we expect."""
    w = _mk_log()
    w.ingest([
        {"ts": 1.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.42, "size_usdc": 5.0},
        {"ts": 2.0, "kind": "SELL", "strategy": "refined",
         "side": "Up", "price": 0.55, "pnl": 0.65, "exit_type": "TP",
         "size_usdc": 5.0},
        {"ts": 3.0, "kind": "RES", "strategy": "refined",
         "side": "Up", "price": 1.0, "won": True, "pnl": 1.0,
         "size_usdc": 5.0},
    ])
    assert w.write.call_count == 3
    rendered = [str(c.args[0]) for c in w.write.call_args_list]
    assert "BUY" in rendered[0]
    assert "SELL" in rendered[1]
    assert "WON" in rendered[2]


def test_orders_log_distinguishes_actions_by_kind():
    """Same ts+price but different kind must be two log entries."""
    w = _mk_log()
    w.ingest([
        {"ts": 1.0, "kind": "BUY", "strategy": "refined",
         "side": "Up", "price": 0.42, "size_usdc": 5.0},
        {"ts": 1.0, "kind": "SELL", "strategy": "refined",
         "side": "Up", "price": 0.42, "pnl": 0.0, "exit_type": "RES",
         "size_usdc": 5.0},
    ])
    assert w.write.call_count == 2
