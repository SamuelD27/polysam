#!/usr/bin/env python3
"""
scrap/03_trades.py -- Collect all trades from the Polymarket Data API
for every market of a given asset.

Depends on 01_markets.py having run first (needs condition_ids from the
markets table).

Usage:
    python scrap/03_trades.py --asset btc
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scrap.config import (
    ASSETS,
    DATA_TRADES_URL,
    init_db,
    init_logging,
    is_shutdown,
    limiter_data_trades,
    make_session,
    resilient_get,
    get_progress,
    set_progress,
    report_progress,
    parse_asset_arg,
)


def collect_trades(asset: str) -> None:
    logger = init_logging(asset)
    logger.info("=" * 60)
    logger.info("[trades] Starting trade collection for asset=%s", asset)
    logger.info("=" * 60)

    conn = init_db(asset)
    session = make_session()

    # ------------------------------------------------------------------
    # 1. Load all markets (must have been scraped already)
    # ------------------------------------------------------------------
    markets = conn.execute(
        "SELECT condition_id, slug FROM markets ORDER BY start_date"
    ).fetchall()

    if not markets:
        logger.error("[trades] No markets in DB -- run 01_markets.py first")
        report_progress(asset, "trades", 0, 0, "ERROR: no markets in DB")
        conn.close()
        sys.exit(1)

    total_markets = len(markets)
    logger.info("[trades] %d markets to process", total_markets)

    # ------------------------------------------------------------------
    # 2. Resumability: skip condition_ids that already have trades
    # ------------------------------------------------------------------
    progress = get_progress(conn, "trades")
    done_cids: set[str] = set()
    if progress["last_condition_id"]:
        rows = conn.execute("SELECT DISTINCT condition_id FROM trades").fetchall()
        done_cids = {r[0] for r in rows}
        logger.info(
            "[trades] Resuming -- %d markets already have trades", len(done_cids)
        )

    markets_processed = len(done_cids)
    total_trades = progress["items_collected"]
    checkpoint_counter = 0

    # ------------------------------------------------------------------
    # 3. Iterate over every market and paginate its trades
    # ------------------------------------------------------------------
    for cid, slug in markets:
        if is_shutdown():
            logger.info("[trades] Shutdown requested -- stopping gracefully")
            break
        if cid in done_cids:
            continue

        try:
            market_trades = []
            trade_offset = 0

            while not is_shutdown():
                resp = resilient_get(
                    session,
                    DATA_TRADES_URL,
                    {"market": cid, "limit": 500, "offset": trade_offset},
                    limiter_data_trades,
                    logger=logger,
                )

                if resp is None:
                    break

                try:
                    data = resp.json()
                except Exception:
                    logger.error(
                        "[trades] JSON decode error for %s offset=%d",
                        cid[:16],
                        trade_offset,
                    )
                    break

                if not isinstance(data, list) or len(data) == 0:
                    break

                for t in data:
                    size = float(t.get("size", 0) or 0)
                    price = float(t.get("price", 0) or 0)
                    ts_raw = t.get(
                        "timestamp", t.get("match_time", t.get("matchTime", ""))
                    )
                    market_trades.append(
                        (
                            t.get(
                                "transactionHash",
                                t.get("transaction_hash", t.get("id", "")),
                            ),
                            t.get("proxyWallet", t.get("owner", "")).lower(),
                            cid,
                            slug,
                            t.get("side", ""),
                            t.get("outcome", ""),
                            size,
                            price,
                            round(size * price, 6),
                            float(
                                t.get("fee_rate_bps", t.get("feeRateBps", 0)) or 0
                            ),
                            str(ts_raw),
                            t.get(
                                "transactionHash", t.get("transaction_hash", "")
                            ),
                        )
                    )

                if len(data) < 500:
                    break
                trade_offset += 500

            # Insert trades for this market
            if market_trades:
                conn.executemany(
                    """
                    INSERT INTO trades
                    (trade_id, wallet, condition_id, slug, side, outcome,
                     size, price, usdc_value, fee_rate_bps, match_time,
                     transaction_hash)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    market_trades,
                )
                conn.commit()
                total_trades += len(market_trades)

        except Exception as e:
            logger.error("[trades] Error on market %s: %s", cid[:20], e)

        markets_processed += 1
        checkpoint_counter += 1

        # Progress log every 100 markets
        if markets_processed % 100 == 0:
            logger.info(
                "[trades] %d/%d markets (%.1f%%) | %d trades total",
                markets_processed,
                total_markets,
                100 * markets_processed / total_markets,
                total_trades,
            )
            report_progress(
                asset,
                "trades",
                markets_processed,
                total_markets,
                f"{total_trades} trades",
            )

        # Checkpoint every 500 markets
        if checkpoint_counter >= 500:
            set_progress(conn, "trades", condition_id=cid, items=total_trades)
            checkpoint_counter = 0
            logger.info(
                "[trades] Checkpoint at %d markets, %d trades",
                markets_processed,
                total_trades,
            )

    # ------------------------------------------------------------------
    # 4. Final progress save
    # ------------------------------------------------------------------
    last_cid = cid if markets else ""
    set_progress(conn, "trades", condition_id=last_cid, items=total_trades)

    logger.info(
        "[trades] Done -- %d/%d markets, %d trades total",
        markets_processed,
        total_markets,
        total_trades,
    )
    report_progress(
        asset,
        "trades",
        markets_processed,
        total_markets,
        f"DONE: {total_trades} trades",
    )

    conn.close()


def main():
    asset = parse_asset_arg()
    collect_trades(asset)


if __name__ == "__main__":
    main()
