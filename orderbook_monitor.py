#!/usr/bin/env python3
"""
orderbook_monitor.py -- Capture BTC 5m orderbook snapshots every 5 minutes for 7 days.

Refreshes active markets each cycle, snapshots both yes/no books,
stores in the existing data/btc5m.db orderbooks table.

Usage:
  python orderbook_monitor.py                # Run for 7 days
  python orderbook_monitor.py --days 3       # Run for 3 days
  python orderbook_monitor.py --interval 60  # Snapshot every 60 seconds
  nohup python orderbook_monitor.py &        # Run in background
"""

import argparse
import json
import logging
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent / "data"
DB_PATH = BASE_DIR / "btc5m.db"

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "orderbook_monitor.log", mode="a"),
    ],
)
logger = logging.getLogger("ob_monitor")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.warning("Signal %s -- shutting down after current cycle", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "polymarket-ob-monitor/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


def fetch_active_markets(session):
    """Get all currently active BTC 5m markets from Gamma API."""
    markets = []
    offset = 0
    while True:
        try:
            r = session.get(GAMMA_EVENTS_URL, params={
                "tag_slug": "5M",
                "limit": 100,
                "offset": offset,
                "order": "endDate",
                "ascending": "false",
                "active": "true",
                "closed": "false",
            }, timeout=15)
            if r.status_code != 200:
                break
            events = r.json()
            if not events:
                break

            all_closed = True
            for e in events:
                if e.get("closed"):
                    continue
                all_closed = False
                slug = e.get("slug", "")
                if not slug.startswith("btc-updown-5m"):
                    continue
                for m in e.get("markets", []):
                    cid = m.get("conditionId", "")
                    tokens = m.get("clobTokenIds", "[]")
                    if isinstance(tokens, str):
                        tokens = json.loads(tokens)
                    if cid and len(tokens) >= 2:
                        markets.append({
                            "condition_id": cid,
                            "slug": slug,
                            "yes_token": tokens[0],
                            "no_token": tokens[1],
                        })

            if all_closed or len(events) < 100:
                break
            offset += 100
        except Exception as e:
            logger.error("Error fetching markets: %s", e)
            break

    return markets


def upsert_markets(conn, markets):
    """INSERT OR IGNORE each active market into the markets table so the
    replay harness can translate slug -> condition_id / token_ids. No-op
    if the markets table does not exist."""
    try:
        conn.execute("SELECT 1 FROM markets LIMIT 1").fetchone()
    except sqlite3.Error:
        return
    for m in markets:
        conn.execute(
            "INSERT OR IGNORE INTO markets "
            "(condition_id, slug, yes_token_id, no_token_id, active, closed) "
            "VALUES (?, ?, ?, ?, 1, 0)",
            (m["condition_id"], m["slug"], m["yes_token"], m["no_token"]),
        )


def snapshot_orderbooks(session, conn, markets):
    """Snapshot orderbooks for all active markets."""
    snapshot_time = datetime.now(timezone.utc).isoformat()
    captured = 0
    skipped = 0

    upsert_markets(conn, markets)

    for m in markets:
        if _shutdown:
            break

        for side_label, token_id in [("yes", m["yes_token"]), ("no", m["no_token"])]:
            try:
                time.sleep(0.01)  # light rate limit
                r = session.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=10)
                if r.status_code == 404:
                    skipped += 1
                    continue
                if r.status_code != 200:
                    continue

                book = r.json()
                if "error" in book:
                    skipped += 1
                    continue

                bids = book.get("bids", [])
                asks = book.get("asks", [])

                best_bid = float(bids[0]["price"]) if bids else 0.0
                best_ask = float(asks[0]["price"]) if asks else 0.0
                spread = round(best_ask - best_bid, 6) if (best_bid and best_ask) else 0.0

                depth_bid = sum(float(b.get("size", 0)) for b in bids if best_bid - float(b["price"]) <= 0.10)
                depth_ask = sum(float(a.get("size", 0)) for a in asks if float(a["price"]) - best_ask <= 0.10)
                depth_10c = round(depth_bid + depth_ask, 2)

                conn.execute("""
                    INSERT INTO orderbooks
                    (condition_id, side, bids, asks, best_bid, best_ask, spread, depth_10c, snapshot_time)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    m["condition_id"], side_label,
                    json.dumps(bids), json.dumps(asks),
                    best_bid, best_ask, spread, depth_10c,
                    snapshot_time,
                ))
                captured += 1

            except Exception as e:
                logger.warning("Error on %s %s: %s", m["slug"][:30], side_label, e)

    conn.commit()
    return captured, skipped


def main():
    parser = argparse.ArgumentParser(description="BTC 5m orderbook monitor")
    parser.add_argument("--days", type=float, default=7, help="Days to run (default: 7)")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between snapshots (default: 300)")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    session = make_session()

    end_time = time.time() + (args.days * 86400)
    cycle = 0

    logger.info("=" * 50)
    logger.info("Orderbook monitor starting -- %.1f days, %ds interval", args.days, args.interval)
    logger.info("=" * 50)

    while time.time() < end_time and not _shutdown:
        cycle += 1
        t0 = time.time()

        markets = fetch_active_markets(session)
        captured, skipped = snapshot_orderbooks(session, conn, markets)

        total_rows = conn.execute("SELECT COUNT(*) FROM orderbooks").fetchone()[0]
        elapsed = time.time() - t0

        logger.info(
            "[cycle %d] %d active markets, %d books captured, %d no-book, %d total rows (%.1fs)",
            cycle, len(markets), captured, skipped, total_rows, elapsed,
        )

        # Sleep until next interval
        sleep_time = max(0, args.interval - elapsed)
        if sleep_time > 0 and not _shutdown:
            time.sleep(sleep_time)

    # Final count
    total = conn.execute("SELECT COUNT(*) FROM orderbooks").fetchone()[0]
    logger.info("Monitor stopped -- %d total orderbook snapshots", total)
    conn.close()


if __name__ == "__main__":
    main()
