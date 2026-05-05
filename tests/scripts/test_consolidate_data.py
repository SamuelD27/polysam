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


def _make_spot_db(path: Path, *, spot=(), ws_spot=()):
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE spot (
        timestamp INTEGER PRIMARY KEY, open REAL, high REAL,
        low REAL, close REAL, volume REAL)""")
    cur.execute("""CREATE TABLE ws_spot (
        timestamp TEXT, price REAL, size REAL)""")
    cur.executemany("INSERT INTO spot VALUES (?,?,?,?,?,?)", spot)
    cur.executemany("INSERT INTO ws_spot VALUES (?,?,?)", ws_spot)
    con.commit()
    con.close()


def test_write_spot_unified(tmp_path):
    from scripts.consolidate_data import write_spot

    db = tmp_path / "btc5m.db"
    _make_spot_db(
        db,
        spot=[(1773000000, 70000.0, 70100.0, 69900.0, 70050.0, 12.5)],
        ws_spot=[
            ("1773000300", 70075.5, 0.1),
            ("2026-04-24T07:10:19", 70080.0, 0.05),  # iso string
        ],
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_spot({"btc": db}, out_dir)

    rows = list(csv.DictReader((out_dir / "spot.csv").open()))
    assert len(rows) == 3

    rest_row = next(r for r in rows if r["source"] == "rest")
    assert rest_row["granularity"] == "5m"
    assert rest_row["timestamp"] == "1773000000"
    assert rest_row["open"] == "70000.0"
    assert rest_row["close"] == "70050.0"
    assert rest_row["price"] == ""  # NULL for REST
    assert rest_row["size"] == ""

    ws_rows = [r for r in rows if r["source"] == "ws"]
    assert len(ws_rows) == 2
    # Numeric string parsed
    numeric_ts = next(r for r in ws_rows if r["price"] == "70075.5")
    assert numeric_ts["timestamp"] == "1773000300"
    # ISO string parsed via fromisoformat
    iso_ts = next(r for r in ws_rows if r["price"] == "70080.0")
    from datetime import datetime, timezone
    expected = int(datetime.fromisoformat("2026-04-24T07:10:19").replace(tzinfo=timezone.utc).timestamp())
    assert iso_ts["timestamp"] == str(expected)
    assert iso_ts["open"] == ""  # NULL for WS


def _make_traders_db(path: Path, *, rows=()):
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE traders (
        wallet TEXT PRIMARY KEY, total_trades INTEGER,
        total_volume_usdc REAL, win_rate REAL, avg_trade_size REAL,
        first_trade_time TEXT, last_trade_time TEXT,
        favorite_side TEXT, favorite_outcome TEXT)""")
    cur.executemany("INSERT INTO traders VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def test_write_traders(tmp_path):
    from scripts.consolidate_data import write_traders

    db = tmp_path / "btc5m.db"
    _make_traders_db(db, rows=[
        ("0xWALLET1", 100, 5000.0, 0.55, 50.0, "1773000000", "1773100000", "BUY", "Up"),
    ])

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_traders({"btc": db}, out_dir)

    rows = list(csv.DictReader((out_dir / "traders.csv").open()))
    assert len(rows) == 1
    r = rows[0]
    assert r["asset"] == "btc"
    assert r["wallet"] == "0xWALLET1"
    assert r["total_trades"] == "100"
    assert r["win_rate"] == "0.55"


import gzip
import json


def test_append_book_feed_to_orderbooks(tmp_path):
    from scripts.consolidate_data import (
        write_orderbooks_rest, append_book_feed_to_orderbooks,
    )

    # Step 1: write the REST half (creates orderbooks.csv with header).
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_orderbooks_rest({}, out_dir)  # no DBs → header-only

    # Step 2: build a synthetic book_feed/<date>/<slug>.jsonl.gz fixture.
    feed_dir = tmp_path / "book_feed" / "2026-05-01"
    feed_dir.mkdir(parents=True)
    snapshot_record = {
        "type": "snapshot", "reason": "initial",
        "ts_ns": 1777864005346867486,
        "asset_id": "AID1",
        "slug": "btc-updown-5m-1777864200",
        "side": "yes",
        "condition_id": "0xCID",
        "canonical_tick": "0.01", "effective_tick": "0.01",
        "bids": [{"price": "0.50", "size": "100"}],
        "asks": [{"price": "0.51", "size": "200"}],
        "remote_hash": "rh1", "local_hash": "lh1",
    }
    book_record = {
        "type": "book",
        "ts_ns": 1777864010000000000,
        "asset_id": "AID1",
        "slug": "btc-updown-5m-1777864200",
        "side": "yes",
        "condition_id": "0xCID",
        "canonical_tick": "0.01",
        "raw": {
            "bids": [{"price": "0.49", "size": "5"}],
            "asks": [{"price": "0.52", "size": "10"}],
            "event_type": "book",
        },
    }
    feed_file = feed_dir / "btc-updown-5m-1777864200.jsonl.gz"
    with gzip.open(feed_file, "wt") as f:
        f.write(json.dumps(snapshot_record) + "\n")
        f.write(json.dumps(book_record) + "\n")

    # Step 3: append.
    n = append_book_feed_to_orderbooks(tmp_path / "book_feed", out_dir)
    assert n == 2

    rows = list(csv.DictReader((out_dir / "orderbooks.csv").open()))
    assert len(rows) == 2

    snap = next(r for r in rows if r["event_type"] == "snapshot")
    assert snap["asset"] == "btc"
    assert snap["source"] == "ws_book_feed"
    assert snap["slug"] == "btc-updown-5m-1777864200"
    assert snap["side"] == "yes"
    assert snap["condition_id"] == "0xCID"
    assert snap["asset_id"] == "AID1"
    assert snap["canonical_tick"] == "0.01"
    assert snap["effective_tick"] == "0.01"
    assert snap["remote_hash"] == "rh1"
    assert snap["local_hash"] == "lh1"
    assert snap["reason"] == "initial"
    assert snap["ts_ns"] == "1777864005346867486"
    # snapshot_time should be ISO8601 UTC
    assert snap["snapshot_time"].startswith("2026-")
    # Bids/asks JSON preserved
    assert json.loads(snap["bids_json"]) == [{"price": "0.50", "size": "100"}]
    # Derived metrics
    assert snap["best_bid"] == "0.5"
    assert snap["best_ask"] == "0.51"

    book = next(r for r in rows if r["event_type"] == "book")
    assert json.loads(book["bids_json"]) == [{"price": "0.49", "size": "5"}]
    assert book["best_bid"] == "0.49"


def test_write_daemon_events_two_pass(tmp_path):
    from scripts.consolidate_data import write_daemon_events

    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join([
        json.dumps({"ts": 1.0, "type": "session_start", "pid": 1234}),
        json.dumps({"ts": 2.0, "type": "startup", "mode": "paper", "max_trade_size": 100.0}),
        json.dumps({"ts": 3.0, "type": "market_rollover", "slug": "btc-updown-5m-1", "strike": 75000.0}),
    ]) + "\n")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    n = write_daemon_events(events_path, out_dir)
    assert n == 3

    with (out_dir / "daemon_events.csv").open() as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        rows = list(reader)
    # Key union should include keys from all events
    assert "ts" in cols
    assert "type" in cols
    assert "pid" in cols
    assert "mode" in cols
    assert "max_trade_size" in cols
    assert "slug" in cols
    assert "strike" in cols
    # ts and type should lead
    assert cols[0] == "ts"
    assert cols[1] == "type"
    # Each row populates only its own keys; others blank
    sess = next(r for r in rows if r["type"] == "session_start")
    assert sess["pid"] == "1234"
    assert sess["mode"] == ""
    assert sess["strike"] == ""

    boot = next(r for r in rows if r["type"] == "startup")
    assert boot["mode"] == "paper"
    assert boot["max_trade_size"] == "100.0"
    assert boot["pid"] == ""


def test_write_daemon_events_serializes_nested(tmp_path):
    from scripts.consolidate_data import write_daemon_events

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps({"ts": 1.0, "type": "x", "payload": {"a": 1, "b": [2, 3]}}) + "\n"
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_daemon_events(events_path, out_dir)

    rows = list(csv.DictReader((out_dir / "daemon_events.csv").open()))
    assert len(rows) == 1
    # Nested object JSON-serialized
    assert json.loads(rows[0]["payload"]) == {"a": 1, "b": [2, 3]}


def test_write_dashboard_history(tmp_path):
    from scripts.consolidate_data import write_dashboard_history

    src = tmp_path / "dashboard_history.jsonl"
    src.write_text("\n".join([
        json.dumps({"ts": 1.0, "btc_price": 70000.0,
                    "market_price_up": 0.6, "market_price_down": 0.4,
                    "fair_base": 0.55, "fair_enh": 0.57}),
        json.dumps({"ts": 2.0, "btc_price": 70010.0,
                    "market_price_up": 0.61, "market_price_down": 0.39,
                    "fair_base": 0.56, "fair_enh": 0.58}),
    ]) + "\n")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    n = write_dashboard_history(src, out_dir)
    assert n == 2

    rows = list(csv.DictReader((out_dir / "dashboard_history.csv").open()))
    assert len(rows) == 2
    assert rows[0]["btc_price"] == "70000.0"
    assert rows[0]["fair_enh"] == "0.57"


def test_main_end_to_end(tmp_path, capsys, monkeypatch):
    """End-to-end smoke test: build minimal fixtures and run main()."""
    from scripts import consolidate_data as cd

    # Build a single-asset fixture DB with one row in each table.
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    db = db_dir / "btc5m.db"
    con = sqlite3.connect(db)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE markets (
            condition_id TEXT PRIMARY KEY, slug TEXT, title TEXT,
            start_date TEXT, end_date TEXT, volume REAL, liquidity REAL,
            closed INTEGER, active INTEGER,
            yes_token_id TEXT, no_token_id TEXT,
            outcome_price_yes REAL, outcome_price_no REAL,
            resolved_outcome TEXT);
        CREATE TABLE trades (
            trade_id TEXT, wallet TEXT, condition_id TEXT, slug TEXT,
            side TEXT, outcome TEXT, size REAL, price REAL,
            usdc_value REAL, fee_rate_bps REAL,
            match_time TEXT, transaction_hash TEXT);
        CREATE TABLE ws_trades (
            timestamp TEXT, slug TEXT, side TEXT, outcome TEXT,
            price REAL, size REAL, source TEXT);
        CREATE TABLE price_histories (
            condition_id TEXT, timestamp INTEGER,
            yes_price REAL, no_price REAL);
        CREATE TABLE orderbooks (
            condition_id TEXT, side TEXT, bids TEXT, asks TEXT,
            best_bid REAL, best_ask REAL, spread REAL,
            depth_10c REAL, snapshot_time TEXT);
        CREATE TABLE spot (
            timestamp INTEGER PRIMARY KEY, open REAL, high REAL,
            low REAL, close REAL, volume REAL);
        CREATE TABLE ws_spot (
            timestamp TEXT, price REAL, size REAL);
        CREATE TABLE resolutions (
            condition_id TEXT PRIMARY KEY, slug TEXT,
            start_date TEXT, end_date TEXT,
            resolved_outcome TEXT, outcome_price_yes REAL, outcome_price_no REAL,
            volume REAL, btc_price_at_start REAL, btc_price_at_end REAL);
        CREATE TABLE traders (
            wallet TEXT PRIMARY KEY, total_trades INTEGER,
            total_volume_usdc REAL, win_rate REAL, avg_trade_size REAL,
            first_trade_time TEXT, last_trade_time TEXT,
            favorite_side TEXT, favorite_outcome TEXT);
    """)
    cur.execute("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("0xCID", "btc-updown-5m-1", "title", "s", "e",
                 100.0, 10.0, 0, 1, "yes", "no", 0.0, 0.0, None))
    cur.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("0xT1", "0xW", "0xCID", "btc-updown-5m-1",
                 "BUY", "Up", 1.0, 0.5, 0.5, 0.0, "1773000000", "0xTX"))
    cur.execute("INSERT INTO ws_trades VALUES (?,?,?,?,?,?,?)",
                ("1773000060", "btc-updown-5m-1", "SELL", "Down", 0.4, 1.0, "ws"))
    cur.execute("INSERT INTO price_histories VALUES (?,?,?,?)",
                ("0xCID", 1773000000, 0.5, 0.5))
    cur.execute("INSERT INTO orderbooks VALUES (?,?,?,?,?,?,?,?,?)",
                ("0xCID", "yes", "[]", "[]", None, None, None, None, "2026-04-24T07:10:19Z"))
    cur.execute("INSERT INTO spot VALUES (?,?,?,?,?,?)",
                (1773000000, 70000.0, 70100.0, 69900.0, 70050.0, 12.5))
    cur.execute("INSERT INTO ws_spot VALUES (?,?,?)",
                ("1773000300", 70075.5, 0.1))
    cur.execute("INSERT INTO traders VALUES (?,?,?,?,?,?,?,?,?)",
                ("0xW", 100, 5000.0, 0.55, 50.0, "1773000000", "1773100000", "BUY", "Up"))
    con.commit()
    con.close()

    # Daemon-state fixture: events + dashboard + book_feed
    daemon_dir = tmp_path / "daemon_state"
    daemon_dir.mkdir()
    (daemon_dir / "events.jsonl").write_text(
        json.dumps({"ts": 1.0, "type": "session_start", "pid": 1}) + "\n"
    )
    (daemon_dir / "dashboard_history.jsonl").write_text(
        json.dumps({"ts": 1.0, "btc_price": 70000.0,
                    "market_price_up": 0.6, "market_price_down": 0.4,
                    "fair_base": 0.55, "fair_enh": 0.57}) + "\n"
    )
    bf_dir = daemon_dir / "book_feed" / "2026-05-01"
    bf_dir.mkdir(parents=True)
    with gzip.open(bf_dir / "btc-updown-5m-1.jsonl.gz", "wt") as f:
        f.write(json.dumps({
            "type": "snapshot", "ts_ns": 1, "asset_id": "AID",
            "slug": "btc-updown-5m-1", "side": "yes", "condition_id": "0xCID",
            "canonical_tick": "0.01", "effective_tick": "0.01",
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.51", "size": "200"}],
        }) + "\n")

    out_dir = tmp_path / "consolidated"

    # Monkeypatch module-level paths to point at our fixture.
    monkeypatch.setattr(cd, "DATA_DIR", db_dir)
    monkeypatch.setattr(cd, "DAEMON_STATE_DIR", daemon_dir)
    monkeypatch.setattr(cd, "DB_PATHS", {a: db_dir / f"{a}5m.db" for a in cd.ASSETS})

    rc = cd.main(["--out-dir", str(out_dir)])
    assert rc == 0

    for name in [
        "markets.csv", "trades.csv", "price_histories.csv",
        "orderbooks.csv", "spot.csv", "traders.csv",
        "daemon_events.csv", "dashboard_history.csv",
    ]:
        assert (out_dir / name).exists(), f"missing {name}"
    # Spot-check row counts
    assert len(list(csv.DictReader((out_dir / "trades.csv").open()))) == 2  # 1 rest + 1 ws
    assert len(list(csv.DictReader((out_dir / "orderbooks.csv").open()))) == 2  # 1 rest + 1 ws
    captured = capsys.readouterr().out
    assert "Consolidation summary" in captured


def test_main_idempotent_rebuild(tmp_path, monkeypatch):
    """Running main() twice should produce identical output."""
    from scripts import consolidate_data as cd

    # Empty fixtures (no DBs, no daemon_state files).
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    daemon_dir = tmp_path / "daemon_state"
    daemon_dir.mkdir()
    out_dir = tmp_path / "consolidated"

    monkeypatch.setattr(cd, "DATA_DIR", db_dir)
    monkeypatch.setattr(cd, "DAEMON_STATE_DIR", daemon_dir)
    monkeypatch.setattr(cd, "DB_PATHS", {a: db_dir / f"{a}5m.db" for a in cd.ASSETS})

    cd.main(["--out-dir", str(out_dir)])
    first_run = {}
    for p in out_dir.glob("*.csv"):
        first_run[p.name] = p.read_bytes()

    cd.main(["--out-dir", str(out_dir)])
    for p in out_dir.glob("*.csv"):
        assert p.read_bytes() == first_run[p.name], f"{p.name} differs after rerun"


def test_book_feed_skips_truncated_gz(tmp_path):
    """A truncated .jsonl.gz must be skipped gracefully (not crash the run)."""
    from scripts.consolidate_data import (
        write_orderbooks_rest, append_book_feed_to_orderbooks,
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_orderbooks_rest({}, out_dir)

    feed_dir = tmp_path / "book_feed" / "2026-05-01"
    feed_dir.mkdir(parents=True)

    # File 1: valid (one snapshot record)
    good_file = feed_dir / "btc-updown-5m-1.jsonl.gz"
    with gzip.open(good_file, "wt") as f:
        f.write(json.dumps({
            "type": "snapshot", "ts_ns": 1, "asset_id": "AID",
            "slug": "btc-updown-5m-1", "side": "yes", "condition_id": "0xCID",
            "bids": [{"price": "0.5", "size": "1"}],
            "asks": [{"price": "0.6", "size": "1"}],
        }) + "\n")

    # File 2: truncated. Write valid gzip header + one record, then truncate the
    # final bytes so the gzip stream is incomplete.
    bad_file = feed_dir / "btc-updown-5m-2.jsonl.gz"
    with gzip.open(bad_file, "wt") as f:
        f.write(json.dumps({
            "type": "snapshot", "ts_ns": 2, "asset_id": "AID2",
            "slug": "btc-updown-5m-2", "side": "yes", "condition_id": "0xCID2",
            "bids": [{"price": "0.4", "size": "1"}],
            "asks": [{"price": "0.5", "size": "1"}],
        }) + "\n")
    # Truncate to half its size to corrupt the gzip stream
    raw = bad_file.read_bytes()
    bad_file.write_bytes(raw[:len(raw) // 2])

    # File 3: valid (one record after the bad file in sort order)
    good_file2 = feed_dir / "btc-updown-5m-3.jsonl.gz"
    with gzip.open(good_file2, "wt") as f:
        f.write(json.dumps({
            "type": "snapshot", "ts_ns": 3, "asset_id": "AID3",
            "slug": "btc-updown-5m-3", "side": "yes", "condition_id": "0xCID3",
            "bids": [{"price": "0.3", "size": "1"}],
            "asks": [{"price": "0.4", "size": "1"}],
        }) + "\n")

    # Should not raise. Returns count of valid rows from valid files.
    n = append_book_feed_to_orderbooks(tmp_path / "book_feed", out_dir)

    # Files 1 and 3 succeed. File 2 is partially or fully skipped.
    # Minimum guarantee: rows from file 1 and file 3 are present.
    rows = list(csv.DictReader((out_dir / "orderbooks.csv").open()))
    cids = {r["condition_id"] for r in rows}
    assert "0xCID" in cids
    assert "0xCID3" in cids
    # The bad file may yield 0, 1, or even produce an EOFError mid-line;
    # what matters is that the run completed and good files were preserved.
    assert n >= 2


def test_book_feed_skips_corrupted_zlib_block(tmp_path):
    """A .jsonl.gz with a corrupted compression block must also be skipped."""
    from scripts.consolidate_data import (
        write_orderbooks_rest, append_book_feed_to_orderbooks,
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_orderbooks_rest({}, out_dir)

    feed_dir = tmp_path / "book_feed" / "2026-05-01"
    feed_dir.mkdir(parents=True)

    good_file = feed_dir / "btc-updown-5m-1.jsonl.gz"
    with gzip.open(good_file, "wt") as f:
        f.write(json.dumps({
            "type": "snapshot", "ts_ns": 1, "asset_id": "AID",
            "slug": "btc-updown-5m-1", "side": "yes", "condition_id": "0xCID_OK",
            "bids": [{"price": "0.5", "size": "1"}],
            "asks": [{"price": "0.6", "size": "1"}],
        }) + "\n")

    # Build a "corrupted" gz: valid 10-byte gzip header followed by garbage
    # compressed bytes. This triggers zlib.error rather than EOFError.
    bad_file = feed_dir / "btc-updown-5m-2.jsonl.gz"
    bad_file.write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff" + b"\xff" * 64)

    n = append_book_feed_to_orderbooks(tmp_path / "book_feed", out_dir)

    rows = list(csv.DictReader((out_dir / "orderbooks.csv").open()))
    cids = {r["condition_id"] for r in rows}
    assert "0xCID_OK" in cids
    assert n >= 1
