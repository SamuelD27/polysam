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
