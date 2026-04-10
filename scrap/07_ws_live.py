#!/usr/bin/env python3
"""
scrap/07_ws_live.py -- WebSocket live capture for ALL 5 assets simultaneously.

Runs two concurrent async tasks:
  1. Binance combined stream -- captures spot trade ticks for all 5 symbols
  2. Polymarket RTDS stream  -- captures matched order activity

Inserts into per-asset SQLite databases (ws_spot / ws_trades tables).
Buffers writes and flushes every 100 records or 5 seconds.
Auto-reconnects with exponential backoff on disconnect.

Usage:
    python scrap/07_ws_live.py
"""

import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrap.config import ASSETS, init_db, init_logging, is_shutdown

try:
    import websockets
except ImportError:
    print("Missing dependency: pip install websockets", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BINANCE_WS_URL = (
    "wss://stream.binance.com:9443/stream?streams="
    "btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade/dogeusdt@trade"
)

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
RTDS_SUBSCRIBE = {
    "action": "subscribe",
    "subscriptions": [{"topic": "activity", "type": "orders_matched"}],
}

PING_INTERVAL = 5           # seconds between keep-alive pings (RTDS)
FLUSH_INTERVAL = 5.0        # seconds -- max time before flushing buffers
FLUSH_SIZE = 100            # records -- flush when buffer reaches this size
PROGRESS_INTERVAL = 30.0    # seconds between JSON progress lines on stdout

MAX_BACKOFF = 60.0          # max reconnect delay

# Map Binance stream names to asset keys
STREAM_TO_ASSET = {
    "btcusdt@trade": "btc",
    "ethusdt@trade": "eth",
    "solusdt@trade": "sol",
    "xrpusdt@trade": "xrp",
    "dogeusdt@trade": "doge",
}

# Map Polymarket slug prefixes to asset keys
SLUG_PREFIXES = {v["slug_prefix"]: k for k, v in ASSETS.items()}

# ---------------------------------------------------------------------------
# Logging (shared "ws_live" logger writing to all_errors.log)
# ---------------------------------------------------------------------------

logger = init_logging("ws_live")

# ---------------------------------------------------------------------------
# Database connections (one per asset, opened at startup)
# ---------------------------------------------------------------------------

db_conns: dict = {}  # asset -> sqlite3.Connection

# ---------------------------------------------------------------------------
# Buffered insert state
# ---------------------------------------------------------------------------

# Buffers: asset -> list of row tuples
spot_buffers: dict[str, list] = {k: [] for k in ASSETS}
trade_buffers: dict[str, list] = {k: [] for k in ASSETS}

# Counters for progress reporting
counts = {
    "spot": {k: 0 for k in ASSETS},
    "trades": {k: 0 for k in ASSETS},
}

# Async event to signal shutdown
shutdown_event = asyncio.Event()


def _request_shutdown():
    """Set both sync flag and async event."""
    shutdown_event.set()


# Wire SIGINT/SIGTERM to our async-aware shutdown
def _signal_handler(signum, frame):
    logger.warning("Signal %s received -- initiating graceful shutdown", signum)
    _request_shutdown()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ---------------------------------------------------------------------------
# Buffer flush
# ---------------------------------------------------------------------------


def flush_buffers():
    """Flush all pending spot and trade inserts to their databases."""
    for asset in ASSETS:
        conn = db_conns.get(asset)
        if conn is None:
            continue

        buf = spot_buffers[asset]
        if buf:
            conn.executemany(
                "INSERT INTO ws_spot (timestamp, price, size) VALUES (?,?,?)",
                buf,
            )
            conn.commit()
            counts["spot"][asset] += len(buf)
            spot_buffers[asset] = []

        buf = trade_buffers[asset]
        if buf:
            conn.executemany(
                "INSERT INTO ws_trades (timestamp, slug, side, outcome, price, size, source) "
                "VALUES (?,?,?,?,?,?,?)",
                buf,
            )
            conn.commit()
            counts["trades"][asset] += len(buf)
            trade_buffers[asset] = []


def maybe_flush(asset: str):
    """Flush a single asset's buffers if either has reached FLUSH_SIZE."""
    conn = db_conns.get(asset)
    if conn is None:
        return

    if len(spot_buffers[asset]) >= FLUSH_SIZE:
        conn.executemany(
            "INSERT INTO ws_spot (timestamp, price, size) VALUES (?,?,?)",
            spot_buffers[asset],
        )
        conn.commit()
        counts["spot"][asset] += len(spot_buffers[asset])
        spot_buffers[asset] = []

    if len(trade_buffers[asset]) >= FLUSH_SIZE:
        conn.executemany(
            "INSERT INTO ws_trades (timestamp, slug, side, outcome, price, size, source) "
            "VALUES (?,?,?,?,?,?,?)",
            trade_buffers[asset],
        )
        conn.commit()
        counts["trades"][asset] += len(trade_buffers[asset])
        trade_buffers[asset] = []


# ---------------------------------------------------------------------------
# Slug matching helper
# ---------------------------------------------------------------------------


def _slug_to_asset(slug: str) -> str | None:
    """Return asset key if slug matches a known prefix, else None."""
    for prefix, asset in SLUG_PREFIXES.items():
        if slug.startswith(prefix):
            return asset
    return None


# ---------------------------------------------------------------------------
# Binance combined stream task
# ---------------------------------------------------------------------------


async def binance_stream():
    """Connect to Binance combined trade stream and buffer spot ticks."""
    backoff = 1.0
    while not shutdown_event.is_set():
        try:
            logger.info("[binance] Connecting to combined stream")
            async with websockets.connect(BINANCE_WS_URL) as ws:
                logger.info("[binance] Connected -- streaming spot ticks for all assets")
                backoff = 1.0  # reset on successful connect

                async for raw in ws:
                    if shutdown_event.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    stream = msg.get("stream", "")
                    asset = STREAM_TO_ASSET.get(stream)
                    if asset is None:
                        continue

                    data = msg.get("data", {})
                    epoch_ms = data.get("T")
                    price_str = data.get("p")
                    size_str = data.get("q")

                    if epoch_ms is None or price_str is None or size_str is None:
                        continue

                    ts_iso = datetime.fromtimestamp(
                        epoch_ms / 1000.0, tz=timezone.utc
                    ).isoformat()
                    price = float(price_str)
                    size = float(size_str)

                    spot_buffers[asset].append((ts_iso, price, size))
                    maybe_flush(asset)

        except websockets.ConnectionClosed as e:
            logger.warning("[binance] Connection closed: %s -- reconnecting in %.1fs", e, backoff)
        except Exception as e:
            logger.error("[binance] Error: %s -- reconnecting in %.1fs", e, backoff)

        if shutdown_event.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


# ---------------------------------------------------------------------------
# Polymarket RTDS stream task
# ---------------------------------------------------------------------------


async def rtds_stream():
    """Connect to Polymarket RTDS WebSocket and buffer matched trades."""
    backoff = 1.0
    while not shutdown_event.is_set():
        try:
            logger.info("[rtds] Connecting to %s", RTDS_WS_URL)
            async with websockets.connect(RTDS_WS_URL) as ws:
                logger.info("[rtds] Connected -- subscribing to orders_matched")
                await ws.send(json.dumps(RTDS_SUBSCRIBE, separators=(",", ":")))
                backoff = 1.0

                # Keep-alive ping loop
                async def ping_loop():
                    while not shutdown_event.is_set():
                        try:
                            await ws.send("ping")
                            await asyncio.sleep(PING_INTERVAL)
                        except websockets.ConnectionClosed:
                            break

                ping_task = asyncio.create_task(ping_loop())

                try:
                    async for raw in ws:
                        if shutdown_event.is_set():
                            break
                        if not isinstance(raw, str):
                            continue
                        if raw == "pong" or "payload" not in raw:
                            continue

                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        payload = msg.get("payload")
                        if not payload:
                            continue

                        slug = payload.get("slug", payload.get("eventSlug", ""))
                        asset = _slug_to_asset(slug)
                        if asset is None:
                            continue

                        # Parse timestamp
                        trade_ts = payload.get("timestamp", 0)
                        if trade_ts > 1e12:
                            ts_iso = datetime.fromtimestamp(
                                trade_ts / 1000.0, tz=timezone.utc
                            ).isoformat()
                        elif trade_ts > 0:
                            ts_iso = datetime.fromtimestamp(
                                trade_ts, tz=timezone.utc
                            ).isoformat()
                        else:
                            ts_iso = datetime.now(timezone.utc).isoformat()

                        side = payload.get("side", "")
                        outcome = payload.get("outcome", "")
                        try:
                            price = float(payload.get("price", 0))
                            size = float(payload.get("size", 0))
                        except (ValueError, TypeError):
                            price, size = 0.0, 0.0

                        trade_buffers[asset].append(
                            (ts_iso, slug, side, outcome, price, size, "rtds")
                        )
                        maybe_flush(asset)
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass

        except websockets.ConnectionClosed as e:
            logger.warning("[rtds] Connection closed: %s -- reconnecting in %.1fs", e, backoff)
        except Exception as e:
            logger.error("[rtds] Error: %s -- reconnecting in %.1fs", e, backoff)

        if shutdown_event.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


# ---------------------------------------------------------------------------
# Periodic flush + progress task
# ---------------------------------------------------------------------------


async def periodic_flush():
    """Flush buffers every FLUSH_INTERVAL seconds and report progress."""
    last_progress = time.monotonic()
    while not shutdown_event.is_set():
        await asyncio.sleep(FLUSH_INTERVAL)
        flush_buffers()

        now = time.monotonic()
        if now - last_progress >= PROGRESS_INTERVAL:
            last_progress = now
            total_spot = sum(counts["spot"].values())
            total_trades = sum(counts["trades"].values())
            total = total_spot + total_trades

            # Also count records still in buffers
            pending_spot = sum(len(b) for b in spot_buffers.values())
            pending_trades = sum(len(b) for b in trade_buffers.values())

            detail = ", ".join(
                f"{a}: {counts['spot'][a]}s+{counts['trades'][a]}t"
                for a in ASSETS
            )
            msg = f"spot={total_spot} trades={total_trades} pending={pending_spot+pending_trades} [{detail}]"
            print(
                json.dumps({
                    "asset": "all",
                    "dataset": "ws_live",
                    "done": total,
                    "total": 0,
                    "msg": msg,
                }),
                flush=True,
            )
            logger.info("[progress] %s", msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    """Open DBs, run Binance + RTDS + flush tasks concurrently."""
    logger.info("=" * 60)
    logger.info("[ws_live] Starting WebSocket live capture for all assets")
    logger.info("=" * 60)

    # Open DB connections for all 5 assets
    for asset in ASSETS:
        db_conns[asset] = init_db(asset)
        logger.info("[ws_live] Opened DB for %s", asset)

    # Run tasks concurrently
    tasks = [
        asyncio.create_task(binance_stream()),
        asyncio.create_task(rtds_stream()),
        asyncio.create_task(periodic_flush()),
    ]

    # Wait for shutdown signal
    await shutdown_event.wait()
    logger.info("[ws_live] Shutdown requested -- cancelling tasks")

    # Cancel all tasks
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Final flush
    logger.info("[ws_live] Final buffer flush")
    flush_buffers()

    # Report final counts
    total_spot = sum(counts["spot"].values())
    total_trades = sum(counts["trades"].values())
    total = total_spot + total_trades
    detail = ", ".join(
        f"{a}: {counts['spot'][a]}s+{counts['trades'][a]}t"
        for a in ASSETS
    )
    msg = f"done -- spot={total_spot} trades={total_trades} [{detail}]"
    print(
        json.dumps({
            "asset": "all",
            "dataset": "ws_live",
            "done": total,
            "total": total,
            "msg": msg,
        }),
        flush=True,
    )
    logger.info("[ws_live] %s", msg)

    # Close DB connections
    for asset, conn in db_conns.items():
        conn.close()
        logger.info("[ws_live] Closed DB for %s", asset)

    logger.info("[ws_live] Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
