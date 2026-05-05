# Polymarket Data Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One-shot Python script that walks all per-asset SQLite DBs and daemon-run JSONL files, then writes 8 standardized CSVs to `data/consolidated/` with no information loss.

**Architecture:** Single Python module `scripts/consolidate_data.py` with stdlib-only deps. Each output CSV gets a dedicated writer function that streams rows from its source(s); `main()` orchestrates them in dependency order. Tests use temporary fixture DBs/JSONL via `pytest.tmp_path`.

**Tech Stack:** Python 3.11+, stdlib (`sqlite3`, `csv`, `json`, `gzip`, `pathlib`, `datetime`, `argparse`), pytest for tests.

**Spec:** `docs/superpowers/specs/2026-05-05-data-consolidation-design.md`

---

## File Structure

**Create:**
- `scripts/consolidate_data.py` — the consolidation script (single module, ~500-700 lines)
- `tests/scripts/__init__.py` — empty package marker
- `tests/scripts/test_consolidate_data.py` — unit tests against fixture data

**No existing files modified.** Legacy `data/0X_*.csv` files left untouched per spec.

---

## Task 1: Project scaffolding

**Files:**
- Create: `scripts/__init__.py` (empty — makes `scripts/` a Python package so `from scripts.consolidate_data import ...` works)
- Create: `scripts/consolidate_data.py`
- Create: `tests/scripts/__init__.py` (empty)
- Create: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_consolidate_data.py
"""Tests for scripts/consolidate_data.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def test_module_imports():
    from scripts import consolidate_data  # noqa: F401
```

```python
# tests/scripts/__init__.py
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/samsam/polymarket-hustle && pytest tests/scripts/test_consolidate_data.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.consolidate_data'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/__init__.py` (empty file):

```python
```

Create `scripts/consolidate_data.py`:

```python
# scripts/consolidate_data.py
"""Consolidate scraped data and daemon-run output into 8 standardized CSVs.

Spec: docs/superpowers/specs/2026-05-05-data-consolidation-design.md

Usage: python scripts/consolidate_data.py [--out-dir DIR]
"""
from __future__ import annotations

import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DAEMON_STATE_DIR = REPO_ROOT / "daemon_state"
DEFAULT_OUT_DIR = DATA_DIR / "consolidated"

ASSETS = ("btc", "doge", "eth", "sol", "xrp")
DB_PATHS = {asset: DATA_DIR / f"{asset}5m.db" for asset in ASSETS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/samsam/polymarket-hustle && pytest tests/scripts/test_consolidate_data.py -v`
Expected: 1 passed.

Also run: `python scripts/consolidate_data.py --out-dir /tmp/_consolidate_smoke`
Expected: prints `Output directory: /tmp/_consolidate_smoke` and exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/consolidate_data.py tests/scripts/__init__.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): scaffold consolidate_data script"
```

---

## Task 2: Asset detection from slug

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_slug_to_asset_known_prefixes -v`
Expected: FAIL with `ImportError: cannot import name 'slug_to_asset'`

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
def slug_to_asset(slug: str | None) -> str | None:
    """Map a market slug to its underlying asset, or None if unknown."""
    if not slug:
        return None
    s = slug.lower()
    for asset in ASSETS:
        if s.startswith(f"{asset}-"):
            return asset
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: 7 passed (1 import + 6 slug + 1 unknown = 8 total, all pass).

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): add slug_to_asset helper"
```

---

## Task 3: Schema constants for all 8 CSVs

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

(Note: `SCHEMA_DAEMON_EVENTS` is not constant — it's computed dynamically from the events.jsonl key union — so it's not asserted here. It's tested separately in Task 13.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_schema_markets_columns -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
SCHEMA_MARKETS = [
    "condition_id", "slug", "asset", "title", "start_date", "end_date",
    "volume", "liquidity", "closed", "active",
    "yes_token_id", "no_token_id",
    "outcome_price_yes", "outcome_price_no", "resolved_outcome",
    "btc_price_at_start", "btc_price_at_end",
]

SCHEMA_TRADES = [
    "asset", "source", "trade_id", "wallet", "condition_id", "slug",
    "side", "outcome", "size", "price", "usdc_value", "fee_rate_bps",
    "match_time", "transaction_hash", "timestamp",
]

SCHEMA_PRICE_HISTORIES = [
    "asset", "condition_id", "timestamp", "yes_price", "no_price",
]

SCHEMA_ORDERBOOKS = [
    "asset", "source", "condition_id", "slug", "side", "snapshot_time", "ts_ns",
    "event_type", "best_bid", "best_ask", "spread", "depth_10c",
    "bids_json", "asks_json",
    "asset_id", "canonical_tick", "effective_tick",
    "remote_hash", "local_hash", "reason",
]

SCHEMA_SPOT = [
    "asset", "source", "granularity", "timestamp",
    "open", "high", "low", "close", "volume", "price", "size",
]

SCHEMA_TRADERS = [
    "asset", "wallet", "total_trades", "total_volume_usdc", "win_rate",
    "avg_trade_size", "first_trade_time", "last_trade_time",
    "favorite_side", "favorite_outcome",
]

SCHEMA_DASHBOARD = [
    "ts", "btc_price", "market_price_up", "market_price_down",
    "fair_base", "fair_enh",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: all schema tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): define output CSV schemas"
```

---

## Task 4: Ladder metric derivation helper

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

This helper is needed by both the REST orderbook writer (Task 8) and the book_feed writer (Task 9), so we build it now.

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_ladder_metrics_basic -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
def compute_ladder_metrics(bids: list[dict] | None, asks: list[dict] | None) -> dict:
    """Compute best_bid, best_ask, spread, and depth_10c from a ladder.

    Returns dict with keys: best_bid, best_ask, spread, depth_10c.
    Values may be None when the corresponding side is empty.
    depth_10c sums sizes within 10c of best_bid (bid side) plus within 10c of best_ask (ask side).
    """
    bid_pairs = [(float(b["price"]), float(b["size"])) for b in (bids or [])]
    ask_pairs = [(float(a["price"]), float(a["size"])) for a in (asks or [])]
    best_bid = max((p for p, _ in bid_pairs), default=None)
    best_ask = min((p for p, _ in ask_pairs), default=None)
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    depth_bid = sum(s for p, s in bid_pairs if best_bid is not None and (best_bid - p) <= 0.10 + 1e-9)
    depth_ask = sum(s for p, s in ask_pairs if best_ask is not None and (p - best_ask) <= 0.10 + 1e-9)
    if best_bid is None and best_ask is None:
        depth_10c = None
    else:
        depth_10c = depth_bid + depth_ask
    return {"best_bid": best_bid, "best_ask": best_ask, "spread": spread, "depth_10c": depth_10c}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: 4 new ladder tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): add ladder metric helper"
```

---

## Task 5: Markets writer (with resolutions merged + dedup)

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_write_markets_dedups_and_merges_resolutions -v`
Expected: FAIL with `ImportError: cannot import name 'write_markets'`

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
import csv
import sqlite3


def _open_db(path: Path) -> sqlite3.Connection | None:
    """Open a SQLite DB read-only. Return None if missing."""
    if not path.exists():
        print(f"  [skip] missing: {path}")
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def write_markets(db_paths: dict[str, Path], out_dir: Path) -> int:
    """Write markets.csv (markets + resolutions merged, deduped across DBs)."""
    # Pass 1: collect markets candidates per condition_id with their owning asset.
    candidates: dict[str, tuple[str, sqlite3.Row]] = {}  # condition_id -> (asset, row)
    # Pass 2: collect resolutions (any DB providing one is fine).
    resolutions: dict[str, sqlite3.Row] = {}

    for asset, db_path in db_paths.items():
        con = _open_db(db_path)
        if con is None:
            continue
        try:
            for row in con.execute("SELECT * FROM markets"):
                cid = row["condition_id"]
                existing = candidates.get(cid)
                if existing is None or _is_more_complete(row, existing[1]):
                    candidates[cid] = (asset, row)
            try:
                for row in con.execute("SELECT * FROM resolutions"):
                    resolutions[row["condition_id"]] = row
            except sqlite3.OperationalError:
                pass  # resolutions table missing in some DBs
        finally:
            con.close()

    out_path = out_dir / "markets.csv"
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_MARKETS)
        w.writeheader()
        for cid, (asset, m) in candidates.items():
            r = resolutions.get(cid)
            row_out = {
                "condition_id": cid,
                "slug": m["slug"],
                "asset": asset,
                "title": m["title"],
                "start_date": m["start_date"],
                "end_date": m["end_date"],
                "volume": m["volume"],
                "liquidity": m["liquidity"],
                "closed": m["closed"],
                "active": m["active"],
                "yes_token_id": m["yes_token_id"],
                "no_token_id": m["no_token_id"],
                "outcome_price_yes": m["outcome_price_yes"],
                "outcome_price_no": m["outcome_price_no"],
                "resolved_outcome": (r["resolved_outcome"] if r else m["resolved_outcome"]),
                "btc_price_at_start": (r["btc_price_at_start"] if r else None),
                "btc_price_at_end": (r["btc_price_at_end"] if r else None),
            }
            w.writerow(row_out)
            n += 1
    print(f"  markets.csv: {n} rows")
    return n


def _is_more_complete(new_row: sqlite3.Row, old_row: sqlite3.Row) -> bool:
    """Prefer rows where closed=1, then higher volume."""
    new_closed = (new_row["closed"] or 0) > 0
    old_closed = (old_row["closed"] or 0) > 0
    if new_closed != old_closed:
        return new_closed
    new_vol = new_row["volume"] or 0.0
    old_vol = old_row["volume"] or 0.0
    return new_vol > old_vol
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: markets test passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): write markets.csv with resolution merge"
```

---

## Task 6: Trades writer (REST + WS unified)

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_write_trades_rest_and_ws_unified -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
def write_trades(db_paths: dict[str, Path], out_dir: Path) -> int:
    """Write trades.csv (REST + WS unified across all assets)."""
    out_path = out_dir / "trades.csv"
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_TRADES)
        w.writeheader()
        for asset, db_path in db_paths.items():
            con = _open_db(db_path)
            if con is None:
                continue
            try:
                # REST trades
                try:
                    for r in con.execute("SELECT * FROM trades"):
                        w.writerow({
                            "asset": asset, "source": "rest",
                            "trade_id": r["trade_id"], "wallet": r["wallet"],
                            "condition_id": r["condition_id"], "slug": r["slug"],
                            "side": r["side"], "outcome": r["outcome"],
                            "size": r["size"], "price": r["price"],
                            "usdc_value": r["usdc_value"], "fee_rate_bps": r["fee_rate_bps"],
                            "match_time": r["match_time"],
                            "transaction_hash": r["transaction_hash"],
                            "timestamp": None,
                        })
                        n += 1
                except sqlite3.OperationalError:
                    pass
                # WS trades
                try:
                    for r in con.execute("SELECT * FROM ws_trades"):
                        w.writerow({
                            "asset": asset, "source": "ws",
                            "trade_id": None, "wallet": None,
                            "condition_id": None, "slug": r["slug"],
                            "side": r["side"], "outcome": r["outcome"],
                            "size": r["size"], "price": r["price"],
                            "usdc_value": None, "fee_rate_bps": None,
                            "match_time": None, "transaction_hash": None,
                            "timestamp": r["timestamp"],
                        })
                        n += 1
                except sqlite3.OperationalError:
                    pass
            finally:
                con.close()
    print(f"  trades.csv: {n} rows")
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: trades test passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): write trades.csv unifying REST and WS"
```

---

## Task 7: Price histories writer

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_write_price_histories_concats_with_asset -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
def write_price_histories(db_paths: dict[str, Path], out_dir: Path) -> int:
    """Write price_histories.csv (direct concat across DBs with asset col)."""
    out_path = out_dir / "price_histories.csv"
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_PRICE_HISTORIES)
        w.writeheader()
        for asset, db_path in db_paths.items():
            con = _open_db(db_path)
            if con is None:
                continue
            try:
                for r in con.execute("SELECT * FROM price_histories"):
                    w.writerow({
                        "asset": asset,
                        "condition_id": r["condition_id"],
                        "timestamp": r["timestamp"],
                        "yes_price": r["yes_price"],
                        "no_price": r["no_price"],
                    })
                    n += 1
            finally:
                con.close()
    print(f"  price_histories.csv: {n} rows")
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: price_histories test passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): write price_histories.csv"
```

---

## Task 8: REST orderbooks writer

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
    # write_orderbooks_rest opens orderbooks.csv in 'w' mode (creates).
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_write_orderbooks_rest_only -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
def write_orderbooks_rest(db_paths: dict[str, Path], out_dir: Path) -> int:
    """Open orderbooks.csv for writing and stream the REST half from SQLite.

    Leaves the file open for the book_feed walker to append to in a separate call?
    No — for simplicity, write the REST half here in 'w' mode (header + REST rows),
    then the book_feed walker opens in 'a' mode (append-only).
    """
    out_path = out_dir / "orderbooks.csv"
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_ORDERBOOKS)
        w.writeheader()
        for asset, db_path in db_paths.items():
            con = _open_db(db_path)
            if con is None:
                continue
            try:
                for r in con.execute("SELECT * FROM orderbooks"):
                    w.writerow({
                        "asset": asset,
                        "source": "rest",
                        "condition_id": r["condition_id"],
                        "slug": None,
                        "side": r["side"],
                        "snapshot_time": r["snapshot_time"],
                        "ts_ns": None,
                        "event_type": "rest_snapshot",
                        "best_bid": r["best_bid"],
                        "best_ask": r["best_ask"],
                        "spread": r["spread"],
                        "depth_10c": r["depth_10c"],
                        "bids_json": r["bids"],
                        "asks_json": r["asks"],
                        "asset_id": None,
                        "canonical_tick": None,
                        "effective_tick": None,
                        "remote_hash": None,
                        "local_hash": None,
                        "reason": None,
                    })
                    n += 1
            except sqlite3.OperationalError:
                pass
            finally:
                con.close()
    print(f"  orderbooks.csv (rest): {n} rows")
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: orderbooks rest test passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): write REST half of orderbooks.csv"
```

---

## Task 9: Spot writer (5m candles + WS ticks unified)

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
    # 2026-04-24T07:10:19 UTC == unix seconds...
    from datetime import datetime, timezone
    expected = int(datetime.fromisoformat("2026-04-24T07:10:19").replace(tzinfo=timezone.utc).timestamp())
    assert iso_ts["timestamp"] == str(expected)
    assert iso_ts["open"] == ""  # NULL for WS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_write_spot_unified -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
from datetime import datetime, timezone


def _parse_ws_timestamp(ts) -> int | str:
    """Parse a ws_spot.timestamp value to integer Unix seconds.

    Accepts:
      - integer or float (returned as int)
      - numeric strings (e.g. "1773000300") → int
      - ISO8601 strings (e.g. "2026-04-24T07:10:19") → int Unix UTC seconds
    Falls back to the original string on parse failure.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    s = str(ts)
    try:
        return int(float(s))
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return s  # give up, keep raw


def write_spot(db_paths: dict[str, Path], out_dir: Path) -> int:
    """Write spot.csv (5m candles + WS ticks unified)."""
    out_path = out_dir / "spot.csv"
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_SPOT)
        w.writeheader()
        for asset, db_path in db_paths.items():
            con = _open_db(db_path)
            if con is None:
                continue
            try:
                # 5m candles
                try:
                    for r in con.execute("SELECT * FROM spot"):
                        w.writerow({
                            "asset": asset, "source": "rest", "granularity": "5m",
                            "timestamp": r["timestamp"],
                            "open": r["open"], "high": r["high"], "low": r["low"],
                            "close": r["close"], "volume": r["volume"],
                            "price": None, "size": None,
                        })
                        n += 1
                except sqlite3.OperationalError:
                    pass
                # WS ticks
                try:
                    for r in con.execute("SELECT * FROM ws_spot"):
                        w.writerow({
                            "asset": asset, "source": "ws", "granularity": "tick",
                            "timestamp": _parse_ws_timestamp(r["timestamp"]),
                            "open": None, "high": None, "low": None,
                            "close": None, "volume": None,
                            "price": r["price"], "size": r["size"],
                        })
                        n += 1
                except sqlite3.OperationalError:
                    pass
            finally:
                con.close()
    print(f"  spot.csv: {n} rows")
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: spot test passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): write spot.csv unifying 5m candles and WS ticks"
```

---

## Task 10: Traders writer

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_write_traders -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
def write_traders(db_paths: dict[str, Path], out_dir: Path) -> int:
    """Write traders.csv (wallet aggregates, asset col preserved)."""
    out_path = out_dir / "traders.csv"
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_TRADERS)
        w.writeheader()
        for asset, db_path in db_paths.items():
            con = _open_db(db_path)
            if con is None:
                continue
            try:
                for r in con.execute("SELECT * FROM traders"):
                    w.writerow({
                        "asset": asset,
                        "wallet": r["wallet"],
                        "total_trades": r["total_trades"],
                        "total_volume_usdc": r["total_volume_usdc"],
                        "win_rate": r["win_rate"],
                        "avg_trade_size": r["avg_trade_size"],
                        "first_trade_time": r["first_trade_time"],
                        "last_trade_time": r["last_trade_time"],
                        "favorite_side": r["favorite_side"],
                        "favorite_outcome": r["favorite_outcome"],
                    })
                    n += 1
            except sqlite3.OperationalError:
                pass
            finally:
                con.close()
    print(f"  traders.csv: {n} rows")
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: traders test passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): write traders.csv"
```

---

## Task 11: Book feed walker (append WS half to orderbooks.csv)

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
    assert snap["snapshot_time"].startswith("2026-05-04T") or snap["snapshot_time"].startswith("2026-04-")  # ts_ns is approximate
    # Bids/asks JSON preserved
    assert json.loads(snap["bids_json"]) == [{"price": "0.50", "size": "100"}]
    # Derived metrics
    assert snap["best_bid"] == "0.5"
    assert snap["best_ask"] == "0.51"

    book = next(r for r in rows if r["event_type"] == "book")
    assert json.loads(book["bids_json"]) == [{"price": "0.49", "size": "5"}]
    assert book["best_bid"] == "0.49"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_append_book_feed_to_orderbooks -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
import gzip
import json


def append_book_feed_to_orderbooks(book_feed_root: Path, out_dir: Path) -> int:
    """Append the WS half of orderbooks.csv from book_feed/<date>/*.jsonl.gz files.

    Expects orderbooks.csv to already exist (created by write_orderbooks_rest).
    """
    out_path = out_dir / "orderbooks.csv"
    if not book_feed_root.exists():
        print(f"  [skip] missing: {book_feed_root}")
        return 0

    n = 0
    with out_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_ORDERBOOKS)
        for date_dir in sorted(book_feed_root.iterdir()):
            if not date_dir.is_dir():
                continue
            if date_dir.name == "logs":
                continue
            for gz_file in sorted(date_dir.glob("*.jsonl.gz")):
                try:
                    with gzip.open(gz_file, "rt") as gz:
                        for line_no, raw_line in enumerate(gz, start=1):
                            line = raw_line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except json.JSONDecodeError as e:
                                print(f"  [warn] {gz_file}:{line_no} bad JSON: {e}")
                                continue
                            row = _book_feed_row(rec)
                            if row is None:
                                continue
                            w.writerow(row)
                            n += 1
                except OSError as e:
                    print(f"  [warn] failed to read {gz_file}: {e}")
    print(f"  orderbooks.csv (ws_book_feed): {n} rows appended")
    return n


def _book_feed_row(rec: dict) -> dict | None:
    """Convert one parsed book_feed record into a SCHEMA_ORDERBOOKS row.

    Returns None to skip the record.
    """
    rec_type = rec.get("type")
    if rec_type == "snapshot":
        bids = rec.get("bids") or []
        asks = rec.get("asks") or []
    elif rec_type == "book":
        raw = rec.get("raw") or {}
        bids = raw.get("bids") or []
        asks = raw.get("asks") or []
    else:
        # Other types (price_change, last_trade_price, tick_size_change)
        # don't carry full ladders; skip.
        return None

    metrics = compute_ladder_metrics(bids, asks)
    ts_ns = rec.get("ts_ns")
    snapshot_time = None
    if ts_ns is not None:
        try:
            dt = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
            snapshot_time = dt.isoformat()
        except (OSError, OverflowError, ValueError):
            snapshot_time = None
    slug = rec.get("slug") or ""
    asset = slug_to_asset(slug)
    return {
        "asset": asset,
        "source": "ws_book_feed",
        "condition_id": rec.get("condition_id"),
        "slug": slug,
        "side": rec.get("side"),
        "snapshot_time": snapshot_time,
        "ts_ns": ts_ns,
        "event_type": rec_type,
        "best_bid": metrics["best_bid"],
        "best_ask": metrics["best_ask"],
        "spread": metrics["spread"],
        "depth_10c": metrics["depth_10c"],
        "bids_json": json.dumps(bids),
        "asks_json": json.dumps(asks),
        "asset_id": rec.get("asset_id"),
        "canonical_tick": rec.get("canonical_tick"),
        "effective_tick": rec.get("effective_tick"),
        "remote_hash": rec.get("remote_hash"),
        "local_hash": rec.get("local_hash"),
        "reason": rec.get("reason"),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: book feed test passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): append book_feed WS half to orderbooks.csv"
```

---

## Task 12: Daemon events writer (two-pass key union)

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_write_daemon_events_two_pass -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
def write_daemon_events(events_path: Path, out_dir: Path) -> int:
    """Write daemon_events.csv from events.jsonl with a two-pass key union."""
    out_path = out_dir / "daemon_events.csv"
    if not events_path.exists():
        print(f"  [skip] missing: {events_path}")
        # still emit an empty file with no columns? No — leave file absent.
        return 0

    # Pass 1: collect key union
    keys: list[str] = []
    seen: set[str] = set()
    # Always lead with ts, type
    for k in ("ts", "type"):
        keys.append(k)
        seen.add(k)
    with events_path.open() as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  [warn] events.jsonl:{line_no} bad JSON: {e}")
                continue
            for k in rec:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)

    # Pass 2: write
    n = 0
    with out_path.open("w", newline="") as f_out, events_path.open() as f_in:
        w = csv.DictWriter(f_out, fieldnames=keys)
        w.writeheader()
        for line_no, raw in enumerate(f_in, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = {}
            for k in keys:
                v = rec.get(k)
                if isinstance(v, (dict, list)):
                    row[k] = json.dumps(v)
                else:
                    row[k] = v
            w.writerow(row)
            n += 1
    print(f"  daemon_events.csv: {n} rows ({len(keys)} columns)")
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: both daemon_events tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): write daemon_events.csv with key-union schema"
```

---

## Task 13: Dashboard history writer

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_write_dashboard_history -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/consolidate_data.py`:

```python
def write_dashboard_history(src_path: Path, out_dir: Path) -> int:
    """Write dashboard_history.csv from dashboard_history.jsonl."""
    out_path = out_dir / "dashboard_history.csv"
    if not src_path.exists():
        print(f"  [skip] missing: {src_path}")
        return 0
    n = 0
    with out_path.open("w", newline="") as f_out, src_path.open() as f_in:
        w = csv.DictWriter(f_out, fieldnames=SCHEMA_DASHBOARD)
        w.writeheader()
        for line_no, raw in enumerate(f_in, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  [warn] dashboard_history.jsonl:{line_no} bad JSON: {e}")
                continue
            w.writerow({k: rec.get(k) for k in SCHEMA_DASHBOARD})
            n += 1
    print(f"  dashboard_history.csv: {n} rows")
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: dashboard test passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): write dashboard_history.csv"
```

---

## Task 14: Main orchestrator + summary printout

**Files:**
- Modify: `scripts/consolidate_data.py`
- Modify: `tests/scripts/test_consolidate_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/scripts/test_consolidate_data.py`:

```python
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

    # All 8 CSVs exist.
    for name in [
        "markets.csv", "trades.csv", "price_histories.csv",
        "orderbooks.csv", "spot.csv", "traders.csv",
        "daemon_events.csv", "dashboard_history.csv",
    ]:
        assert (out_dir / name).exists(), f"missing {name}"
    # Spot-check row counts
    assert len(list(csv.DictReader((out_dir / "trades.csv").open()))) == 2  # 1 rest + 1 ws
    assert len(list(csv.DictReader((out_dir / "orderbooks.csv").open()))) == 2  # 1 rest + 1 ws
    # Summary printed
    captured = capsys.readouterr().out
    assert "Consolidation summary" in captured


def test_main_idempotent_rebuild(tmp_path, monkeypatch):
    """Running main() twice should produce identical output."""
    from scripts import consolidate_data as cd

    # Use empty fixtures (no DBs, no daemon_state) — main() should still produce
    # CSV files (header-only or absent for missing daemon files).
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/scripts/test_consolidate_data.py::test_main_end_to_end -v`
Expected: FAIL — `main()` doesn't take an argv list yet, doesn't call any of the writers, doesn't print summary.

- [ ] **Step 3: Write the implementation**

Replace the existing `main()` in `scripts/consolidate_data.py`:

```python
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Truncate any pre-existing 8 CSVs (idempotent rebuild).
    for name in [
        "markets.csv", "trades.csv", "price_histories.csv",
        "orderbooks.csv", "spot.csv", "traders.csv",
        "daemon_events.csv", "dashboard_history.csv",
    ]:
        p = out_dir / name
        if p.exists():
            p.unlink()

    # Resolve current paths (allows monkeypatching in tests).
    db_paths = {asset: DATA_DIR / f"{asset}5m.db" for asset in ASSETS}
    daemon_dir = DAEMON_STATE_DIR

    print(f"Output directory: {out_dir}")
    counts: dict[str, int] = {}

    print("Writing markets.csv ...")
    counts["markets.csv"] = write_markets(db_paths, out_dir)

    print("Writing trades.csv ...")
    counts["trades.csv"] = write_trades(db_paths, out_dir)

    print("Writing price_histories.csv ...")
    counts["price_histories.csv"] = write_price_histories(db_paths, out_dir)

    print("Writing orderbooks.csv (REST half) ...")
    rest_n = write_orderbooks_rest(db_paths, out_dir)
    print("Appending orderbooks.csv (book_feed) ...")
    ws_n = append_book_feed_to_orderbooks(daemon_dir / "book_feed", out_dir)
    counts["orderbooks.csv"] = rest_n + ws_n

    print("Writing spot.csv ...")
    counts["spot.csv"] = write_spot(db_paths, out_dir)

    print("Writing traders.csv ...")
    counts["traders.csv"] = write_traders(db_paths, out_dir)

    print("Writing daemon_events.csv ...")
    counts["daemon_events.csv"] = write_daemon_events(daemon_dir / "events.jsonl", out_dir)

    print("Writing dashboard_history.csv ...")
    counts["dashboard_history.csv"] = write_dashboard_history(
        daemon_dir / "dashboard_history.jsonl", out_dir
    )

    # Summary
    print("\n=== Consolidation summary ===")
    total_rows = 0
    total_size = 0
    for name in [
        "markets.csv", "trades.csv", "price_histories.csv",
        "orderbooks.csv", "spot.csv", "traders.csv",
        "daemon_events.csv", "dashboard_history.csv",
    ]:
        p = out_dir / name
        size = p.stat().st_size if p.exists() else 0
        rows = counts.get(name, 0)
        total_rows += rows
        total_size += size
        print(f"  {name:<26} {rows:>12} rows  {_human_bytes(size):>10}")
    print(f"  {'Total':<26} {total_rows:>12} rows  {_human_bytes(total_size):>10}")

    return 0


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
```

(The `db_paths` and `daemon_dir` resolution above relies on the module-level `DATA_DIR` and `DAEMON_STATE_DIR` which `monkeypatch` replaces in the test. To make this work, change the body of `main()` to read from the module-level names directly so monkeypatching takes effect at call time:)

```python
    # ... inside main(), use module globals so monkeypatching takes effect
    db_paths = {asset: DATA_DIR / f"{asset}5m.db" for asset in ASSETS}
    daemon_dir = DAEMON_STATE_DIR
```

(This is already what the snippet shows. Just confirm in practice.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/scripts/test_consolidate_data.py -v`
Expected: ALL tests pass (15+ tests total).

Then run the script smoke-test against an empty out dir:
```bash
mkdir -p /tmp/_consol_smoke
python scripts/consolidate_data.py --out-dir /tmp/_consol_smoke
ls -la /tmp/_consol_smoke
```
Expected: 8 CSV files appear (sizes will reflect real data — see Task 15).

- [ ] **Step 5: Commit**

```bash
git add scripts/consolidate_data.py tests/scripts/test_consolidate_data.py
git commit -m "feat(consolidate): wire main() orchestrator with summary"
```

---

## Task 15: Run on real data + manual validation

**Files:** None modified — this is a manual verification task.

- [ ] **Step 1: Run the script against production data**

```bash
cd /home/samsam/polymarket-hustle
time python scripts/consolidate_data.py
```

Expected: completes without errors. Print summary should show:
- `markets.csv` ~95K rows (sum of unique condition_ids across DBs)
- `trades.csv` ~52M rows
- `price_histories.csv` ~4.2M rows
- `orderbooks.csv` ~525K rest rows + several million from book_feed
- `spot.csv` ~838K rows (5m candles + WS ticks)
- `traders.csv` ~500 rows
- `daemon_events.csv` ~10K rows
- `dashboard_history.csv` ~5K rows

- [ ] **Step 2: Sanity-check row counts against source counts**

```bash
sqlite3 data/btc5m.db "SELECT 'btc.trades', COUNT(*) FROM trades UNION ALL SELECT 'btc.ws_trades', COUNT(*) FROM ws_trades;"
sqlite3 data/doge5m.db "SELECT 'doge.trades', COUNT(*) FROM trades UNION ALL SELECT 'doge.ws_trades', COUNT(*) FROM ws_trades;"
sqlite3 data/eth5m.db "SELECT 'eth.trades', COUNT(*) FROM trades UNION ALL SELECT 'eth.ws_trades', COUNT(*) FROM ws_trades;"
sqlite3 data/sol5m.db "SELECT 'sol.trades', COUNT(*) FROM trades UNION ALL SELECT 'sol.ws_trades', COUNT(*) FROM ws_trades;"
sqlite3 data/xrp5m.db "SELECT 'xrp.trades', COUNT(*) FROM trades UNION ALL SELECT 'xrp.ws_trades', COUNT(*) FROM ws_trades;"
```

Sum the right-hand column. Then:
```bash
wc -l data/consolidated/trades.csv
```

Expected: `wc -l` count = sum + 1 (the +1 is the header).

If they don't match, investigate the diff.

- [ ] **Step 3: Sanity-check trades source split**

```bash
# Counts per asset/source
python -c "
import csv
from collections import Counter
c = Counter()
with open('data/consolidated/trades.csv') as f:
    for row in csv.DictReader(f):
        c[(row['asset'], row['source'])] += 1
for k, v in sorted(c.items()): print(k, v)
"
```

Expected: matches the per-DB sums from Step 2.

- [ ] **Step 4: Sanity-check orderbook ladder integrity**

```bash
python -c "
import csv, json
n = 0
bad = 0
with open('data/consolidated/orderbooks.csv') as f:
    for row in csv.DictReader(f):
        if row['source'] != 'ws_book_feed': continue
        n += 1
        if n > 10000: break
        try:
            json.loads(row['bids_json'])
            json.loads(row['asks_json'])
        except Exception as e:
            bad += 1
print(f'{n} rows checked, {bad} JSON-parse failures')
"
```

Expected: `0 JSON-parse failures`.

- [ ] **Step 5: Document the results**

Append a short results note to the spec doc summarizing:
- Final row counts per CSV
- Total disk size
- Wall-clock time

```bash
$EDITOR docs/superpowers/specs/2026-05-05-data-consolidation-design.md
```

Then commit:
```bash
git add docs/superpowers/specs/2026-05-05-data-consolidation-design.md
git commit -m "docs(spec): record consolidation run results"
```

---

## Self-review notes

This plan covers every requirement of the spec:
- 8 CSVs ✓ (Tasks 5, 6, 7, 8, 9, 10, 11, 12, 13)
- Asset column on per-asset tables ✓ (every writer task)
- Source column for REST/WS unification ✓ (Tasks 6, 8, 9, 11)
- Markets+resolutions merge with dedup ✓ (Task 5)
- Bids/asks as JSON strings ✓ (Tasks 8, 11)
- Ladder metric derivation for book_feed ✓ (Task 4)
- Two-pass key union for daemon_events ✓ (Task 12)
- Idempotent rebuild ✓ (Task 14)
- Skipped sources documented in spec, not in code ✓
- Plain `.csv`, output to `data/consolidated/`, no compression ✓
- Stdlib-only, streaming for large tables ✓
