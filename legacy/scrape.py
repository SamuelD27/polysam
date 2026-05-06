#!/usr/bin/env python3
"""
scrape.py -- Comprehensive historical data scraper for ALL Polymarket BTC 5-minute
("Bitcoin Up or Down" 5m) markets.

Collects 7 datasets:
  1. markets     -- Full market catalog from Gamma API
  2. trades      -- All trades from Data API (largest, hours)
  3. prices      -- Price histories from CLOB API
  4. orderbooks  -- Order book snapshots for active markets
  5. btc_spot    -- BTC/USDT 1m candles from Binance
  6. resolutions -- Resolution data (computed from markets + spot)
  7. traders     -- Top 500 trader profiles (computed from trades)

Usage:
  python scrape.py              # Run all steps sequentially
  python scrape.py --only trades  # Run a single step
  python scrape.py --only markets,trades  # Run multiple steps

Data stored in:
  data/btc5m.db   -- SQLite database (WAL mode)
  data/*.csv       -- CSV exports per dataset
  data/errors.log  -- Error log
"""

import argparse
import csv
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent / "data"
DB_PATH = BASE_DIR / "btc5m.db"
ERROR_LOG = BASE_DIR / "errors.log"

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_PRICES_URL = "https://clob.polymarket.com/prices-history"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
CLOB_MIDPOINT_URL = "https://clob.polymarket.com/midpoint"
DATA_TRADES_URL = "https://data-api.polymarket.com/trades"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# ---------------------------------------------------------------------------
# Rate limits (requests per second, conservative)
# ---------------------------------------------------------------------------
RATE_GAMMA_EVENTS = 40       # 500/10s -> 50/s, use 40
RATE_GAMMA_MARKETS = 25      # 300/10s -> 30/s, use 25
RATE_CLOB_PRICES = 80        # 1000/10s -> 100/s, use 80
RATE_CLOB_MARKET = 120       # 1500/10s -> 150/s, use 120
RATE_DATA_TRADES = 15        # 200/10s -> 20/s, use 15
RATE_BINANCE = 10            # conservative

# ---------------------------------------------------------------------------
# Date filter (set via --since CLI arg)
# ---------------------------------------------------------------------------
SINCE_DATE: datetime | None = None  # set in main()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
BASE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(ERROR_LOG, mode="a"),
    ],
)
logger = logging.getLogger("scrape")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.warning("Signal %s received -- will stop after current item", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ---------------------------------------------------------------------------
# Rate limiter (token bucket)
# ---------------------------------------------------------------------------


class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, rate_per_second: float):
        self.rate = rate_per_second
        self.interval = 1.0 / rate_per_second
        self.last_call = 0.0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self.last_call
        if elapsed < self.interval:
            sleep_time = self.interval - elapsed
            time.sleep(sleep_time)
        self.last_call = time.monotonic()


# Pre-built limiters
limiter_gamma_events = RateLimiter(RATE_GAMMA_EVENTS)
limiter_gamma_markets = RateLimiter(RATE_GAMMA_MARKETS)
limiter_clob_prices = RateLimiter(RATE_CLOB_PRICES)
limiter_clob_market = RateLimiter(RATE_CLOB_MARKET)
limiter_data_trades = RateLimiter(RATE_DATA_TRADES)
limiter_binance = RateLimiter(RATE_BINANCE)

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "polymarket-btc5m-scraper/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",  # avoid brotli decode errors
    })
    return s


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache

    # -- scrape_progress table for resumability
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_progress (
            dataset_name TEXT PRIMARY KEY,
            last_offset INTEGER DEFAULT 0,
            last_condition_id TEXT DEFAULT '',
            items_collected INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)

    # -- 01 markets
    conn.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            condition_id TEXT PRIMARY KEY,
            slug TEXT,
            title TEXT,
            start_date TEXT,
            end_date TEXT,
            volume REAL,
            liquidity REAL,
            closed INTEGER,
            active INTEGER,
            yes_token_id TEXT,
            no_token_id TEXT,
            outcome_price_yes REAL,
            outcome_price_no REAL,
            resolved_outcome TEXT
        )
    """)

    # -- 02 trades
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT,
            wallet TEXT,
            condition_id TEXT,
            slug TEXT,
            side TEXT,
            outcome TEXT,
            size REAL,
            price REAL,
            usdc_value REAL,
            fee_rate_bps REAL,
            match_time TEXT,
            transaction_hash TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_match_time ON trades(match_time)")

    # -- 03 price_histories
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_histories (
            condition_id TEXT,
            timestamp INTEGER,
            yes_price REAL,
            no_price REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_condition ON price_histories(condition_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_timestamp ON price_histories(timestamp)")

    # -- 04 orderbooks
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orderbooks (
            condition_id TEXT,
            side TEXT,
            bids TEXT,
            asks TEXT,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            depth_10c REAL,
            snapshot_time TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ob_condition ON orderbooks(condition_id)")

    # -- 05 btc_spot
    conn.execute("""
        CREATE TABLE IF NOT EXISTS btc_spot (
            timestamp INTEGER PRIMARY KEY,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL
        )
    """)

    # -- 06 resolutions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resolutions (
            condition_id TEXT PRIMARY KEY,
            slug TEXT,
            start_date TEXT,
            end_date TEXT,
            resolved_outcome TEXT,
            outcome_price_yes REAL,
            outcome_price_no REAL,
            volume REAL,
            btc_price_at_start REAL,
            btc_price_at_end REAL
        )
    """)

    # -- 07 traders
    conn.execute("""
        CREATE TABLE IF NOT EXISTS traders (
            wallet TEXT PRIMARY KEY,
            total_trades INTEGER,
            total_volume_usdc REAL,
            win_rate REAL,
            avg_trade_size REAL,
            first_trade_time TEXT,
            last_trade_time TEXT,
            favorite_side TEXT,
            favorite_outcome TEXT
        )
    """)

    conn.commit()
    return conn


def get_progress(conn: sqlite3.Connection, dataset: str) -> dict:
    row = conn.execute(
        "SELECT last_offset, last_condition_id, items_collected FROM scrape_progress WHERE dataset_name=?",
        (dataset,),
    ).fetchone()
    if row:
        return {"last_offset": row[0], "last_condition_id": row[1], "items_collected": row[2]}
    return {"last_offset": 0, "last_condition_id": "", "items_collected": 0}


def set_progress(conn: sqlite3.Connection, dataset: str, offset: int = 0,
                 condition_id: str = "", items: int = 0):
    conn.execute("""
        INSERT INTO scrape_progress (dataset_name, last_offset, last_condition_id, items_collected, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(dataset_name) DO UPDATE SET
            last_offset=excluded.last_offset,
            last_condition_id=excluded.last_condition_id,
            items_collected=excluded.items_collected,
            updated_at=excluded.updated_at
    """, (dataset, offset, condition_id, items, datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ---------------------------------------------------------------------------
# Resilient HTTP GET with backoff
# ---------------------------------------------------------------------------


def resilient_get(session: requests.Session, url: str, params: dict,
                  limiter: RateLimiter, max_retries: int = 5,
                  allow_404: bool = False) -> requests.Response | None:
    """GET with rate limiting, retries, and exponential backoff."""
    backoff = 5.0
    for attempt in range(max_retries):
        if _shutdown:
            return None
        limiter.wait()
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = min(backoff, 60)
                logger.warning("429 on %s -- waiting %.1fs (attempt %d)", url, wait, attempt + 1)
                time.sleep(wait)
                backoff *= 2
                continue
            if resp.status_code == 403:
                logger.warning("403 Cloudflare on %s -- waiting 30s", url)
                time.sleep(30)
                continue
            if resp.status_code == 404 and allow_404:
                return resp
            if resp.status_code == 400:
                # Bad request (e.g., invalid offset) -- non-retryable
                return None
            if resp.status_code >= 500:
                wait = min(backoff, 60)
                logger.warning("%d on %s -- waiting %.1fs", resp.status_code, url, wait)
                time.sleep(wait)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            logger.warning("Timeout on %s -- retry %d", url, attempt + 1)
            time.sleep(backoff)
            backoff *= 2
        except requests.exceptions.ConnectionError as e:
            logger.warning("Connection error on %s: %s -- retry %d", url, e, attempt + 1)
            time.sleep(backoff)
            backoff *= 2
    logger.error("Failed after %d retries: %s params=%s", max_retries, url, params)
    return None


# ===================================================================
# STEP 1: Markets
# ===================================================================


def _parse_iso_to_epoch(dt_str: str) -> int | None:
    """Parse an ISO date string to epoch seconds."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _parse_event_market(event: dict) -> list[tuple]:
    """Parse markets from a Gamma event response into DB tuples."""
    batch = []
    # Use event-level slug to identify BTC 5m events
    event_slug = event.get("slug", "")
    if not event_slug.startswith("btc-updown-5m"):
        return batch

    # Date filter: skip events older than SINCE_DATE
    if SINCE_DATE is not None:
        end_str = event.get("endDate", "")
        if end_str:
            end_ts = _parse_iso_to_epoch(end_str)
            if end_ts and end_ts < int(SINCE_DATE.timestamp()):
                return batch

    markets_list = event.get("markets", [])
    for m in markets_list:
        cid = m.get("conditionId", m.get("condition_id", ""))
        if not cid:
            continue

        clob_tokens = m.get("clobTokenIds", "[]")
        if isinstance(clob_tokens, str):
            try:
                clob_tokens = json.loads(clob_tokens)
            except json.JSONDecodeError:
                clob_tokens = []

        yes_token = clob_tokens[0] if len(clob_tokens) > 0 else ""
        no_token = clob_tokens[1] if len(clob_tokens) > 1 else ""

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


def collect_markets(conn: sqlite3.Connection, session: requests.Session):
    logger.info("=" * 60)
    logger.info("[markets] Starting market catalog collection")
    logger.info("=" * 60)

    progress = get_progress(conn, "markets")
    offset = progress["last_offset"]
    total_inserted = progress["items_collected"]
    consecutive_empty = 0

    # Gamma slug_contains is unreliable; use tag_slug=5M and filter client-side.
    # Sort by endDate descending so newest markets come first (critical for --since).
    while not _shutdown:
        resp = resilient_get(session, GAMMA_EVENTS_URL, {
            "tag_slug": "5M",
            "limit": 100,
            "offset": offset,
            "order": "endDate",
            "ascending": "false",
        }, limiter_gamma_events)

        if resp is None:
            break

        events = resp.json()
        if not events:
            logger.info("[markets] No more events at offset %d", offset)
            break

        batch = []
        for event in events:
            batch.extend(_parse_event_market(event))

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
        set_progress(conn, "markets", offset=offset, items=total_inserted)

        if total_inserted % 500 == 0 or len(events) < 100:
            logger.info("[markets] %d BTC 5m markets collected (offset %d, page had %d events)",
                        total_inserted, offset, len(events))

        # With descending order + SINCE_DATE, once all events on a page are
        # before the cutoff, every subsequent page will be too -- stop early.
        if SINCE_DATE is not None and len(events) > 0:
            oldest_end = events[-1].get("endDate", "")
            oldest_ts = _parse_iso_to_epoch(oldest_end)
            if oldest_ts and oldest_ts < int(SINCE_DATE.timestamp()):
                logger.info("[markets] Reached events before cutoff (%s) -- stopping", oldest_end[:19])
                break

        # tag_slug=5M returns BTC + ETH + SOL + XRP 5m events.
        # Stop if we've had 20 consecutive pages with zero BTC markets
        # (means we're past the end) or the page was short.
        if len(events) < 100:
            break
        if consecutive_empty >= 20:
            logger.info("[markets] 20 consecutive pages with no BTC 5m markets -- stopping")
            break

    set_progress(conn, "markets", offset=offset, items=total_inserted)
    logger.info("[markets] Done -- %d total BTC 5m markets", total_inserted)


# ===================================================================
# STEP 2: Trades
# ===================================================================


def collect_trades(conn: sqlite3.Connection, session: requests.Session):
    logger.info("=" * 60)
    logger.info("[trades] Starting trade collection for all markets")
    logger.info("=" * 60)

    # Get all markets
    markets = conn.execute("SELECT condition_id, slug FROM markets ORDER BY start_date").fetchall()
    if not markets:
        logger.error("[trades] No markets in DB -- run markets step first")
        return

    total_markets = len(markets)
    logger.info("[trades] %d markets to process", total_markets)

    # Find which markets already have trades (for resumability)
    progress = get_progress(conn, "trades")
    done_cids = set()
    if progress["last_condition_id"]:
        rows = conn.execute("SELECT DISTINCT condition_id FROM trades").fetchall()
        done_cids = {r[0] for r in rows}
        logger.info("[trades] Resuming -- %d markets already have trades", len(done_cids))

    markets_processed = len(done_cids)
    total_trades = progress["items_collected"]
    csv_path = BASE_DIR / "02_trades.csv"
    csv_partial = BASE_DIR / "02_trades_partial.csv"
    checkpoint_counter = 0

    for cid, slug in markets:
        if _shutdown:
            break
        if cid in done_cids:
            continue

        try:
            # Paginate all trades for this market
            market_trades = []
            trade_offset = 0

            while not _shutdown:
                resp = resilient_get(session, DATA_TRADES_URL, {
                    "market": cid,
                    "limit": 500,
                    "offset": trade_offset,
                }, limiter_data_trades)

                if resp is None:
                    break

                try:
                    data = resp.json()
                except Exception:
                    logger.error("[trades] JSON decode error for %s offset=%d", cid[:16], trade_offset)
                    break

                if not isinstance(data, list) or len(data) == 0:
                    break

                for t in data:
                    size = float(t.get("size", 0) or 0)
                    price = float(t.get("price", 0) or 0)
                    # API returns: proxyWallet, transactionHash, timestamp (epoch int),
                    # side, outcome, size, price. No fee_rate_bps field.
                    ts_raw = t.get("timestamp", t.get("match_time", t.get("matchTime", "")))
                    market_trades.append((
                        t.get("transactionHash", t.get("transaction_hash", t.get("id", ""))),
                        t.get("proxyWallet", t.get("owner", "")).lower(),
                        cid,
                        slug,
                        t.get("side", ""),
                        t.get("outcome", ""),
                        size,
                        price,
                        round(size * price, 6),
                        float(t.get("fee_rate_bps", t.get("feeRateBps", 0)) or 0),
                        str(ts_raw),
                        t.get("transactionHash", t.get("transaction_hash", "")),
                    ))

                if len(data) < 500:
                    break
                trade_offset += 500

            # Insert trades for this market
            if market_trades:
                conn.executemany("""
                    INSERT INTO trades
                    (trade_id, wallet, condition_id, slug, side, outcome,
                     size, price, usdc_value, fee_rate_bps, match_time, transaction_hash)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, market_trades)
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
                markets_processed, total_markets,
                100 * markets_processed / total_markets,
                total_trades,
            )

        # Checkpoint every 500 markets
        if checkpoint_counter >= 500:
            set_progress(conn, "trades", condition_id=cid, items=total_trades)
            checkpoint_counter = 0
            logger.info("[trades] Checkpoint at %d markets, %d trades", markets_processed, total_trades)

    set_progress(conn, "trades", condition_id=cid if markets else "", items=total_trades)
    logger.info("[trades] Done -- %d markets, %d trades", markets_processed, total_trades)


# ===================================================================
# STEP 3: Price histories
# ===================================================================


def collect_price_histories(conn: sqlite3.Connection, session: requests.Session):
    logger.info("=" * 60)
    logger.info("[prices] Starting price history collection")
    logger.info("=" * 60)

    # CLOB prices-history needs token_id, not condition_id
    markets = conn.execute(
        "SELECT condition_id, yes_token_id FROM markets WHERE closed=1 AND yes_token_id != '' ORDER BY start_date"
    ).fetchall()
    if not markets:
        logger.error("[prices] No closed markets with token IDs found")
        return

    total_markets = len(markets)
    logger.info("[prices] %d closed markets to fetch", total_markets)

    # Find already-fetched
    done = set()
    progress = get_progress(conn, "prices")
    if progress["items_collected"] > 0:
        rows = conn.execute("SELECT DISTINCT condition_id FROM price_histories").fetchall()
        done = {r[0] for r in rows}
        logger.info("[prices] Resuming -- %d markets already done", len(done))

    processed = len(done)
    total_points = progress["items_collected"]

    for cid, yes_token in markets:
        if _shutdown:
            break
        if cid in done:
            continue

        # API requires token_id, not condition_id
        resp = resilient_get(session, CLOB_PRICES_URL, {
            "market": yes_token,
            "interval": "max",
            "fidelity": 1,
        }, limiter_clob_prices)

        if resp is None:
            processed += 1
            continue

        try:
            data = resp.json()
        except Exception:
            logger.error("[prices] JSON error for %s", cid[:16])
            processed += 1
            continue

        if not data or not isinstance(data, dict):
            processed += 1
            continue

        history = data.get("history", [])
        if not isinstance(history, list):
            history = []

        # Response format: {t: epoch_seconds, p: yes_price}
        batch = []
        for point in history:
            ts = point.get("t", 0)
            price = float(point.get("p", 0))
            batch.append((cid, int(ts), price, round(1.0 - price, 6)))

        if batch:
            conn.executemany(
                "INSERT INTO price_histories (condition_id, timestamp, yes_price, no_price) VALUES (?,?,?,?)",
                batch,
            )
            conn.commit()
            total_points += len(batch)

        processed += 1

        if processed % 100 == 0:
            logger.info("[prices] %d/%d markets | %d data points", processed, total_markets, total_points)

        if processed % 1000 == 0:
            set_progress(conn, "prices", items=total_points)

    set_progress(conn, "prices", items=total_points)
    logger.info("[prices] Done -- %d markets, %d data points", processed, total_points)


# ===================================================================
# STEP 4: Orderbooks
# ===================================================================


def collect_orderbooks(conn: sqlite3.Connection, session: requests.Session):
    logger.info("=" * 60)
    logger.info("[orderbooks] Starting orderbook snapshots for active markets")
    logger.info("=" * 60)

    markets = conn.execute(
        "SELECT condition_id, yes_token_id, no_token_id FROM markets WHERE closed=0 AND active=1"
    ).fetchall()
    if not markets:
        logger.info("[orderbooks] No active markets found")
        return

    logger.info("[orderbooks] %d active markets", len(markets))
    snapshot_time = datetime.now(timezone.utc).isoformat()
    processed = 0

    for cid, yes_token, no_token in markets:
        if _shutdown:
            break

        for side_label, token_id in [("yes", yes_token), ("no", no_token)]:
            if not token_id:
                continue

            # Many 5m markets return 404 ("No orderbook exists") -- skip gracefully
            limiter_clob_market.wait()
            try:
                resp = session.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=30)
            except Exception as e:
                logger.warning("[orderbooks] Request error for %s %s: %s", cid[:16], side_label, e)
                continue
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                continue

            try:
                book = resp.json()
            except Exception:
                logger.error("[orderbooks] JSON error for %s %s", cid[:16], side_label)
                continue

            if "error" in book:
                continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 0.0
            spread = round(best_ask - best_bid, 6) if (best_bid and best_ask) else 0.0

            # Depth within 10 cents of best
            depth_bid = sum(float(b.get("size", 0)) for b in bids if best_bid - float(b["price"]) <= 0.10)
            depth_ask = sum(float(a.get("size", 0)) for a in asks if float(a["price"]) - best_ask <= 0.10)
            depth_10c = round(depth_bid + depth_ask, 2)

            conn.execute("""
                INSERT INTO orderbooks
                (condition_id, side, bids, asks, best_bid, best_ask, spread, depth_10c, snapshot_time)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                cid, side_label,
                json.dumps(bids), json.dumps(asks),
                best_bid, best_ask, spread, depth_10c,
                snapshot_time,
            ))

        processed += 1
        if processed % 100 == 0:
            conn.commit()
            logger.info("[orderbooks] %d/%d markets", processed, len(markets))

    conn.commit()
    set_progress(conn, "orderbooks", items=processed)
    logger.info("[orderbooks] Done -- %d markets snapshotted", processed)


# ===================================================================
# STEP 5: BTC Spot
# ===================================================================


def collect_btc_spot(conn: sqlite3.Connection, session: requests.Session):
    logger.info("=" * 60)
    logger.info("[btc_spot] Starting BTC/USDT 1m candle collection")
    logger.info("=" * 60)

    # Get date range from markets
    row = conn.execute("SELECT MIN(start_date), MAX(end_date) FROM markets").fetchone()
    if not row or not row[0]:
        logger.error("[btc_spot] No market dates found")
        return

    start_str, end_str = row[0], row[1]
    try:
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    except Exception:
        # Fallback: parse common date formats
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                start_dt = datetime.strptime(start_str[:26], fmt).replace(tzinfo=timezone.utc)
                end_dt = datetime.strptime(end_str[:26], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            logger.error("[btc_spot] Cannot parse dates: %s -- %s", start_str, end_str)
            return

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # Check what we already have
    existing_max = conn.execute("SELECT MAX(timestamp) FROM btc_spot").fetchone()[0]
    if existing_max:
        start_ms = max(start_ms, existing_max * 1000 + 60000)  # start after last candle
        logger.info("[btc_spot] Resuming from %s", datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc))

    total_candles = 0
    current_ms = start_ms

    while current_ms < end_ms and not _shutdown:
        resp = resilient_get(session, BINANCE_KLINES_URL, {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "startTime": current_ms,
            "endTime": end_ms,
            "limit": 1000,
        }, limiter_binance)

        if resp is None:
            break

        try:
            klines = resp.json()
        except Exception:
            logger.error("[btc_spot] JSON error at %d", current_ms)
            break

        if not klines:
            break

        batch = []
        for k in klines:
            # [open_time, open, high, low, close, volume, close_time, ...]
            batch.append((
                int(k[0]) // 1000,  # timestamp in seconds
                float(k[1]),  # open
                float(k[2]),  # high
                float(k[3]),  # low
                float(k[4]),  # close
                float(k[5]),  # volume
            ))

        if batch:
            conn.executemany(
                "INSERT OR IGNORE INTO btc_spot (timestamp, open, high, low, close, volume) VALUES (?,?,?,?,?,?)",
                batch,
            )
            conn.commit()
            total_candles += len(batch)

        # Move past last candle
        current_ms = int(klines[-1][0]) + 60000

        if total_candles % 10000 == 0:
            logger.info("[btc_spot] %d candles collected", total_candles)

        if len(klines) < 1000:
            break

    set_progress(conn, "btc_spot", items=total_candles)
    logger.info("[btc_spot] Done -- %d candles", total_candles)


# ===================================================================
# STEP 6: Resolutions (computed)
# ===================================================================


def compute_resolutions(conn: sqlite3.Connection):
    logger.info("=" * 60)
    logger.info("[resolutions] Computing resolution data")
    logger.info("=" * 60)

    conn.execute("DELETE FROM resolutions")

    # Compute in Python since SQLite strftime('%s') doesn't handle all ISO formats
    markets = conn.execute("""
        SELECT condition_id, slug, start_date, end_date, resolved_outcome,
               outcome_price_yes, outcome_price_no, volume
        FROM markets WHERE closed = 1 AND resolved_outcome != ''
    """).fetchall()

    batch = []
    for cid, slug, start_date, end_date, resolved, py, pn, vol in markets:
        # Parse timestamps
        start_ts = _parse_iso_to_epoch(start_date)
        end_ts = _parse_iso_to_epoch(end_date)

        btc_start = None
        btc_end = None
        if start_ts:
            row = conn.execute(
                "SELECT close FROM btc_spot WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                (start_ts,),
            ).fetchone()
            if row:
                btc_start = row[0]
        if end_ts:
            row = conn.execute(
                "SELECT close FROM btc_spot WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                (end_ts,),
            ).fetchone()
            if row:
                btc_end = row[0]

        batch.append((cid, slug, start_date, end_date, resolved, py, pn, vol, btc_start, btc_end))

    if batch:
        conn.executemany("""
            INSERT INTO resolutions
            (condition_id, slug, start_date, end_date, resolved_outcome,
             outcome_price_yes, outcome_price_no, volume,
             btc_price_at_start, btc_price_at_end)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, batch)
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
    set_progress(conn, "resolutions", items=count)
    logger.info("[resolutions] Done -- %d resolved markets", count)


# ===================================================================
# STEP 7: Traders (computed)
# ===================================================================


def compute_traders(conn: sqlite3.Connection):
    logger.info("=" * 60)
    logger.info("[traders] Computing top 500 trader profiles")
    logger.info("=" * 60)

    conn.execute("DELETE FROM traders")

    # First, compute total volume per wallet, get top 500
    conn.execute("""
        INSERT INTO traders
        (wallet, total_trades, total_volume_usdc, win_rate, avg_trade_size,
         first_trade_time, last_trade_time, favorite_side, favorite_outcome)
        SELECT
            t.wallet,
            COUNT(*) as total_trades,
            ROUND(SUM(t.usdc_value), 2) as total_volume_usdc,
            ROUND(
                CAST(SUM(CASE
                    WHEN t.outcome = m.resolved_outcome
                    THEN 1 ELSE 0
                END) AS REAL) / NULLIF(
                    SUM(CASE WHEN m.resolved_outcome != '' THEN 1 ELSE 0 END), 0
                ), 4
            ) as win_rate,
            ROUND(AVG(t.usdc_value), 2) as avg_trade_size,
            MIN(t.match_time) as first_trade_time,
            MAX(t.match_time) as last_trade_time,
            (SELECT t2.side FROM trades t2 WHERE t2.wallet = t.wallet
             GROUP BY t2.side ORDER BY COUNT(*) DESC LIMIT 1) as favorite_side,
            (SELECT t3.outcome FROM trades t3 WHERE t3.wallet = t.wallet
             GROUP BY t3.outcome ORDER BY COUNT(*) DESC LIMIT 1) as favorite_outcome
        FROM trades t
        LEFT JOIN markets m ON t.condition_id = m.condition_id
        WHERE t.wallet != ''
        GROUP BY t.wallet
        ORDER BY total_volume_usdc DESC
        LIMIT 500
    """)
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM traders").fetchone()[0]
    set_progress(conn, "traders", items=count)
    logger.info("[traders] Done -- %d trader profiles", count)


# ===================================================================
# CSV Export
# ===================================================================

EXPORT_TABLES = [
    ("markets", "01_markets.csv"),
    ("trades", "02_trades.csv"),
    ("price_histories", "03_price_histories.csv"),
    ("orderbooks", "04_orderbooks.csv"),
    ("btc_spot", "05_btc_spot.csv"),
    ("resolutions", "06_resolutions.csv"),
    ("traders", "07_traders.csv"),
]


def export_csv(conn: sqlite3.Connection, table: str = None):
    """Export one or all tables to CSV. Streams rows to avoid memory issues on large tables."""
    tables = EXPORT_TABLES if table is None else [(t, f) for t, f in EXPORT_TABLES if t == table]

    for tbl, filename in tables:
        path = BASE_DIR / filename
        cursor = conn.execute(f"SELECT * FROM {tbl}")  # noqa: S608
        cols = [desc[0] for desc in cursor.description]

        first_row = cursor.fetchone()
        if not first_row:
            logger.info("[export] %s -- empty, skipping", tbl)
            continue

        count = 0
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerow(first_row)
            count = 1
            while True:
                batch = cursor.fetchmany(10000)
                if not batch:
                    break
                writer.writerows(batch)
                count += len(batch)

        logger.info("[export] %s -> %s (%d rows)", tbl, filename, count)


# ===================================================================
# Main
# ===================================================================

STEPS = {
    "markets": lambda conn, sess: collect_markets(conn, sess),
    "trades": lambda conn, sess: collect_trades(conn, sess),
    "prices": lambda conn, sess: collect_price_histories(conn, sess),
    "orderbooks": lambda conn, sess: collect_orderbooks(conn, sess),
    "btc_spot": lambda conn, sess: collect_btc_spot(conn, sess),
    "resolutions": lambda conn, sess: compute_resolutions(conn),
    "traders": lambda conn, sess: compute_traders(conn),
}

STEP_ORDER = ["markets", "trades", "prices", "orderbooks", "btc_spot", "resolutions", "traders"]


def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m historical data scraper")
    parser.add_argument("--only", type=str, default=None,
                        help="Comma-separated list of steps to run (e.g., 'trades,prices')")
    parser.add_argument("--export-only", action="store_true",
                        help="Skip scraping, just export existing DB to CSV")
    parser.add_argument("--since", type=str, default=None,
                        help="Only collect markets ending after this date (YYYY-MM-DD or '30d' for relative)")
    args = parser.parse_args()

    # Parse --since into global SINCE_DATE
    global SINCE_DATE
    if args.since:
        if args.since.endswith("d"):
            from datetime import timedelta
            days = int(args.since[:-1])
            SINCE_DATE = datetime.now(timezone.utc) - timedelta(days=days)
        else:
            SINCE_DATE = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    BASE_DIR.mkdir(parents=True, exist_ok=True)

    conn = init_db()
    session = make_session()

    t_start = time.time()
    logger.info("=" * 60)
    logger.info("Polymarket BTC 5m Scraper -- Starting")
    logger.info("Database: %s", DB_PATH)
    if SINCE_DATE:
        logger.info("Date filter: only markets ending after %s", SINCE_DATE.strftime("%Y-%m-%d"))
    logger.info("=" * 60)

    if args.export_only:
        export_csv(conn)
        conn.close()
        return

    steps_to_run = STEP_ORDER
    if args.only:
        steps_to_run = [s.strip() for s in args.only.split(",")]
        invalid = [s for s in steps_to_run if s not in STEPS]
        if invalid:
            logger.error("Unknown steps: %s. Valid: %s", invalid, list(STEPS.keys()))
            sys.exit(1)

    for step in steps_to_run:
        if _shutdown:
            logger.warning("Shutdown requested -- stopping")
            break
        logger.info("--- Running step: %s ---", step)
        try:
            STEPS[step](conn, session)
        except Exception as e:
            logger.error("Step '%s' failed: %s", step, e, exc_info=True)
            continue

        # Export CSV after each step completes
        table_name = step
        if step == "prices":
            table_name = "price_histories"
        export_csv(conn, table=table_name)

    elapsed = time.time() - t_start
    logger.info("=" * 60)
    logger.info("ALL DONE -- %.1f minutes (%.0f seconds)", elapsed / 60, elapsed)

    # Final summary
    for tbl, _ in EXPORT_TABLES:
        count = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]  # noqa: S608
        logger.info("  %s: %d rows", tbl, count)

    logger.info("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
