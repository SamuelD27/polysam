#!/usr/bin/env python3
"""RTDS WebSocket monitor — streams live Polymarket trades to terminal.

Usage:
  python rtds_monitor.py                          # all trades, all markets
  python rtds_monitor.py --slug btc-updown-5m     # filter by market slug prefix
  python rtds_monitor.py --raw                    # print full JSON payloads
  python rtds_monitor.py --whales wallets.json    # highlight whale wallets
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import websockets
except ImportError:
    print("pip install websockets")
    sys.exit(1)

WS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL = 5


def load_whales(path: str) -> set[str]:
    p = Path(path)
    if not p.exists():
        print(f"whale file not found: {path}")
        return set()
    with open(p) as f:
        data = json.load(f)
    if isinstance(data, dict) and "wallets" in data:
        return {w.lower() for w in data["wallets"]}
    if isinstance(data, dict) and "active_whales" in data:
        return {w.lower() for w in data["active_whales"]}
    if isinstance(data, list):
        return {w.lower() for w in data}
    return set()


def format_trade(payload: dict, msg_ts: int, whale_set: set[str]) -> str:
    now_ms = int(time.time() * 1000)
    trade_ts = payload.get("timestamp", 0)
    # msg_ts is the envelope timestamp (ms from server)
    # trade_ts is the trade's own timestamp (could be seconds or ms)
    if trade_ts > 1e12:
        trade_epoch_ms = int(trade_ts)
    else:
        trade_epoch_ms = int(trade_ts * 1000)

    server_delay_ms = now_ms - msg_ts if msg_ts else "?"
    trade_age_ms = now_ms - trade_epoch_ms if trade_epoch_ms else "?"

    wallet = (payload.get("proxyWallet") or "")
    wallet_short = wallet[:10] if wallet else "???"
    is_whale = wallet.lower() in whale_set if wallet and whale_set else False
    whale_tag = " [WHALE]" if is_whale else ""

    side = payload.get("side", "?")
    outcome = payload.get("outcome", "?")
    try:
        price_f = float(payload.get("price", 0))
        size_f = float(payload.get("size", 0))
        usdc = price_f * size_f
    except (ValueError, TypeError):
        price_f, size_f, usdc = 0, 0, 0
    slug = payload.get("slug", payload.get("eventSlug", "?"))

    ts_str = datetime.fromtimestamp(trade_epoch_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")

    return (
        f"${usdc:>8.2f} | {ts_str} | {side:<4} {outcome:<4} | "
        f"p={price_f:<6.3f} sz={size_f:<8.1f} | "
        f"{slug:<40} | "
        f"{wallet_short}{whale_tag} | "
        f"ws_delay={server_delay_ms}ms"
    )


async def monitor(args):
    whale_set = load_whales(args.whales) if args.whales else set()
    if whale_set:
        print(f"Loaded {len(whale_set)} whale wallets")

    trade_count = 0
    connect_time = None

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                connect_time = time.time()
                print(f"Connected to {WS_URL}")

                # Subscribe
                sub = {
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "activity",
                        "type": "orders_matched",
                    }],
                }
                await ws.send(json.dumps(sub, separators=(',', ':')))
                print(f"Subscribed to activity/orders_matched (all markets)")
                print("-" * 120)

                # Ping loop
                async def ping_loop():
                    while True:
                        try:
                            await ws.send("ping")
                            await asyncio.sleep(PING_INTERVAL)
                        except websockets.ConnectionClosed:
                            break

                ping_task = asyncio.create_task(ping_loop())

                try:
                    async for raw in ws:
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

                        # Slug filter
                        if args.slug:
                            slug = payload.get("slug", "") or payload.get("eventSlug", "")
                            if not slug.startswith(args.slug):
                                continue

                        trade_count += 1
                        msg_ts = msg.get("timestamp", 0)

                        if args.raw:
                            payload["_ws_timestamp"] = msg_ts
                            payload["_local_recv_time"] = int(time.time() * 1000)
                            print(json.dumps(payload, indent=2))
                        else:
                            line = format_trade(payload, msg_ts, whale_set)
                            print(f"[{trade_count:>6}] {line}")

                finally:
                    ping_task.cancel()

        except websockets.ConnectionClosed:
            elapsed = time.time() - connect_time if connect_time else 0
            print(f"\nDisconnected after {elapsed:.0f}s ({trade_count} trades). Reconnecting in 2s...")
            await asyncio.sleep(2)
        except KeyboardInterrupt:
            print(f"\nStopped. {trade_count} trades received.")
            break
        except Exception as e:
            print(f"\nError: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Polymarket RTDS live trade monitor")
    parser.add_argument("--slug", default="btc-updown-5m", help="Filter by market slug prefix (default: btc-updown-5m)")
    parser.add_argument("--raw", action="store_true", help="Print full JSON payloads")
    parser.add_argument("--whales", help="Path to whale wallets JSON file")
    args = parser.parse_args()
    asyncio.run(monitor(args))


if __name__ == "__main__":
    main()
