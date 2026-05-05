"""
scrap/config.py -- Shared configuration module for the multi-asset Polymarket scraper.

Provides asset definitions, API endpoints, rate limiters, HTTP helpers,
database initialization, progress tracking, and graceful shutdown support.

All per-asset scraper scripts (01 through 07) and the shell orchestrator
import from this module.
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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent / "data"

# ---------------------------------------------------------------------------
# Asset definitions
# ---------------------------------------------------------------------------
ASSETS = {
    "btc": {
        "slug_prefix": "btc-updown-5m-",
        "binance_symbol": "BTCUSDT",
        "db_name": "btc5m.db",
    },
    "eth": {
        "slug_prefix": "eth-updown-5m-",
        "binance_symbol": "ETHUSDT",
        "db_name": "eth5m.db",
    },
    "sol": {
        "slug_prefix": "sol-updown-5m-",
        "binance_symbol": "SOLUSDT",
        "db_name": "sol5m.db",
    },
    "xrp": {
        "slug_prefix": "xrp-updown-5m-",
        "binance_symbol": "XRPUSDT",
        "db_name": "xrp5m.db",
    },
    "doge": {
        "slug_prefix": "doge-updown-5m-",
        "binance_symbol": "DOGEUSDT",
        "db_name": "doge5m.db",
    },
}

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_PRICES_URL = "https://clob.polymarket.com/prices-history"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
DATA_TRADES_URL = "https://data-api.polymarket.com/trades"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# ---------------------------------------------------------------------------
# Rate limits (divided by 5 for parallel safety -- 5 assets at once)
# ---------------------------------------------------------------------------
RATE_GAMMA_EVENTS = 4       # 40 / 5 / 2 -- extra margin, shared endpoint
RATE_GAMMA_MARKETS = 3      # 25 / 5 / ~2 -- extra margin, shared endpoint
RATE_CLOB_PRICES = 16       # 80 / 5
RATE_CLOB_MARKET = 24       # 120 / 5
RATE_DATA_TRADES = 3        # 15 / 5
RATE_BINANCE = 10           # per-symbol, no contention

# ---------------------------------------------------------------------------
# Logging (configured per-asset by init_logging)
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def init_logging(asset: str) -> logging.Logger:
    """Configure and return a logger for the given asset.

    Logs to stderr (stdout is reserved for JSON progress) and to
    ``data/{asset}_errors.log``.
    """
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    error_log = BASE_DIR / f"{asset}_errors.log"

    logger = logging.getLogger(f"scrap.{asset}")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(sh)
        fh = logging.FileHandler(error_log, mode="a")
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(fh)
    return logger


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def is_shutdown() -> bool:
    """Check whether a graceful shutdown has been requested."""
    return _shutdown


def _handle_signal(signum, frame):
    global _shutdown
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
            time.sleep(self.interval - elapsed)
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
    """Create an HTTP session with standard headers."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "polymarket-crypto5m-scraper/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


# ---------------------------------------------------------------------------
# Resilient HTTP GET with backoff
# ---------------------------------------------------------------------------


def resilient_get(
    session: requests.Session,
    url: str,
    params: dict,
    limiter: RateLimiter,
    max_retries: int = 8,
    allow_404: bool = False,
    logger: logging.Logger | None = None,
) -> requests.Response | None:
    """GET with rate limiting, retries, and exponential backoff."""
    if logger is None:
        logger = logging.getLogger("scrap")
    backoff = 5.0
    for attempt in range(max_retries):
        if _shutdown:
            return None
        limiter.wait()
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = min(backoff, 60)
                logger.warning(
                    "429 on %s -- waiting %.1fs (attempt %d)",
                    url, wait, attempt + 1,
                )
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
                logger.warning(
                    "%d on %s -- waiting %.1fs",
                    resp.status_code, url, wait,
                )
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
            logger.warning(
                "Connection error on %s: %s -- retry %d",
                url, e, attempt + 1,
            )
            time.sleep(backoff)
            backoff *= 2
    logger.error("Failed after %d retries: %s params=%s", max_retries, url, params)
    return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db_path(asset: str) -> Path:
    """Return the SQLite database path for the given asset."""
    return BASE_DIR / ASSETS[asset]["db_name"]


def init_db(asset: str) -> sqlite3.Connection:
    """Create / open the asset database with WAL mode and all tables.

    Tables mirror scrape.py schema with two additions:
    - ``spot`` (generic, replaces ``btc_spot``)
    - ``ws_trades`` and ``ws_spot`` for WebSocket capture
    """
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    db_path = get_db_path(asset)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64 MB cache

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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_match_time ON trades(match_time)"
    )

    # -- 03 price_histories
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_histories (
            condition_id TEXT,
            timestamp INTEGER,
            yes_price REAL,
            no_price REAL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ph_condition ON price_histories(condition_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ph_timestamp ON price_histories(timestamp)"
    )

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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ob_condition ON orderbooks(condition_id)"
    )

    # -- 05 spot (generic, replaces btc_spot)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spot (
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

    # -- WebSocket captured trades
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ws_trades (
            timestamp TEXT,
            slug TEXT,
            side TEXT,
            outcome TEXT,
            price REAL,
            size REAL,
            source TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_trades_ts ON ws_trades(timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_trades_slug ON ws_trades(slug)"
    )

    # -- WebSocket captured spot ticks
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ws_spot (
            timestamp TEXT,
            price REAL,
            size REAL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_spot_ts ON ws_spot(timestamp)"
    )

    conn.commit()
    return conn


def get_progress(conn: sqlite3.Connection, dataset: str) -> dict:
    """Read scrape_progress for a dataset."""
    row = conn.execute(
        "SELECT last_offset, last_condition_id, items_collected "
        "FROM scrape_progress WHERE dataset_name=?",
        (dataset,),
    ).fetchone()
    if row:
        return {
            "last_offset": row[0],
            "last_condition_id": row[1],
            "items_collected": row[2],
        }
    return {"last_offset": 0, "last_condition_id": "", "items_collected": 0}


def set_progress(
    conn: sqlite3.Connection,
    dataset: str,
    offset: int = 0,
    condition_id: str = "",
    items: int = 0,
):
    """Write scrape_progress for a dataset."""
    conn.execute(
        """
        INSERT INTO scrape_progress
            (dataset_name, last_offset, last_condition_id, items_collected, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(dataset_name) DO UPDATE SET
            last_offset=excluded.last_offset,
            last_condition_id=excluded.last_condition_id,
            items_collected=excluded.items_collected,
            updated_at=excluded.updated_at
        """,
        (dataset, offset, condition_id, items, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CLI argument parser helper
# ---------------------------------------------------------------------------


def parse_asset_arg() -> str:
    """Parse --asset argument, validate it's a known asset."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True, choices=ASSETS.keys())
    args = parser.parse_args()
    return args.asset


# ---------------------------------------------------------------------------
# JSON progress output helper
# ---------------------------------------------------------------------------


def report_progress(
    asset: str,
    dataset: str,
    done: int,
    total: int,
    msg: str = "",
):
    """Print JSON progress line to stdout for the shell orchestrator."""
    print(
        json.dumps({
            "asset": asset,
            "dataset": dataset,
            "done": done,
            "total": total,
            "msg": msg,
        }),
        flush=True,
    )
