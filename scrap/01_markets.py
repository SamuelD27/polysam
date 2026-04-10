#!/usr/bin/env python3
"""
scrap/01_markets.py -- Market catalog scraper for a single crypto asset.

Collects all market metadata from the Gamma API for the given asset's
5-minute up/down markets.  Designed to run as one of 5 parallel processes
(one per asset) coordinated by scrape_all.sh.

Usage:
    python scrap/01_markets.py --asset btc
    python scrap/01_markets.py --asset eth
"""

import json
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrap.config import (
    ASSETS,
    GAMMA_EVENTS_URL,
    init_db,
    init_logging,
    is_shutdown,
    limiter_gamma_events,
    make_session,
    resilient_get,
    get_progress,
    set_progress,
    report_progress,
    parse_asset_arg,
)

# ---------------------------------------------------------------------------
# Market parsing
# ---------------------------------------------------------------------------

DATASET = "markets"


def _parse_event_markets(event: dict, slug_prefix: str) -> list[tuple]:
    """Parse markets from a Gamma event response into DB tuples.

    Filters by the event-level slug matching the asset's slug_prefix.
    """
    batch = []
    event_slug = event.get("slug", "")
    if not event_slug.startswith(slug_prefix):
        return batch

    markets_list = event.get("markets", [])
    for m in markets_list:
        cid = m.get("conditionId", m.get("condition_id", ""))
        if not cid:
            continue

        # clobTokenIds can be a JSON string or a list
        clob_tokens = m.get("clobTokenIds", "[]")
        if isinstance(clob_tokens, str):
            try:
                clob_tokens = json.loads(clob_tokens)
            except json.JSONDecodeError:
                clob_tokens = []

        yes_token = clob_tokens[0] if len(clob_tokens) > 0 else ""
        no_token = clob_tokens[1] if len(clob_tokens) > 1 else ""

        # outcomePrices can be a JSON string or a list
        outcome_prices = m.get("outcomePrices", "[]")
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except json.JSONDecodeError:
                outcome_prices = []

        price_yes = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0.0
        price_no = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0.0

        # Determine resolved outcome
        resolved = ""
        if m.get("closed", False):
            if price_yes > 0.9:
                resolved = "Up"
            elif price_no > 0.9:
                resolved = "Down"

        batch.append((
            cid,
            event_slug,
            m.get("title", m.get("question", "")),
            m.get("startDate", m.get("start_date", "")),
            m.get("endDate", m.get("end_date", "")),
            float(m.get("volume", 0) or 0),
            float(m.get("liquidity", 0) or 0),
            1 if m.get("closed", False) else 0,
            1 if m.get("active", False) else 0,
            yes_token,
            no_token,
            price_yes,
            price_no,
            resolved,
        ))
    return batch


# ---------------------------------------------------------------------------
# Main collection loop
# ---------------------------------------------------------------------------


def collect_markets(asset: str):
    """Paginate through Gamma events API and collect all markets for asset."""
    logger = init_logging(asset)
    conn = init_db(asset)
    session = make_session()

    slug_prefix = ASSETS[asset]["slug_prefix"]

    logger.info("=" * 60)
    logger.info("[markets] Starting market catalog collection for %s", asset)
    logger.info("[markets] Filtering by slug prefix: %s", slug_prefix)
    logger.info("=" * 60)

    progress = get_progress(conn, DATASET)
    offset = progress["last_offset"]
    total_inserted = progress["items_collected"]
    consecutive_empty = 0

    while not is_shutdown():
        resp = resilient_get(
            session,
            GAMMA_EVENTS_URL,
            {
                "tag_slug": "5M",
                "limit": 100,
                "offset": offset,
                "order": "endDate",
                "ascending": "false",
            },
            limiter_gamma_events,
            logger=logger,
        )

        if resp is None:
            break

        events = resp.json()
        if not events:
            logger.info("[markets] No more events at offset %d", offset)
            break

        batch = []
        for event in events:
            batch.extend(_parse_event_markets(event, slug_prefix))

        if batch:
            conn.executemany("""
                INSERT OR REPLACE INTO markets
                (condition_id, slug, title, start_date, end_date, volume, liquidity,
                 closed, active, yes_token_id, no_token_id,
                 outcome_price_yes, outcome_price_no, resolved_outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, batch)
            conn.commit()
            total_inserted += len(batch)
            consecutive_empty = 0
        else:
            consecutive_empty += 1

        offset += 100
        set_progress(conn, DATASET, offset=offset, items=total_inserted)

        if total_inserted % 500 == 0 or len(events) < 100:
            logger.info(
                "[markets] %d %s 5m markets collected (offset %d, page had %d events)",
                total_inserted, asset.upper(), offset, len(events),
            )
        report_progress(asset, DATASET, done=total_inserted, total=0,
                        msg=f"offset={offset}")

        # Stop conditions
        if len(events) < 100:
            break
        if consecutive_empty >= 20:
            logger.info(
                "[markets] 20 consecutive pages with no %s 5m markets -- stopping",
                asset.upper(),
            )
            break

    set_progress(conn, DATASET, offset=offset, items=total_inserted)
    report_progress(asset, DATASET, done=total_inserted, total=total_inserted,
                    msg="done")
    logger.info("[markets] Done -- %d total %s 5m markets", total_inserted, asset.upper())
    conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asset = parse_asset_arg()
    collect_markets(asset)
