#!/usr/bin/env python3
"""
scrap/02_spot_prices.py -- Collect 1-minute OHLC candles from Binance for a
given crypto asset.

Queries the asset's markets table for the date range (MIN(start_date) to
MAX(end_date)).  Falls back to 6 months ago -> now when the markets table
is empty.  Resumes from the last stored candle on re-run.

Usage:
    python -m scrap.02_spot_prices --asset btc
    python scrap/02_spot_prices.py --asset eth
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scrap.config import (
    ASSETS,
    BINANCE_KLINES_URL,
    init_db,
    init_logging,
    is_shutdown,
    limiter_binance,
    make_session,
    resilient_get,
    get_progress,
    set_progress,
    report_progress,
    parse_asset_arg,
)

# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_ISO_FMTS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_date(s: str) -> datetime | None:
    """Parse an ISO-ish date string into a tz-aware UTC datetime."""
    # Try fromisoformat first (handles +00:00 and Z on 3.11+)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    for fmt in _ISO_FMTS:
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Main collection logic
# ---------------------------------------------------------------------------


def collect_spot(asset: str) -> None:
    logger = init_logging(asset)
    cfg = ASSETS[asset]
    symbol = cfg["binance_symbol"]

    logger.info("=" * 60)
    logger.info("[spot] Starting %s 1m candle collection", symbol)
    logger.info("=" * 60)

    conn = init_db(asset)
    session = make_session()

    # ------------------------------------------------------------------
    # 1. Determine the date range
    # ------------------------------------------------------------------
    row = conn.execute(
        "SELECT MIN(start_date), MAX(end_date) FROM markets"
    ).fetchone()

    if row and row[0]:
        start_dt = _parse_date(row[0])
        end_dt = _parse_date(row[1])
        if start_dt is None or end_dt is None:
            logger.error(
                "[spot] Cannot parse market dates: %s -- %s", row[0], row[1]
            )
            conn.close()
            return
        logger.info(
            "[spot] Market date range: %s -> %s",
            start_dt.isoformat(),
            end_dt.isoformat(),
        )
    else:
        # Fallback: 6 months ago to now
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=182)
        logger.info(
            "[spot] No markets found -- using fallback range: %s -> %s",
            start_dt.isoformat(),
            end_dt.isoformat(),
        )

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # ------------------------------------------------------------------
    # 2. Resume from last stored candle
    # ------------------------------------------------------------------
    existing_max = conn.execute("SELECT MAX(timestamp) FROM spot").fetchone()[0]
    if existing_max:
        resume_ms = existing_max * 1000 + 60_000  # one minute after last candle
        start_ms = max(start_ms, resume_ms)
        logger.info(
            "[spot] Resuming from %s",
            datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
        )

    # Estimate total candles for progress reporting
    total_expected = max((end_ms - start_ms) // 60_000, 0)
    total_candles = 0
    current_ms = start_ms

    if current_ms >= end_ms:
        logger.info("[spot] Already up to date")
        report_progress(asset, "spot", total_expected, total_expected, "up to date")
        conn.close()
        return

    # ------------------------------------------------------------------
    # 3. Paginate through Binance klines API
    # ------------------------------------------------------------------
    while current_ms < end_ms and not is_shutdown():
        resp = resilient_get(
            session,
            BINANCE_KLINES_URL,
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": current_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
            limiter_binance,
            logger=logger,
        )

        if resp is None:
            logger.warning("[spot] Request failed at startTime=%d -- stopping", current_ms)
            break

        try:
            klines = resp.json()
        except Exception:
            logger.error("[spot] JSON decode error at startTime=%d", current_ms)
            break

        if not klines:
            break

        # Parse batch
        batch = []
        for k in klines:
            # [open_time, open, high, low, close, volume, close_time, ...]
            batch.append((
                int(k[0]) // 1000,   # timestamp in seconds
                float(k[1]),          # open
                float(k[2]),          # high
                float(k[3]),          # low
                float(k[4]),          # close
                float(k[5]),          # volume
            ))

        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO spot "
                "(timestamp, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?)",
                batch,
            )
            conn.commit()
            total_candles += len(batch)

        # Advance past the last candle's close_time + 1ms
        current_ms = int(klines[-1][6]) + 1

        # Periodic progress
        if total_candles % 10_000 == 0:
            report_progress(
                asset, "spot", total_candles, total_expected,
                f"{total_candles} candles",
            )
            logger.info("[spot] %d candles collected so far", total_candles)

        # Last page
        if len(klines) < 1000:
            break

    # ------------------------------------------------------------------
    # 4. Finalize
    # ------------------------------------------------------------------
    set_progress(conn, "spot", items=total_candles)
    report_progress(
        asset, "spot", total_expected, total_expected,
        f"done -- {total_candles} candles",
    )
    logger.info("[spot] Done -- %d candles collected", total_candles)
    conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asset = parse_asset_arg()
    collect_spot(asset)
