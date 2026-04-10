#!/usr/bin/env python3
"""
scrap/04_price_histories.py -- Collect CLOB price histories for all closed
markets of a given asset.

Fetches price-history timeseries from the Polymarket CLOB API for each
closed market's YES token.  Skips markets already present in the
price_histories table for resumability.

Depends on 01_markets.py having run first (needs yes_token_id from the
markets table).

Usage:
    python scrap/04_price_histories.py --asset btc
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scrap.config import (
    CLOB_PRICES_URL,
    init_db,
    init_logging,
    is_shutdown,
    limiter_clob_prices,
    make_session,
    resilient_get,
    get_progress,
    set_progress,
    report_progress,
    parse_asset_arg,
)


def collect_price_histories(asset: str) -> None:
    logger = init_logging(asset)
    logger.info("=" * 60)
    logger.info("[prices] Starting price history collection for asset=%s", asset)
    logger.info("=" * 60)

    conn = init_db(asset)
    session = make_session()

    # ------------------------------------------------------------------
    # 1. Load closed markets with yes_token_id
    # ------------------------------------------------------------------
    markets = conn.execute(
        "SELECT condition_id, yes_token_id FROM markets "
        "WHERE closed=1 AND yes_token_id != '' ORDER BY start_date"
    ).fetchall()

    if not markets:
        logger.error("[prices] No closed markets with token IDs -- run 01_markets.py first")
        report_progress(asset, "prices", 0, 0, "ERROR: no closed markets in DB")
        conn.close()
        sys.exit(1)

    total_markets = len(markets)
    logger.info("[prices] %d closed markets to fetch", total_markets)

    # ------------------------------------------------------------------
    # 2. Resumability: skip condition_ids already in price_histories
    # ------------------------------------------------------------------
    progress = get_progress(conn, "prices")
    done_cids: set[str] = set()
    if progress["items_collected"] > 0:
        rows = conn.execute(
            "SELECT DISTINCT condition_id FROM price_histories"
        ).fetchall()
        done_cids = {r[0] for r in rows}
        logger.info("[prices] Resuming -- %d markets already done", len(done_cids))

    processed = len(done_cids)
    total_points = progress["items_collected"]
    checkpoint_counter = 0

    # ------------------------------------------------------------------
    # 3. Fetch price history for each market's YES token
    # ------------------------------------------------------------------
    for cid, yes_token in markets:
        if is_shutdown():
            logger.info("[prices] Shutdown requested -- stopping gracefully")
            break
        if cid in done_cids:
            continue

        # CLOB API uses token_id, not condition_id
        resp = resilient_get(
            session,
            CLOB_PRICES_URL,
            {"market": yes_token, "interval": "max", "fidelity": 1},
            limiter_clob_prices,
            logger=logger,
        )

        if resp is None:
            processed += 1
            checkpoint_counter += 1
            continue

        try:
            data = resp.json()
        except Exception:
            logger.error("[prices] JSON decode error for %s", cid[:16])
            processed += 1
            checkpoint_counter += 1
            continue

        if not data or not isinstance(data, dict):
            processed += 1
            checkpoint_counter += 1
            continue

        history = data.get("history", [])
        if not isinstance(history, list):
            history = []

        # Response format: {"history": [{"t": epoch_seconds, "p": yes_price}, ...]}
        batch = []
        for point in history:
            ts = point.get("t", 0)
            price = float(point.get("p", 0))
            batch.append((cid, int(ts), price, round(1.0 - price, 6)))

        if batch:
            conn.executemany(
                "INSERT INTO price_histories "
                "(condition_id, timestamp, yes_price, no_price) "
                "VALUES (?,?,?,?)",
                batch,
            )
            conn.commit()
            total_points += len(batch)

        processed += 1
        checkpoint_counter += 1

        # Log progress every 100 markets
        if processed % 100 == 0:
            logger.info(
                "[prices] %d/%d markets (%.1f%%) | %d data points",
                processed,
                total_markets,
                100 * processed / total_markets,
                total_points,
            )
            report_progress(
                asset,
                "prices",
                processed,
                total_markets,
                f"{total_points} data points",
            )

        # Checkpoint every 1000 markets
        if checkpoint_counter >= 1000:
            set_progress(conn, "prices", condition_id=cid, items=total_points)
            checkpoint_counter = 0
            logger.info(
                "[prices] Checkpoint at %d markets, %d data points",
                processed,
                total_points,
            )

    # ------------------------------------------------------------------
    # 4. Final progress save
    # ------------------------------------------------------------------
    last_cid = cid if markets else ""
    set_progress(conn, "prices", condition_id=last_cid, items=total_points)

    logger.info(
        "[prices] Done -- %d/%d markets, %d data points total",
        processed,
        total_markets,
        total_points,
    )
    report_progress(
        asset,
        "prices",
        processed,
        total_markets,
        f"DONE: {total_points} data points",
    )

    conn.close()


def main():
    asset = parse_asset_arg()
    collect_price_histories(asset)


if __name__ == "__main__":
    main()
