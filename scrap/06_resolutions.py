#!/usr/bin/env python3
"""
scrap/06_resolutions.py -- Compute resolution data by joining market metadata
with spot prices.

This is a compute-only step (no API calls).  Depends on BOTH 01_markets.py
AND 02_spot_prices.py having run first.

For each closed market:
  - Extract t_zero from the slug (authoritative start time)
  - Look up spot prices at t_zero and t_zero + 300
  - Determine resolved outcome from outcome prices
  - Write to the resolutions table

Usage:
    python scrap/06_resolutions.py --asset btc
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scrap.config import (
    init_db,
    init_logging,
    is_shutdown,
    report_progress,
    set_progress,
    parse_asset_arg,
)

DATASET = "resolutions"


def compute_resolutions(asset: str) -> None:
    logger = init_logging(asset)
    conn = init_db(asset)

    logger.info("=" * 60)
    logger.info("[resolutions] Computing resolution data for %s", asset.upper())
    logger.info("=" * 60)

    # Fetch all closed markets
    markets = conn.execute("""
        SELECT condition_id, slug, start_date, end_date, volume,
               outcome_price_yes, outcome_price_no, resolved_outcome
        FROM markets WHERE closed = 1
    """).fetchall()

    total = len(markets)
    logger.info("[resolutions] Found %d closed markets", total)

    if total == 0:
        report_progress(asset, DATASET, 0, 0, "no closed markets")
        conn.close()
        return

    batch = []
    skipped = 0

    for i, (cid, slug, start_date, end_date, vol, py, pn, resolved) in enumerate(markets):
        if is_shutdown():
            logger.info("[resolutions] Shutdown requested -- stopping")
            break

        # Extract t_zero from slug -- this is the authoritative start time
        try:
            t_zero = int(slug.split("-")[-1])
        except (ValueError, IndexError):
            logger.warning("[resolutions] Cannot parse t_zero from slug: %s", slug)
            skipped += 1
            continue

        t_end = t_zero + 300  # 5 minutes

        # Determine resolved outcome from outcome prices
        if py > 0.9:
            outcome = "Up"
        elif pn > 0.9:
            outcome = "Down"
        else:
            outcome = resolved  # fall back to markets table value

        # Look up spot price at start
        row = conn.execute(
            "SELECT close FROM spot WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
            (t_zero,),
        ).fetchone()
        spot_start = row[0] if row else None

        # Look up spot price at end
        row = conn.execute(
            "SELECT close FROM spot WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
            (t_end,),
        ).fetchone()
        spot_end = row[0] if row else None

        batch.append((
            cid, slug, start_date, end_date, outcome,
            py, pn, vol, spot_start, spot_end,
        ))

        # Progress every 500 markets
        if (i + 1) % 500 == 0:
            logger.info(
                "[resolutions] Processed %d / %d markets", i + 1, total,
            )
            report_progress(asset, DATASET, i + 1, total)

    # Write all at once
    if batch:
        conn.execute("DELETE FROM resolutions")
        conn.executemany("""
            INSERT OR REPLACE INTO resolutions
            (condition_id, slug, start_date, end_date, resolved_outcome,
             outcome_price_yes, outcome_price_no, volume,
             btc_price_at_start, btc_price_at_end)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, batch)
        conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
    set_progress(conn, DATASET, items=count)
    report_progress(asset, DATASET, total, total, f"done -- {count} resolved")

    logger.info(
        "[resolutions] Done -- %d resolved markets (%d skipped)",
        count, skipped,
    )
    conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asset = parse_asset_arg()
    compute_resolutions(asset)
