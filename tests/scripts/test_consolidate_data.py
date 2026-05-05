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


import csv
import sqlite3
from pathlib import Path


def _make_test_db(path: Path, *, markets: list[tuple] = (), resolutions: list[tuple] = ()) -> None:
    """Create a minimal SQLite DB matching the production schema for testing."""
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE markets (
        condition_id TEXT PRIMARY KEY, slug TEXT, title TEXT,
        start_date TEXT, end_date TEXT, volume REAL, liquidity REAL,
        closed INTEGER, active INTEGER,
        yes_token_id TEXT, no_token_id TEXT,
        outcome_price_yes REAL, outcome_price_no REAL,
        resolved_outcome TEXT)""")
    cur.execute("""CREATE TABLE resolutions (
        condition_id TEXT PRIMARY KEY, slug TEXT,
        start_date TEXT, end_date TEXT,
        resolved_outcome TEXT, outcome_price_yes REAL, outcome_price_no REAL,
        volume REAL, btc_price_at_start REAL, btc_price_at_end REAL)""")
    cur.executemany(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        markets,
    )
    cur.executemany(
        "INSERT INTO resolutions VALUES (?,?,?,?,?,?,?,?,?,?)",
        resolutions,
    )
    con.commit()
    con.close()


def test_write_markets_dedups_and_merges_resolutions(tmp_path):
    from scripts.consolidate_data import write_markets

    btc_db = tmp_path / "btc5m.db"
    eth_db = tmp_path / "eth5m.db"
    # Same condition_id appears in both; the closed=1, higher-volume row wins.
    _make_test_db(
        btc_db,
        markets=[
            ("0xAAA", "btc-updown-5m-1", "BTC market", "s1", "e1",
             100.0, 10.0, 0, 1, "yes_aaa", "no_aaa", 0.0, 0.0, None),
        ],
        resolutions=[
            ("0xAAA", "btc-updown-5m-1", "s1", "e1",
             "Up", 1.0, 0.0, 200.0, 70000.0, 71000.0),
        ],
    )
    _make_test_db(
        eth_db,
        markets=[
            # Same condition_id, but closed=1 with higher volume — should win.
            ("0xAAA", "btc-updown-5m-1", "BTC market", "s1", "e1",
             200.0, 10.0, 1, 0, "yes_aaa", "no_aaa", 1.0, 0.0, "Up"),
            # Eth-only market, no resolution.
            ("0xBBB", "eth-updown-5m-2", "ETH market", "s2", "e2",
             50.0, 5.0, 0, 1, "yes_bbb", "no_bbb", 0.0, 0.0, None),
        ],
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_markets({"btc": btc_db, "eth": eth_db}, out_dir)

    rows = list(csv.DictReader((out_dir / "markets.csv").open()))
    by_id = {r["condition_id"]: r for r in rows}
    assert set(by_id) == {"0xAAA", "0xBBB"}

    aaa = by_id["0xAAA"]
    # eth row wins (closed=1 trumps closed=0)
    assert aaa["asset"] == "eth"
    assert aaa["closed"] == "1"
    assert aaa["volume"] == "200.0"
    # Resolution data merged in (from btc DB, since the resolutions table only existed there)
    assert aaa["btc_price_at_start"] == "70000.0"
    assert aaa["btc_price_at_end"] == "71000.0"
    assert aaa["resolved_outcome"] == "Up"

    bbb = by_id["0xBBB"]
    assert bbb["asset"] == "eth"
    # No resolution → resolution-only fields blank
    assert bbb["btc_price_at_start"] == ""
    assert bbb["btc_price_at_end"] == ""


def _make_trades_db(path: Path, *, trades=(), ws_trades=()):
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE trades (
        trade_id TEXT, wallet TEXT, condition_id TEXT, slug TEXT,
        side TEXT, outcome TEXT, size REAL, price REAL,
        usdc_value REAL, fee_rate_bps REAL,
        match_time TEXT, transaction_hash TEXT)""")
    cur.execute("""CREATE TABLE ws_trades (
        timestamp TEXT, slug TEXT, side TEXT, outcome TEXT,
        price REAL, size REAL, source TEXT)""")
    cur.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", trades)
    cur.executemany("INSERT INTO ws_trades VALUES (?,?,?,?,?,?,?)", ws_trades)
    con.commit()
    con.close()


def test_write_trades_rest_and_ws_unified(tmp_path):
    from scripts.consolidate_data import write_trades

    db = tmp_path / "btc5m.db"
    _make_trades_db(
        db,
        trades=[
            ("0xT1", "0xWALLET", "0xCID", "btc-updown-5m-1",
             "BUY", "Up", 1.5, 0.55, 0.825, 0.0, "1773148903", "0xTX1"),
        ],
        ws_trades=[
            ("1773149000", "btc-updown-5m-1", "SELL", "Down", 0.42, 2.0, "ws"),
        ],
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_trades({"btc": db}, out_dir)

    rows = list(csv.DictReader((out_dir / "trades.csv").open()))
    assert len(rows) == 2

    rest_row = next(r for r in rows if r["source"] == "rest")
    assert rest_row["asset"] == "btc"
    assert rest_row["trade_id"] == "0xT1"
    assert rest_row["wallet"] == "0xWALLET"
    assert rest_row["transaction_hash"] == "0xTX1"
    assert rest_row["match_time"] == "1773148903"
    assert rest_row["timestamp"] == ""  # NULL for REST

    ws_row = next(r for r in rows if r["source"] == "ws")
    assert ws_row["asset"] == "btc"
    assert ws_row["trade_id"] == ""  # NULL for WS
    assert ws_row["wallet"] == ""
    assert ws_row["transaction_hash"] == ""
    assert ws_row["match_time"] == ""
    assert ws_row["timestamp"] == "1773149000"
    assert ws_row["price"] == "0.42"
    assert ws_row["size"] == "2.0"
    assert ws_row["slug"] == "btc-updown-5m-1"


def _make_ph_db(path: Path, *, rows=()):
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE price_histories (
        condition_id TEXT, timestamp INTEGER,
        yes_price REAL, no_price REAL)""")
    cur.executemany("INSERT INTO price_histories VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()


def test_write_price_histories_concats_with_asset(tmp_path):
    from scripts.consolidate_data import write_price_histories

    btc_db = tmp_path / "btc5m.db"
    eth_db = tmp_path / "eth5m.db"
    _make_ph_db(btc_db, rows=[("0xC1", 1773000000, 0.5, 0.5)])
    _make_ph_db(eth_db, rows=[("0xC2", 1773000060, 0.6, 0.4),
                              ("0xC2", 1773000120, 0.61, 0.39)])

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_price_histories({"btc": btc_db, "eth": eth_db}, out_dir)

    rows = list(csv.DictReader((out_dir / "price_histories.csv").open()))
    assert len(rows) == 3
    assets = {r["asset"] for r in rows}
    assert assets == {"btc", "eth"}
    btc_row = next(r for r in rows if r["asset"] == "btc")
    assert btc_row["condition_id"] == "0xC1"
    assert btc_row["yes_price"] == "0.5"


def _make_orderbooks_db(path: Path, *, rows=()):
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE orderbooks (
        condition_id TEXT, side TEXT, bids TEXT, asks TEXT,
        best_bid REAL, best_ask REAL, spread REAL,
        depth_10c REAL, snapshot_time TEXT)""")
    cur.executemany("INSERT INTO orderbooks VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def test_write_orderbooks_rest_only(tmp_path):
    from scripts.consolidate_data import write_orderbooks_rest

    btc_db = tmp_path / "btc5m.db"
    bids_json = '[{"price": "0.50", "size": "100"}]'
    asks_json = '[{"price": "0.51", "size": "200"}]'
    _make_orderbooks_db(btc_db, rows=[
        ("0xCID", "yes", bids_json, asks_json, 0.50, 0.51, 0.01, 300.0, "2026-04-24T07:10:19Z"),
    ])

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_orderbooks_rest({"btc": btc_db}, out_dir)

    rows = list(csv.DictReader((out_dir / "orderbooks.csv").open()))
    assert len(rows) == 1
    r = rows[0]
    assert r["asset"] == "btc"
    assert r["source"] == "rest"
    assert r["event_type"] == "rest_snapshot"
    assert r["bids_json"] == bids_json
    assert r["asks_json"] == asks_json
    assert r["best_bid"] == "0.5"
    assert r["snapshot_time"] == "2026-04-24T07:10:19Z"
    # WS-only fields blank
    assert r["ts_ns"] == ""
    assert r["asset_id"] == ""
    assert r["canonical_tick"] == ""
    assert r["remote_hash"] == ""
