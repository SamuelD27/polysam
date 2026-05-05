"""Tests for scripts/consolidate_data.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def test_module_imports():
    from scripts import consolidate_data  # noqa: F401


import pytest
from scripts.consolidate_data import slug_to_asset


@pytest.mark.parametrize("slug, expected", [
    ("btc-updown-5m-1777864200", "btc"),
    ("doge-up-or-down-5m-123", "doge"),
    ("eth-updown-5m-99", "eth"),
    ("sol-up-or-down-5m-1", "sol"),
    ("xrp-updown-5m-42", "xrp"),
    ("XRP-FOO", "xrp"),
])
def test_slug_to_asset_known_prefixes(slug, expected):
    assert slug_to_asset(slug) == expected


def test_slug_to_asset_unknown_returns_none():
    assert slug_to_asset("trump-2028-winner") is None
    assert slug_to_asset("") is None
    assert slug_to_asset(None) is None


from scripts.consolidate_data import (
    SCHEMA_MARKETS, SCHEMA_TRADES, SCHEMA_PRICE_HISTORIES,
    SCHEMA_ORDERBOOKS, SCHEMA_SPOT, SCHEMA_TRADERS,
    SCHEMA_DASHBOARD,
)


def test_schema_markets_columns():
    assert SCHEMA_MARKETS == [
        "condition_id", "slug", "asset", "title", "start_date", "end_date",
        "volume", "liquidity", "closed", "active",
        "yes_token_id", "no_token_id",
        "outcome_price_yes", "outcome_price_no", "resolved_outcome",
        "btc_price_at_start", "btc_price_at_end",
    ]


def test_schema_trades_columns():
    assert SCHEMA_TRADES == [
        "asset", "source", "trade_id", "wallet", "condition_id", "slug",
        "side", "outcome", "size", "price", "usdc_value", "fee_rate_bps",
        "match_time", "transaction_hash", "timestamp",
    ]


def test_schema_price_histories_columns():
    assert SCHEMA_PRICE_HISTORIES == [
        "asset", "condition_id", "timestamp", "yes_price", "no_price",
    ]


def test_schema_orderbooks_columns():
    assert SCHEMA_ORDERBOOKS == [
        "asset", "source", "condition_id", "slug", "side", "snapshot_time", "ts_ns",
        "event_type", "best_bid", "best_ask", "spread", "depth_10c",
        "bids_json", "asks_json",
        "asset_id", "canonical_tick", "effective_tick",
        "remote_hash", "local_hash", "reason",
    ]


def test_schema_spot_columns():
    assert SCHEMA_SPOT == [
        "asset", "source", "granularity", "timestamp",
        "open", "high", "low", "close", "volume", "price", "size",
    ]


def test_schema_traders_columns():
    assert SCHEMA_TRADERS == [
        "asset", "wallet", "total_trades", "total_volume_usdc", "win_rate",
        "avg_trade_size", "first_trade_time", "last_trade_time",
        "favorite_side", "favorite_outcome",
    ]


def test_schema_dashboard_columns():
    assert SCHEMA_DASHBOARD == [
        "ts", "btc_price", "market_price_up", "market_price_down",
        "fair_base", "fair_enh",
    ]


from scripts.consolidate_data import compute_ladder_metrics


def test_ladder_metrics_basic():
    bids = [{"price": "0.50", "size": "100"}, {"price": "0.49", "size": "50"}]
    asks = [{"price": "0.51", "size": "200"}, {"price": "0.52", "size": "75"}]
    m = compute_ladder_metrics(bids, asks)
    assert m["best_bid"] == 0.50
    assert m["best_ask"] == 0.51
    assert m["spread"] == pytest.approx(0.01)


def test_ladder_metrics_depth_10c():
    bids = [
        {"price": "0.50", "size": "100"},
        {"price": "0.45", "size": "50"},   # within 10c
        {"price": "0.40", "size": "999"},  # outside
        {"price": "0.41", "size": "1"},    # within 10c (0.50-0.41=0.09)
    ]
    asks = [
        {"price": "0.51", "size": "10"},
        {"price": "0.55", "size": "20"},   # within 10c
        {"price": "0.62", "size": "999"},  # outside
    ]
    m = compute_ladder_metrics(bids, asks)
    # bids within 10c of 0.50: 100 + 50 + 1 = 151
    # asks within 10c of 0.51: 10 + 20 = 30
    assert m["depth_10c"] == pytest.approx(151 + 30)


def test_ladder_metrics_empty():
    m = compute_ladder_metrics([], [])
    assert m == {"best_bid": None, "best_ask": None, "spread": None, "depth_10c": None}


def test_ladder_metrics_one_sided():
    m = compute_ladder_metrics([{"price": "0.5", "size": "1"}], [])
    assert m["best_bid"] == 0.5
    assert m["best_ask"] is None
    assert m["spread"] is None
    assert m["depth_10c"] == pytest.approx(1.0)
