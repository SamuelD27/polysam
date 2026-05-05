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


def slug_to_asset(slug: str | None) -> str | None:
    """Map a market slug to its underlying asset, or None if unknown."""
    if not slug:
        return None
    s = slug.lower()
    for asset in ASSETS:
        if s.startswith(f"{asset}-"):
            return asset
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
