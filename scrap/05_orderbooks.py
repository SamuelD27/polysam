#!/usr/bin/env python3
"""
scrap/05_orderbooks.py -- Capture orderbook snapshots for active markets
of a given asset.

Only captures current-moment snapshots (no historical orderbook data
exists on Polymarket).  For each active market, fetches the orderbook
for both yes and no token sides.

Depends on 01_markets.py having run first (needs token_ids from the
markets table).

Usage:
    python scrap/05_orderbooks.py --asset btc
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scrap.config import (
    CLOB_BOOK_URL,
    init_db,
    init_logging,
    is_shutdown,
    limiter_clob_market,
    make_session,
    get_progress,
    set_progress,
    report_progress,
    parse_asset_arg,
)


def collect_orderbooks(asset: str) -> None:
    logger = init_logging(asset)
    logger.info("=" * 60)
    logger.info("[orderbooks] Starting orderbook snapshots for asset=%s", asset)
    logger.info("=" * 60)

    conn = init_db(asset)
    session = make_session()

    # ------------------------------------------------------------------
    # 1. Get active markets
    # ------------------------------------------------------------------
    markets = conn.execute(
        "SELECT condition_id, yes_token_id, no_token_id "
        "FROM markets WHERE closed=0 AND active=1"
    ).fetchall()

    if not markets:
        logger.info("[orderbooks] No active markets found")
        report_progress(asset, "orderbooks", 0, 0, "No active markets")
        conn.close()
        return

    total_markets = len(markets)
    logger.info("[orderbooks] %d active markets", total_markets)
    snapshot_time = datetime.now(timezone.utc).isoformat()
    processed = 0

    # ------------------------------------------------------------------
    # 2. For each market, fetch orderbook for yes and no sides
    # ------------------------------------------------------------------
    for cid, yes_token, no_token in markets:
        if is_shutdown():
            logger.info("[orderbooks] Shutdown requested -- stopping gracefully")
            break

        for side_label, token_id in [("yes", yes_token), ("no", no_token)]:
            if not token_id:
                continue

            # Many 5m markets return 404 ("No orderbook exists") -- skip gracefully
            limiter_clob_market.wait()
            try:
                resp = session.get(
                    CLOB_BOOK_URL,
                    params={"token_id": token_id},
                    timeout=30,
                )
            except Exception as e:
                logger.warning(
                    "[orderbooks] Request error for %s %s: %s",
                    cid[:16], side_label, e,
                )
                continue

            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                continue

            try:
                book = resp.json()
            except Exception:
                logger.error(
                    "[orderbooks] JSON error for %s %s", cid[:16], side_label
                )
                continue

            if "error" in book:
                continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 0.0
            spread = (
                round(best_ask - best_bid, 6)
                if (best_bid and best_ask)
                else 0.0
            )

            # Depth within 10 cents of best price
            depth_bid = sum(
                float(b.get("size", 0))
                for b in bids
                if best_bid - float(b["price"]) <= 0.10
            )
            depth_ask = sum(
                float(a.get("size", 0))
                for a in asks
                if float(a["price"]) - best_ask <= 0.10
            )
            depth_10c = round(depth_bid + depth_ask, 2)

            conn.execute(
                """
                INSERT INTO orderbooks
                (condition_id, side, bids, asks, best_bid, best_ask,
                 spread, depth_10c, snapshot_time)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    cid,
                    side_label,
                    json.dumps(bids),
                    json.dumps(asks),
                    best_bid,
                    best_ask,
                    spread,
                    depth_10c,
                    snapshot_time,
                ),
            )

        processed += 1

        # Commit every 100 markets
        if processed % 100 == 0:
            conn.commit()
            logger.info(
                "[orderbooks] %d/%d markets (%.1f%%)",
                processed, total_markets,
                100 * processed / total_markets,
            )
            report_progress(
                asset,
                "orderbooks",
                processed,
                total_markets,
                f"{processed} markets snapshotted",
            )

    # ------------------------------------------------------------------
    # 3. Final commit and progress save
    # ------------------------------------------------------------------
    conn.commit()
    set_progress(conn, "orderbooks", items=processed)

    logger.info(
        "[orderbooks] Done -- %d/%d markets snapshotted",
        processed, total_markets,
    )
    report_progress(
        asset,
        "orderbooks",
        processed,
        total_markets,
        f"DONE: {processed} markets snapshotted",
    )

    conn.close()


def main():
    asset = parse_asset_arg()
    collect_orderbooks(asset)


if __name__ == "__main__":
    main()
