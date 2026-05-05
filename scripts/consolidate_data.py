"""Consolidate scraped data and daemon-run output into 8 standardized CSVs.

Spec: docs/superpowers/specs/2026-05-05-data-consolidation-design.md

Usage: python scripts/consolidate_data.py [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DAEMON_STATE_DIR = REPO_ROOT / "daemon_state"
DEFAULT_OUT_DIR = DATA_DIR / "consolidated"

ASSETS = ("btc", "doge", "eth", "sol", "xrp")
DB_PATHS = {asset: DATA_DIR / f"{asset}5m.db" for asset in ASSETS}

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
    depth_bid = sum(s for p, s in bid_pairs if best_bid is not None and (best_bid - p) < 0.10 - 1e-9)
    depth_ask = sum(s for p, s in ask_pairs if best_ask is not None and (p - best_ask) < 0.10 - 1e-9)
    if best_bid is None and best_ask is None:
        depth_10c = None
    else:
        depth_10c = depth_bid + depth_ask
    return {"best_bid": best_bid, "best_ask": best_ask, "spread": spread, "depth_10c": depth_10c}


def slug_to_asset(slug: str | None) -> str | None:
    """Map a market slug to its underlying asset, or None if unknown."""
    if not slug:
        return None
    s = slug.lower()
    for asset in ASSETS:
        if s.startswith(f"{asset}-"):
            return asset
    return None


def _open_db(path: Path) -> sqlite3.Connection | None:
    """Open a SQLite DB read-only. Return None if missing."""
    if not path.exists():
        print(f"  [skip] missing: {path}")
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _is_more_complete(new_row: sqlite3.Row, old_row: sqlite3.Row) -> bool:
    """Prefer rows where closed=1, then higher volume."""
    new_closed = (new_row["closed"] or 0) > 0
    old_closed = (old_row["closed"] or 0) > 0
    if new_closed != old_closed:
        return new_closed
    new_vol = new_row["volume"] or 0.0
    old_vol = old_row["volume"] or 0.0
    return new_vol > old_vol


def write_markets(db_paths: dict[str, Path], out_dir: Path) -> int:
    """Write markets.csv (markets + resolutions merged, deduped across DBs)."""
    candidates: dict[str, tuple[str, sqlite3.Row]] = {}  # condition_id -> (asset, row)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
