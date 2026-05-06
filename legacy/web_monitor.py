#!/usr/bin/env python3
"""Live fair price web monitor — Polymarket vs Model.

Single-file web app: Python backend (aiohttp) + embedded HTML/JS frontend.
Uses TradingView Lightweight Charts for the price chart.

    python web_monitor.py              # open http://localhost:3000
    python web_monitor.py --port 9000
"""

import argparse
import asyncio
import json
import math
import time
from collections import deque

import aiohttp
from aiohttp import web

try:
    import websockets
except ImportError:
    raise SystemExit("pip install websockets")

# ── constants ──────────────────────────────────────────────────────────────

WS_RTDS = "wss://ws-live-data.polymarket.com"
WS_BINANCE = "wss://stream.binance.com:9443/ws/btcusdt@trade"
SLUG_PREFIX = "btc-updown-5m-"
MARKET_DURATION = 300
SECONDS_PER_YEAR = 365.25 * 24 * 3600
EWMA_LAMBDA = 0.997   # half-life ≈ 231 ticks × 5s ≈ 19 min (good for 5-min options)
EWMA_DELTA = 5.0
EWMA_WARMUP = 10
BROADCAST_HZ = 2

# ── math (inlined for hot path) ───────────────────────────────────────────


def norm_cdf(x):
    return 0.5 * math.erfc(-x / 1.4142135623730951)


def fair_price_up(spot, strike, sigma, t_remaining_s):
    if t_remaining_s <= 0:
        return 1.0 if spot > strike else (0.0 if spot < strike else 0.5)
    if sigma <= 1e-12 or spot == strike:
        return 1.0 if spot > strike else (0.0 if spot < strike else 0.5)
    tau = t_remaining_s / SECONDS_PER_YEAR
    d2 = math.log(spot / strike) / (sigma * math.sqrt(tau))
    return norm_cdf(d2)


class VarianceEstimator:
    """EWMA variance with Garman-Klass historical warmup.

    On startup, fetch_history() pulls 120 1-minute OHLC candles from
    Binance REST API and computes a Garman-Klass realized variance to
    seed the EWMA. The live EWMA then takes over seamlessly.

    Garman-Klass uses all four OHLC prices per candle, giving ~5x more
    efficient variance estimates than close-to-close squared returns.

    GK per candle: 0.5 * ln(H/L)^2 - (2*ln2 - 1) * ln(C/O)^2
    """

    BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

    __slots__ = ("lam", "delta", "warmup", "ann",
                 "_pp", "_pt", "_var", "_n", "_ready",
                 "_gk_sigma", "_history_prices")

    def __init__(self):
        self.lam = EWMA_LAMBDA
        self.delta = EWMA_DELTA
        self.warmup = EWMA_WARMUP
        self.ann = SECONDS_PER_YEAR / EWMA_DELTA
        self._pp = 0.0
        self._pt = 0.0
        self._var = 0.0
        self._n = 0
        self._ready = False
        self._gk_sigma = None
        self._history_prices = []  # close prices from warmup candles

    async def fetch_history(self):
        """Fetch 120 1-min candles from Binance and compute GK variance."""
        import aiohttp as _aiohttp
        url = f"{self.BINANCE_KLINES}?symbol=BTCUSDT&interval=1m&limit=120"
        print("[EWMA] Fetching 120 1-min BTC candles from Binance...")
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        print(f"[EWMA] Binance API returned {resp.status}, using cold start")
                        return
                    candles = await resp.json()
        except Exception as e:
            print(f"[EWMA] Failed to fetch history: {e}, using cold start")
            return

        if not candles or len(candles) < 10:
            print("[EWMA] Too few candles, using cold start")
            return

        # ── Garman-Klass variance from OHLC ──
        # Candle format: [open_time_ms, open, high, low, close, volume, ...]
        gk_sum = 0.0
        n_candles = 0
        closes = []
        for c in candles:
            try:
                o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            except (IndexError, ValueError):
                continue
            if o <= 0 or h <= 0 or l <= 0 or cl <= 0:
                continue
            ln_hl = math.log(h / l)
            ln_co = math.log(cl / o)
            gk = 0.5 * ln_hl * ln_hl - (2.0 * math.log(2) - 1.0) * ln_co * ln_co
            gk_sum += gk
            n_candles += 1
            closes.append((float(c[0]) / 1000.0, cl))  # (ts_s, close)

        if n_candles < 10:
            print("[EWMA] Not enough valid candles")
            return

        # GK variance is per-candle (1-min intervals)
        gk_var_per_min = gk_sum / n_candles
        gk_ann = SECONDS_PER_YEAR / 60.0  # annualize from 1-min
        self._gk_sigma = math.sqrt(gk_var_per_min * gk_ann)

        # ── Seed EWMA by replaying close prices ──
        # 1-min candles are at 60s intervals; EWMA expects 5s intervals.
        # Replay at native 60s, then scale variance to 5s equivalent.
        candle_dt = 60.0  # seconds between candles
        self._history_prices = closes
        for ts, price in closes:
            if self._pp > 0:
                r = math.log(price / self._pp)
                r2 = r * r
                self._var = r2 if self._n == 0 else self.lam * self._var + (1.0 - self.lam) * r2
                self._n += 1
            self._pp = price
            self._pt = ts

        # Scale variance from per-60s to per-5s (variance scales linearly with time)
        if self._n > 0:
            self._var *= (self.delta / candle_dt)

        self._ready = self._n >= self.warmup
        ewma_sig = self.sigma()
        print(
            f"[EWMA] Warmed up from {n_candles} candles: "
            f"GK σ={self._gk_sigma:.1%}, EWMA σ={ewma_sig:.1%} ({self._n} obs), "
            f"last BTC=${closes[-1][1]:,.2f}"
        )

    async def refresh_gk(self):
        """Re-fetch recent candles and update GK sigma (called periodically)."""
        import aiohttp as _aiohttp
        url = f"{self.BINANCE_KLINES}?symbol=BTCUSDT&interval=1m&limit=60"
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    candles = await resp.json()
        except Exception:
            return
        if not candles or len(candles) < 10:
            return
        gk_sum = 0.0
        n = 0
        for c in candles:
            try:
                o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            except (IndexError, ValueError):
                continue
            if o <= 0 or h <= 0 or l <= 0 or cl <= 0:
                continue
            ln_hl = math.log(h / l)
            ln_co = math.log(cl / o)
            gk_sum += 0.5 * ln_hl * ln_hl - (2.0 * math.log(2) - 1.0) * ln_co * ln_co
            n += 1
        if n >= 10:
            self._gk_sigma = math.sqrt((gk_sum / n) * (SECONDS_PER_YEAR / 60.0))

    def update(self, price, ts):
        """Feed a live tick. Returns annualized sigma (always available after warmup)."""
        if self._pt and ts - self._pt < self.delta:
            return self.sigma()
        if self._pp > 0:
            r = math.log(price / self._pp)
            r2 = r * r
            self._var = r2 if self._n == 0 else self.lam * self._var + (1.0 - self.lam) * r2
            self._n += 1
            self._ready = self._ready or self._n >= self.warmup
        self._pp = price
        self._pt = ts
        return self.sigma()

    def sigma(self):
        """Best available sigma: EWMA if ready, else GK from history, else None."""
        if self._ready:
            return math.sqrt(self._var * self.ann)
        if self._gk_sigma is not None:
            return self._gk_sigma
        return None


# ── shared state ───────────────────────────────────────────────────────────


class LiveState:
    def __init__(self):
        self.btc = 0.0
        self.btc_ts = 0.0
        self.sigma = None
        self.slug = None
        self.t_zero = None
        self.strike = None
        self.mkt_price = None  # Up token
        self.mkt_ts = 0.0
        self.fair = None
        self.trade_count = 0
        self.trades = deque(maxlen=50)
        self.chart_mkt = []  # [(epoch_s, price)]
        self.chart_mdl = []
        self.chart_btc = []
        self.bin_ok = False
        self.rtds_ok = False
        self._prev_t_zero = None
        # Per-second BTC price log for accurate strike capture
        self._btc_by_second = {}  # {int(epoch_s): price}

    def log_btc(self, price, ts):
        """Record BTC price keyed by second for strike lookup."""
        self._btc_by_second[int(ts)] = price
        # Keep last 600s (two market windows)
        if len(self._btc_by_second) > 600:
            cutoff = int(ts) - 600
            self._btc_by_second = {k: v for k, v in self._btc_by_second.items() if k >= cutoff}

    def _lookup_btc_at(self, epoch_s):
        """Find BTC price at a specific second. Checks per-second log,
        then historical candles, then falls back to current price."""
        t = int(epoch_s)
        # 1) Exact or near match in live log
        for offset in range(0, 10):
            if t + offset in self._btc_by_second:
                return self._btc_by_second[t + offset]
            if t - offset in self._btc_by_second:
                return self._btc_by_second[t - offset]
        # 2) Historical candles from warmup
        if ewma._history_prices:
            best_ts, best_p = None, None
            for cts, cp in ewma._history_prices:
                if best_ts is None or abs(cts - epoch_s) < abs(best_ts - epoch_s):
                    best_ts, best_p = cts, cp
            if best_ts is not None and abs(best_ts - epoch_s) < 120:
                return best_p
        # 3) Fallback to current
        return self.btc if self.btc > 0 else None

    def detect_market(self):
        now = time.time()
        tz = int(now // MARKET_DURATION) * MARKET_DURATION
        if tz != self._prev_t_zero:
            self._prev_t_zero = tz
            self.t_zero = tz
            self.slug = f"{SLUG_PREFIX}{tz}"
            # Look up the actual BTC price at market open
            self.strike = self._lookup_btc_at(tz)
            self.mkt_price = None
            self.chart_mkt.clear()
            self.chart_mdl.clear()
            self.chart_btc.clear()

    def offset(self):
        return time.time() - self.t_zero if self.t_zero else None

    def remaining(self):
        o = self.offset()
        return max(0.0, MARKET_DURATION - o) if o is not None else None

    def recompute(self):
        if not (self.btc and self.strike):
            return
        r = self.remaining()
        if r is None:
            return
        sig = self.sigma or ewma.sigma()
        if not sig:
            return
        self.fair = fair_price_up(self.btc, self.strike, sig, r)

    def snapshot(self):
        now = time.time()
        r = self.remaining()
        o = self.offset()
        edge = None
        if self.mkt_price is not None and self.fair is not None:
            edge = round(self.fair - self.mkt_price, 4)
        return {
            "type": "tick",
            "ts": round(now, 3),
            "slug": self.slug,
            "t_zero": self.t_zero,
            "offset": round(o, 1) if o is not None else None,
            "remaining": round(r, 1) if r is not None else None,
            "btc": round(self.btc, 2) if self.btc else None,
            "strike": round(self.strike, 2) if self.strike else None,
            "sigma": round(self.sigma, 4) if self.sigma else None,
            "market": round(self.mkt_price, 4) if self.mkt_price is not None else None,
            "fair": round(self.fair, 4) if self.fair is not None else None,
            "edge": edge,
            "trades": self.trade_count,
            "bin_ok": self.bin_ok,
            "rtds_ok": self.rtds_ok,
            "btc_lat_ms": int((now - self.btc_ts) * 1000) if self.btc_ts else None,
            "mkt_lat_ms": int((now - self.mkt_ts) * 1000) if self.mkt_ts else None,
            "chart_mkt": self.chart_mkt[-600:],
            "chart_mdl": self.chart_mdl[-600:],
            "chart_btc": self.chart_btc[-600:],
            "recent_trades": list(self.trades)[:20],
            "ewma_n": ewma._n,
            "ewma_warmup": ewma.warmup,
            "ewma_ready": ewma._ready,
            "gk_sigma": round(ewma._gk_sigma, 4) if ewma._gk_sigma else None,
        }


# ── WebSocket feeds ────────────────────────────────────────────────────────

state = LiveState()
ewma = VarianceEstimator()
clients = set()


async def binance_feed():
    while True:
        try:
            async with websockets.connect(WS_BINANCE, ping_interval=20) as ws:
                state.bin_ok = True
                print("[BIN] Connected to Binance")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        if msg.get("e") == "trade":
                            p = float(msg["p"])
                            ts = float(msg["T"]) / 1000.0
                            state.btc = p
                            state.btc_ts = ts
                            state.log_btc(p, ts)
                            sig = ewma.update(p, ts)
                            if sig is not None:
                                state.sigma = sig
                            state.recompute()
                    except (KeyError, ValueError):
                        pass
        except (websockets.ConnectionClosed, OSError):
            state.bin_ok = False
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break


async def rtds_feed():
    while True:
        try:
            async with websockets.connect(WS_RTDS) as ws:
                state.rtds_ok = True
                print("[RTDS] Connected to Polymarket")
                sub = {"action": "subscribe",
                       "subscriptions": [{"topic": "activity", "type": "orders_matched"}]}
                await ws.send(json.dumps(sub, separators=(",", ":")))

                async def ping():
                    while True:
                        try:
                            await ws.send("ping")
                            await asyncio.sleep(5)
                        except websockets.ConnectionClosed:
                            break

                pt = asyncio.create_task(ping())
                try:
                    async for raw in ws:
                        if not isinstance(raw, str) or "payload" not in raw:
                            continue
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        payload = msg.get("payload")
                        if not payload:
                            continue
                        slug = payload.get("slug", "") or payload.get("eventSlug", "")
                        if not slug.startswith(SLUG_PREFIX):
                            continue
                        slug_tz = int(slug.split("-")[-1])
                        if state.t_zero and slug_tz == state.t_zero:
                            outcome = payload.get("outcome", "")
                            try:
                                price = float(payload.get("price", 0))
                                size = float(payload.get("size", 0))
                            except (ValueError, TypeError):
                                continue
                            if outcome in ("Up", "Yes"):
                                state.mkt_price = price
                            elif outcome in ("Down", "No"):
                                state.mkt_price = 1.0 - price
                            state.mkt_ts = time.time()
                            state.trade_count += 1
                            side = payload.get("side", "?")
                            usdc = round(price * size, 2)
                            state.trades.appendleft({
                                "side": side, "outcome": outcome,
                                "price": round(price, 4), "size": round(size, 1),
                                "usdc": usdc, "ts": time.time(),
                            })
                finally:
                    pt.cancel()
        except (websockets.ConnectionClosed, OSError):
            state.rtds_ok = False
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break


async def sampler():
    """Sample chart data at 1 Hz."""
    while True:
        await asyncio.sleep(1)
        now = time.time()
        o = state.offset()
        if o is None or o < 0 or o > MARKET_DURATION + 5:
            continue
        epoch = int(now)
        if state.mkt_price is not None:
            state.chart_mkt.append([epoch, round(state.mkt_price, 4)])
        if state.fair is not None:
            state.chart_mdl.append([epoch, round(state.fair, 4)])
        elif state.btc and state.strike:
            # Even before EWMA warms up, show fair=0.5 baseline
            state.chart_mdl.append([epoch, 0.5])
        if state.btc > 0:
            state.chart_btc.append([epoch, round(state.btc, 2)])


async def gk_refresher():
    """Refresh GK sigma from Binance candles every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        await ewma.refresh_gk()
        if ewma._gk_sigma:
            print(f"[GK] Refreshed: σ={ewma._gk_sigma:.1%}")


async def status_printer():
    """Print status to terminal every 10s for debugging."""
    while True:
        await asyncio.sleep(10)
        o = state.offset()
        off_s = f"T+{o:.0f}s" if o else "?"
        print(
            f"[STATUS] BTC=${state.btc:,.0f} σ={'%.1f%%' % (state.sigma*100) if state.sigma else 'warmup(%d/%d)' % (ewma._n, ewma.warmup)} "
            f"fair={state.fair and '%.3f' % state.fair or '?'} mkt={state.mkt_price and '%.3f' % state.mkt_price or '?'} "
            f"{off_s} clients={len(clients)} chart_pts={len(state.chart_mdl)}"
        )


async def broadcaster():
    """Push state to all browser clients at BROADCAST_HZ."""
    while True:
        await asyncio.sleep(1.0 / BROADCAST_HZ)
        state.detect_market()
        state.recompute()
        if not clients:
            continue
        snap = state.snapshot()
        data = json.dumps(snap)
        dead = []
        for ws in clients:
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)


# ── aiohttp handlers ──────────────────────────────────────────────────────


async def handle_index(request):
    return web.Response(text=HTML, content_type="text/html")


async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)
    # Send full state on connect
    try:
        await ws.send_str(json.dumps(state.snapshot()))
    except Exception:
        pass
    try:
        async for msg in ws:
            pass  # client doesn't send anything
    finally:
        clients.discard(ws)
    return ws


async def on_startup(app):
    # Fetch historical BTC candles to warm up EWMA before going live
    await ewma.fetch_history()
    # Set initial sigma on state from the warmed-up estimator
    sig = ewma.sigma()
    if sig:
        state.sigma = sig
    # Seed BTC price log from historical candles (for strike lookup)
    if ewma._history_prices:
        for ts, price in ewma._history_prices:
            state.log_btc(price, ts)
        state.btc = ewma._history_prices[-1][1]
        state.btc_ts = ewma._history_prices[-1][0]
    # Detect market and compute fair price — strike looked up from candle history
    state.detect_market()
    state.recompute()
    strike_src = "candle" if ewma._history_prices else "live"
    print(f"[INIT] slug={state.slug} strike=${state.strike and state.strike:,.2f} ({strike_src}) "
          f"btc=${state.btc:,.2f} fair={state.fair and '%.3f' % state.fair} σ={sig and '%.1f%%' % (sig*100)}")
    app["tasks"] = [
        asyncio.create_task(binance_feed()),
        asyncio.create_task(rtds_feed()),
        asyncio.create_task(sampler()),
        asyncio.create_task(broadcaster()),
        asyncio.create_task(gk_refresher()),
        asyncio.create_task(status_printer()),
    ]


async def on_cleanup(app):
    for t in app.get("tasks", []):
        t.cancel()


# ── HTML ───────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>BTC 5m Fair Price Monitor</title>
<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root {
  --bg: #0d1117; --card: #161b22; --border: #30363d;
  --text: #e6edf3; --muted: #7d8590; --dim: #484f58;
  --green: #3fb950; --red: #f85149; --cyan: #58a6ff;
  --orange: #d29922; --yellow: #e3b341;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:'SF Mono',Monaco,'Cascadia Code',monospace; font-size:13px; overflow:hidden; height:100vh; }

.container { display:grid; grid-template-columns:1fr 340px; grid-template-rows:auto 1fr auto; height:100vh; gap:0; }

/* ── header ── */
header { grid-column:1/-1; display:flex; align-items:center; justify-content:space-between;
  padding:10px 20px; background:var(--card); border-bottom:1px solid var(--border); }
header .title { display:flex; align-items:center; gap:12px; }
header .title h1 { font-size:18px; font-weight:700; }
header .title .slug { color:var(--muted); font-size:12px; }
header .timer { display:flex; align-items:center; gap:16px; }
header .timer .countdown { font-size:32px; font-weight:700; font-variant-numeric:tabular-nums; }
header .timer .countdown.warn { color:var(--orange); }
header .timer .countdown.crit { color:var(--red); }
.conn { display:flex; gap:8px; }
.conn .dot { width:8px; height:8px; border-radius:50%; background:var(--dim); }
.conn .dot.on { background:var(--green); }

/* ── main chart ── */
.chart-area { position:relative; background:var(--card); border-right:1px solid var(--border); min-height:0; }
#main-chart { width:100%; height:100%; }

/* ── sidebar ── */
.sidebar { display:flex; flex-direction:column; gap:0; overflow-y:auto; background:var(--bg); }
.panel { padding:14px 16px; border-bottom:1px solid var(--border); background:var(--card); }
.panel h3 { font-size:11px; text-transform:uppercase; letter-spacing:0.08em; color:var(--muted); margin-bottom:10px; }
.row { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px; }
.row .label { color:var(--muted); font-size:12px; }
.row .value { font-size:14px; font-weight:600; font-variant-numeric:tabular-nums; }
.row .value.green { color:var(--green); }
.row .value.red { color:var(--red); }
.row .value.cyan { color:var(--cyan); }
.row .value.orange { color:var(--orange); }

.edge-display { text-align:center; padding:16px; }
.edge-display .edge-val { font-size:36px; font-weight:700; font-variant-numeric:tabular-nums; }
.edge-display .signal { font-size:14px; font-weight:600; margin-top:6px; padding:4px 12px; border-radius:6px; display:inline-block; }
.edge-display .signal.buy-up { background:rgba(63,185,80,0.15); color:var(--green); }
.edge-display .signal.buy-down { background:rgba(248,81,73,0.15); color:var(--red); }
.edge-display .signal.neutral { background:rgba(125,133,144,0.15); color:var(--muted); }

.progress-bar { height:4px; background:var(--border); border-radius:2px; margin-top:8px; overflow:hidden; }
.progress-bar .fill { height:100%; border-radius:2px; transition:width 0.5s linear; }

/* ── trade feed ── */
.feed { grid-column:1/-1; background:var(--card); border-top:1px solid var(--border);
  max-height:180px; overflow-y:auto; padding:0; }
.feed table { width:100%; border-collapse:collapse; font-size:12px; }
.feed th { position:sticky; top:0; background:var(--card); text-align:left; padding:6px 12px;
  color:var(--muted); font-weight:500; text-transform:uppercase; letter-spacing:0.05em;
  border-bottom:1px solid var(--border); font-size:11px; }
.feed td { padding:4px 12px; border-bottom:1px solid rgba(48,54,61,0.4); font-variant-numeric:tabular-nums; }
.feed tr.buy td:first-child { color:var(--green); }
.feed tr.sell td:first-child { color:var(--red); }
.feed .age { color:var(--dim); }

/* ── legend ── */
.legend { display:flex; gap:16px; align-items:center; padding:4px 0; }
.legend-item { display:flex; align-items:center; gap:6px; font-size:12px; }
.legend-item .swatch { width:12px; height:3px; border-radius:1px; }
</style>
</head>
<body>
<div class="container">
  <!-- Header -->
  <header>
    <div class="title">
      <h1>Bitcoin Up or Down — 5 Minutes</h1>
      <span class="slug" id="slug">connecting...</span>
    </div>
    <div class="timer">
      <div class="conn">
        <div class="dot" id="dot-bin" title="Binance"></div>
        <div class="dot" id="dot-rtds" title="RTDS"></div>
      </div>
      <div class="countdown" id="countdown">--:--</div>
    </div>
  </header>

  <!-- Chart -->
  <div class="chart-area">
    <div id="main-chart"></div>
  </div>

  <!-- Sidebar -->
  <div class="sidebar">
    <!-- Edge -->
    <div class="panel edge-display">
      <h3>Edge (Model − Market)</h3>
      <div class="edge-val" id="edge-val">—</div>
      <div class="signal neutral" id="signal">WAITING</div>
      <div class="progress-bar"><div class="fill" id="progress" style="width:0%;background:var(--cyan)"></div></div>
    </div>

    <!-- Prices -->
    <div class="panel">
      <h3>Prices</h3>
      <div class="row"><span class="label">Market (Up)</span><span class="value green" id="v-mkt">—</span></div>
      <div class="row"><span class="label">Model Fair</span><span class="value cyan" id="v-fair">—</span></div>
      <div class="row"><span class="label">Market (Down)</span><span class="value red" id="v-mkt-down">—</span></div>
      <div class="legend">
        <div class="legend-item"><div class="swatch" style="background:var(--green)"></div>Market</div>
        <div class="legend-item"><div class="swatch" style="background:var(--cyan)"></div>Model</div>
      </div>
    </div>

    <!-- BTC -->
    <div class="panel">
      <h3>Bitcoin</h3>
      <div class="row"><span class="label">Current</span><span class="value" id="v-btc">—</span></div>
      <div class="row"><span class="label">Strike (t₀)</span><span class="value orange" id="v-strike">—</span></div>
      <div class="row"><span class="label">Δ Price</span><span class="value" id="v-delta">—</span></div>
      <div class="row"><span class="label">EWMA σ</span><span class="value" id="v-sigma">—</span></div>
    </div>

    <!-- Latency -->
    <div class="panel">
      <h3>Latency</h3>
      <div class="row"><span class="label">Binance tick</span><span class="value" id="v-blat">—</span></div>
      <div class="row"><span class="label">Market trade</span><span class="value" id="v-mlat">—</span></div>
      <div class="row"><span class="label">Trades seen</span><span class="value" id="v-trades">0</span></div>
    </div>
  </div>

  <!-- Trade Feed -->
  <div class="feed" id="feed">
    <table>
      <thead><tr><th>Side</th><th>Outcome</th><th>Price</th><th>Size</th><th>USDC</th><th>Age</th></tr></thead>
      <tbody id="feed-body"></tbody>
    </table>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

// ── Chart ────────────────────────────────────────────────────────────────
const chartEl = $('main-chart');
const chart = LightweightCharts.createChart(chartEl, {
  layout: { background:{color:'#161b22'}, textColor:'#7d8590', fontFamily:"monospace", fontSize:11 },
  grid: { vertLines:{color:'#21262d'}, horzLines:{color:'#21262d'} },
  rightPriceScale: { borderColor:'#30363d', scaleMargins:{top:0.05,bottom:0.05} },
  timeScale: { borderColor:'#30363d', timeVisible:true, secondsVisible:true },
});
const mktLine = chart.addLineSeries({ color:'#3fb950', lineWidth:2, title:'Market',
  priceFormat:{type:'price',precision:3,minMove:0.001}, lastValueVisible:true });
const mdlLine = chart.addLineSeries({ color:'#58a6ff', lineWidth:2, title:'Model',
  priceFormat:{type:'price',precision:3,minMove:0.001}, lastValueVisible:true });

function resizeChart(){ const r=chartEl.getBoundingClientRect(); chart.resize(r.width,r.height); }
window.addEventListener('resize', resizeChart);
setTimeout(resizeChart, 100);

// ── State ────────────────────────────────────────────────────────────────
let curTZ = null;
let msgCount = 0;

// ── WebSocket ────────────────────────────────────────────────────────────
function connect() {
  const ws = new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
  ws.onopen = () => console.log('WS connected');
  ws.onclose = () => { console.log('WS closed, reconnecting...'); setTimeout(connect, 1000); };
  ws.onerror = (e) => { console.error('WS error', e); ws.close(); };

  ws.onmessage = (evt) => {
    try {
      const d = JSON.parse(evt.data);
      msgCount++;

      // New market → reset
      if (d.t_zero && d.t_zero !== curTZ) {
        curTZ = d.t_zero;
        mktLine.setData([]);
        mdlLine.setData([]);
      }

      // Chart — always setData with full arrays (at most 300 points, trivial)
      if (d.chart_mdl && d.chart_mdl.length > 0)
        mdlLine.setData(d.chart_mdl.map(p => ({time:p[0], value:p[1]})));
      if (d.chart_mkt && d.chart_mkt.length > 0)
        mktLine.setData(d.chart_mkt.map(p => ({time:p[0], value:p[1]})));
      chart.timeScale().fitContent();

      // Header
      $('slug').textContent = d.slug || '...';
      $('dot-bin').className = 'dot' + (d.bin_ok ? ' on' : '');
      $('dot-rtds').className = 'dot' + (d.rtds_ok ? ' on' : '');
      if (d.remaining != null) {
        const m=Math.floor(d.remaining/60), s=Math.floor(d.remaining%60);
        const cd=$('countdown');
        cd.textContent = String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
        cd.className = 'countdown'+(d.remaining<30?' crit':d.remaining<60?' warn':'');
      }
      if (d.offset != null)
        $('progress').style.width = Math.min(100, d.offset/300*100)+'%';

      // Prices
      $('v-mkt').textContent = d.market!=null ? d.market.toFixed(3) : '—';
      $('v-fair').textContent = d.fair!=null ? d.fair.toFixed(3) : '—';
      $('v-mkt-down').textContent = d.market!=null ? (1-d.market).toFixed(3) : '—';

      // Edge
      const edgeEl=$('edge-val'), sigEl=$('signal');
      if (d.edge != null) {
        edgeEl.textContent = (d.edge>=0?'+':'')+d.edge.toFixed(3);
        edgeEl.style.color = d.edge>0.01?'var(--green)':d.edge<-0.01?'var(--red)':'var(--muted)';
        const absE=Math.abs(d.edge);
        if (absE>0.02) {
          sigEl.textContent = d.edge>0?'BUY UP ▲':'BUY DOWN ▼';
          sigEl.className = 'signal '+(d.edge>0?'buy-up':'buy-down');
        } else { sigEl.textContent='NO EDGE'; sigEl.className='signal neutral'; }
      } else { edgeEl.textContent='—'; sigEl.textContent='WAITING'; sigEl.className='signal neutral'; }

      // BTC
      $('v-btc').textContent = d.btc!=null ? '$'+d.btc.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
      $('v-strike').textContent = d.strike!=null ? '$'+d.strike.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
      if (d.btc && d.strike) {
        const delta=d.btc-d.strike, el=$('v-delta');
        el.textContent = (delta>=0?'+':'')+'$'+delta.toFixed(2)+' ('+((delta/d.strike)*10000).toFixed(1)+'bp)';
        el.className = 'value '+(delta>0?'green':delta<0?'red':'');
      }

      // Sigma
      if (d.sigma!=null) {
        const src = d.ewma_ready ? 'EWMA' : (d.gk_sigma ? 'GK' : '?');
        $('v-sigma').textContent = (d.sigma*100).toFixed(1)+'% ('+src+')';
      } else if (d.ewma_n!=null) {
        $('v-sigma').textContent = 'warming '+d.ewma_n+'/'+d.ewma_warmup;
      }

      // Latency
      $('v-blat').textContent = d.btc_lat_ms!=null ? d.btc_lat_ms+'ms' : '—';
      $('v-mlat').textContent = d.mkt_lat_ms!=null ? d.mkt_lat_ms+'ms' : '—';
      $('v-trades').textContent = d.trades||'0';

      // Trade feed
      if (d.recent_trades && d.recent_trades.length) {
        const now=Date.now()/1000, tbody=$('feed-body');
        let h='';
        for (const t of d.recent_trades) {
          const age=Math.floor(now-t.ts);
          h+='<tr class="'+(t.side==='BUY'?'buy':'sell')+'"><td>'+t.side+'</td><td>'+t.outcome+
            '</td><td>'+t.price.toFixed(3)+'</td><td>'+t.size.toFixed(0)+
            '</td><td>$'+t.usdc.toFixed(0)+'</td><td class="age">'+(age<60?age+'s':Math.floor(age/60)+'m')+'</td></tr>';
        }
        tbody.innerHTML = h;
      }

    } catch(e) { console.error('onmessage error:', e); }
  };
}
connect();
</script>
</body>
</html>
"""

# ── app ────────────────────────────────────────────────────────────────────


def build_app():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    parser = argparse.ArgumentParser(description="BTC 5m fair price web monitor")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    app = build_app()
    print(f"Starting fair price monitor at http://localhost:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
