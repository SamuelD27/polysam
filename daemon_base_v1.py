"""daemon_base_v1 -- Headless paper trading daemon for BTC 5m binary options.

Connects to Binance + Polymarket RTDS websockets, computes GBM fair prices
for BTC 5-minute binary options, takes fictitious (paper) trades based on
edge vs market price, and persists state to JSON for monitoring.

Runs TWO strategies simultaneously on the same data:
  BASE:     fixed entry window (T+120..T+150), hold to resolution
  ENHANCED: time-zone entries + profit grabber + squeeze detector
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import websockets
except ImportError:
    print("pip install websockets")
    exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from active_bots.enhanced_strategy import EnhancedStrategy
from active_bots.execution import Executor, MarketCtx
from active_bots.execution.event_logger import EventLogger
from active_bots.execution.paper_executor import PaperExecutor
from active_bots.execution.risk_manager import RiskConfig, RiskManager

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── Constants ──────────────────────────────────────────────────────────────

EDGE_MIN = 0.10
EDGE_MAX = 0.25
MAX_RISK = 100.0
DEFAULT_MAX_BET_PCT = 0.20
SPREAD_COST = 0.01
ENTRY_OFFSET = 120
ENTRY_CUTOFF = 150
MARKET_DURATION = 300
SECONDS_PER_YEAR = 31557600.0
SHUTDOWN_TIMEOUT_S = 5.0

MAX_CLOSED_TRADES = 200

EWMA_LAMBDA = 0.94
EWMA_DELTA = 5.0
EWMA_WARMUP = 10

WS_BINANCE = "wss://stream.binance.com:9443/ws/btcusdt@trade"
WS_RTDS = "wss://ws-live-data.polymarket.com"
SLUG_PREFIX = "btc-updown-5m-"

STATE_DIR = Path(__file__).parent / "daemon_state"
STATE_FILE = STATE_DIR / "state.json"
PID_FILE = STATE_DIR / "daemon.pid"
LOG_FILE = STATE_DIR / "daemon.log"
EVENTS_FILE = STATE_DIR / "events.jsonl"

logger = logging.getLogger("daemon_base_v1")


def _env_float(name: str, default: float | None = None) -> float | None:
    """Parse a float env var, returning ``default`` on missing/invalid input.

    Logs a warning on invalid non-empty values instead of raising, so the
    long-running daemon can tolerate typos like ``PORTFOLIO_SIZE_USDC=1k``
    without crashing at startup.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r (expected float); ignoring", name, raw)
        return default


def compute_effective_max_risk() -> tuple[float, str]:
    """Resolve per-trade max bet from env. Returns (value, source).

    Order:
      1. PORTFOLIO_SIZE_USDC * MAX_BET_PCT (default 0.20)
      2. MAX_TRADE_SIZE_USDC absolute ceiling (wins if smaller)
      3. Fallback to MAX_RISK (100.0)

    Defensive parsing: invalid (non-float, non-positive) env values are
    treated as unset and logged. MAX_BET_PCT > 1.0 is also rejected as
    nonsense. When MAX_BET_PCT is invalid but PORTFOLIO_SIZE_USDC is valid,
    we fall back to the default pct (DEFAULT_MAX_BET_PCT = 0.20) so a bad
    pct does not disable portfolio sizing entirely.
    """
    portfolio = _env_float("PORTFOLIO_SIZE_USDC")
    if portfolio is not None and portfolio <= 0:
        logger.warning(
            "PORTFOLIO_SIZE_USDC=%s is non-positive; ignoring", portfolio,
        )
        portfolio = None

    candidate: float | None = None
    source = "default"

    if portfolio is not None:
        pct = _env_float("MAX_BET_PCT", DEFAULT_MAX_BET_PCT)
        if pct is None or pct <= 0 or pct > 1.0:
            if pct is not None:
                logger.warning(
                    "MAX_BET_PCT=%s out of range (0, 1]; using default %.2f",
                    pct, DEFAULT_MAX_BET_PCT,
                )
            pct = DEFAULT_MAX_BET_PCT
        candidate = portfolio * pct
        source = "portfolio"

    abs_cap = _env_float("MAX_TRADE_SIZE_USDC")
    if abs_cap is not None and abs_cap <= 0:
        logger.warning(
            "MAX_TRADE_SIZE_USDC=%s is non-positive; ignoring", abs_cap,
        )
        abs_cap = None

    if abs_cap is not None:
        if candidate is None or abs_cap < candidate:
            candidate = abs_cap
            source = "absolute"

    if candidate is None:
        return MAX_RISK, "default"
    return candidate, source


def build_executor() -> tuple[Executor, "TokenResolver | None", "RiskManager"]:
    """Assemble the executor stack from env vars.

    POLYMARKET_MODE=paper  -> PaperExecutor (no network)
    POLYMARKET_MODE=live   -> LiveExecutor (real CLOB orders)
    POLYMARKET_DRY_RUN=1   -> PaperExecutor (overrides mode)
    """
    mode = os.environ.get("POLYMARKET_MODE", "paper").lower()
    dry_run = os.environ.get("POLYMARKET_DRY_RUN", "0") == "1"

    effective_max, source = compute_effective_max_risk()

    max_daily_loss = _env_float("MAX_DAILY_LOSS_USDC", 300.0)
    if max_daily_loss is None or max_daily_loss <= 0:
        max_daily_loss = 300.0

    risk_cfg = RiskConfig(
        max_daily_loss_usdc=max_daily_loss,
        max_trade_size_usdc=effective_max,
        kill_switch_file=Path(
            os.environ.get("KILL_SWITCH_FILE", "daemon_state/KILL")
        ),
    )
    risk = RiskManager(risk_cfg)
    logger.info(
        "max_trade_size=%.2f source=%s (portfolio=%s pct=%s abs=%s)",
        effective_max, source,
        os.environ.get("PORTFOLIO_SIZE_USDC", "<unset>"),
        os.environ.get("MAX_BET_PCT", f"{DEFAULT_MAX_BET_PCT:.2f}"),
        os.environ.get("MAX_TRADE_SIZE_USDC", "<unset>"),
    )

    if dry_run:
        logger.info(
            "executor=paper (dry-run; POLYMARKET_MODE=%s ignored)",
            mode,
        )
        return PaperExecutor(), None, risk

    if mode != "live":
        logger.info("executor=paper (MAX_TRADE_SIZE=%.0f)", risk_cfg.max_trade_size_usdc)
        return PaperExecutor(), None, risk

    from active_bots.execution.clob_client_factory import (
        ClobFactoryError, build_client,
    )
    from active_bots.execution.live_executor import LiveExecutor
    from active_bots.execution.reconciler import Reconciler
    from active_bots.execution.token_resolver import TokenResolver

    try:
        client = build_client()
    except ClobFactoryError as e:
        logger.error("LIVE MODE REQUESTED BUT FAILED TO INIT CLIENT: %s", e)
        logger.error("Falling back to paper mode for safety")
        return PaperExecutor(), None, risk

    funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
    resolver = TokenResolver()
    reconciler = Reconciler(funder_address=funder) if funder else None

    logger.info(
        "executor=live funder=%s MAX_TRADE_SIZE=%.0f DAILY_LOSS=%.0f",
        funder, risk_cfg.max_trade_size_usdc, risk_cfg.max_daily_loss_usdc,
    )
    return (
        LiveExecutor(client, resolver, risk, reconciler=reconciler, dry_run=False),
        resolver,
        risk,
    )


# ── Logging setup ──────────────────────────────────────────────────────────

def setup_logging():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(str(LOG_FILE), maxBytes=5242880, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ── Inlined math (no external deps) ──────────────────────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / 1.4142135623730951)


def fair_price_up(spot: float, strike: float, sigma: float, t_remaining_s: float) -> float:
    if t_remaining_s <= 0:
        if spot > strike:
            return 1.0
        elif spot < strike:
            return 0.0
        return 0.5

    if sigma <= 1e-12:
        if spot > strike:
            return 1.0
        elif spot < strike:
            return 0.0
        return 0.5

    if spot == strike:
        return 0.5

    tau = t_remaining_s / SECONDS_PER_YEAR
    d2 = math.log(spot / strike) / (sigma * math.sqrt(tau))
    return norm_cdf(d2)


# ── Inlined EWMA ─────────────────────────────────────────────────────────

class EWMA:
    __slots__ = ("lam", "delta", "warmup", "ann", "_prev_price", "_prev_ts", "_var", "_n")

    def __init__(self):
        self.lam = EWMA_LAMBDA
        self.delta = EWMA_DELTA
        self.warmup = EWMA_WARMUP
        self.ann = SECONDS_PER_YEAR / EWMA_DELTA
        self._prev_price = 0.0
        self._prev_ts = 0.0
        self._var = 0.0
        self._n = 0

    def update(self, price: float, ts: float):
        if self._prev_ts and (ts - self._prev_ts) < self.delta:
            return self.sigma()
        if self._prev_price > 0:
            r = math.log(price / self._prev_price)
            r2 = r * r
            if self._n == 0:
                self._var = r2
            else:
                self._var = self.lam * self._var + (1.0 - self.lam) * r2
            self._n += 1
        self._prev_price = price
        self._prev_ts = ts
        return self.sigma()

    def sigma(self):
        if self._n < self.warmup:
            return None
        return math.sqrt(self._var * self.ann)


# ── DaemonState ─────────────────────────────────────────────────────────��─

class DaemonState:
    """All mutable state for the daemon, serializable to JSON."""

    def __init__(self):
        # Shared market data
        self.btc_price = 0.0
        self.btc_ts = 0.0
        self.sigma = None

        # Current market
        self.t_zero = None
        self.strike = None
        self.slug = None
        self.market_price_up = None
        self.market_price_ts = 0.0

        # Base strategy state
        self.base_fair_price = None
        self.base_position = None
        self.base_trades = []
        self.base_stats = {
            "total_pnl": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "max_drawdown": 0.0,
            "current_drawdown": 0.0,
            "high_water_mark": 0.0,
            "current_streak": 0,
            "streak_type": None,
            "start_time": time.time(),
            "total_risked": 0.0,
        }

        # Enhanced strategy state
        self.enh_fair_price = None
        self.enh_position = None
        self.enh_trades = []
        self.enh_stats = {
            "total_pnl": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "max_drawdown": 0.0,
            "current_drawdown": 0.0,
            "high_water_mark": 0.0,
            "current_streak": 0,
            "streak_type": None,
            "start_time": time.time(),
            "total_risked": 0.0,
        }
        self.enh_extra = {
            "squeeze_active": False,
            "spike_score": 0.0,
            "last_exit_type": None,
            "last_time_zone": None,
            "last_source": None,
            "tp_count": 0,
            "sl_count": 0,
            "resolution_count": 0,
            "edge_trades": 0,
            "squeeze_trades": 0,
        }

        self.connections = {"binance": False, "rtds": False}

    def current_offset(self) -> float:
        if not self.t_zero:
            return None
        return time.time() - self.t_zero

    def time_remaining(self) -> float:
        off = self.current_offset()
        return max(0.0, MARKET_DURATION - off) if off is not None else 0.0

    def detect_market(self) -> bool:
        """Detect current 5-min market from clock. Returns True if market rolled over."""
        now = time.time()
        t_zero = int(now // MARKET_DURATION) * MARKET_DURATION
        old_t_zero = self.t_zero

        if t_zero != self.t_zero:
            self.t_zero = t_zero
            self.slug = SLUG_PREFIX + str(t_zero)
            if self.btc_price > 0:
                self.strike = self.btc_price
            self.market_price_up = None
            self.market_price_ts = 0.0
            self.base_fair_price = None
            self.enh_fair_price = None
            return old_t_zero is not None
        return False

    def recompute_fair(self):
        if not all([self.btc_price, self.strike, self.sigma]):
            return
        tr = self.time_remaining()
        fair = fair_price_up(self.btc_price, self.strike, self.sigma, tr)
        self.base_fair_price = fair
        self.enh_fair_price = fair

    def to_dict(self) -> dict:
        return {
            "btc_price": self.btc_price,
            "btc_ts": self.btc_ts,
            "sigma": self.sigma,
            "t_zero": self.t_zero,
            "strike": self.strike,
            "slug": self.slug,
            "market_price_up": self.market_price_up,
            "market_price_ts": self.market_price_ts,
            "connections": self.connections,
            "base": {
                "fair_price": self.base_fair_price,
                "open_position": self.base_position,
                "closed_trades": self.base_trades,
                "stats": self.base_stats,
            },
            "enhanced": {
                "fair_price": self.enh_fair_price,
                "open_position": self.enh_position,
                "closed_trades": self.enh_trades,
                "stats": self.enh_stats,
                "extra": self.enh_extra,
            },
            "last_update": time.time(),
        }


# ── Websocket feeds ──────────────────────────────────────────────────────

async def binance_feed(state: DaemonState, ewma: EWMA):
    """Connect to Binance and update BTC price + EWMA sigma."""
    while True:
        try:
            async with websockets.connect(WS_BINANCE, ping_interval=20) as ws:
                state.connections["binance"] = True
                logger.info("Binance connected")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("e") != "trade":
                        continue
                    price = float(msg["p"])
                    ts = float(msg["T"]) / 1000.0
                    state.btc_price = price
                    state.btc_ts = ts
                    if state.strike and state.t_zero:
                        sig = ewma.update(price, ts)
                        if sig is not None and sig > 0:
                            state.sigma = sig
                        state.recompute_fair()
        except (websockets.ConnectionClosed, OSError, KeyError, ValueError) as e:
            state.connections["binance"] = False
            logger.warning("Binance disconnected: %s, reconnecting in 1s", e)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return


async def rtds_feed(state: DaemonState):
    """Connect to Polymarket RTDS and update market price."""
    while True:
        try:
            async with websockets.connect(WS_RTDS) as ws:
                state.connections["rtds"] = True
                logger.info("RTDS connected")

                sub = json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "activity",
                        "type": "orders_matched",
                    }],
                }, separators=(",", ":"))
                await ws.send(sub)

                async def ping():
                    while True:
                        await ws.send("ping")
                        await asyncio.sleep(5)

                ping_task = asyncio.create_task(ping())
                try:
                    async for raw in ws:
                        if isinstance(raw, str):
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                        else:
                            continue

                        payload = msg.get("payload")
                        if not payload:
                            continue

                        slug = payload.get("slug", "") or payload.get("eventSlug", "")
                        if not slug.startswith(SLUG_PREFIX):
                            continue

                        # Parse trade slug to check it matches current market
                        try:
                            trade_slug_tz = int(slug.split("-")[-1])
                            if trade_slug_tz != state.t_zero:
                                continue
                        except (ValueError, TypeError):
                            continue

                        outcome = payload.get("outcome", "")
                        price = payload.get("price")
                        if price is None:
                            continue
                        price = float(price)

                        if outcome in ("Up", "Yes"):
                            state.market_price_up = price
                        elif outcome in ("Down", "No"):
                            state.market_price_up = 1.0 - price
                        else:
                            continue

                        state.market_price_ts = time.time()
                finally:
                    ping_task.cancel()
                    # Best-effort: let it observe cancel. Don't await indefinitely.
                    try:
                        await asyncio.wait_for(ping_task, timeout=1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        pass

        except (websockets.ConnectionClosed, OSError) as e:
            state.connections["rtds"] = False
            logger.warning("RTDS disconnected: %s, reconnecting in 1s", e)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return


# ── Position sizing / resolution (base strategy) ─────────────────────────

def compute_position_size(
    edge: float, entry_price: float, max_risk: float = MAX_RISK,
) -> tuple[float, float]:
    """Compute position size. Returns (size_usdc, size_shares)."""
    if edge < EDGE_MIN:
        return (0.0, 0.0)
    frac = min((edge - EDGE_MIN) / (EDGE_MAX - EDGE_MIN), 1.0)
    size_usdc = frac * max_risk
    size_shares = size_usdc / entry_price
    return size_usdc, size_shares


def update_stats(stats: dict, trade: dict):
    """Update cumulative stats after a resolved trade."""
    stats["total_trades"] += 1
    stats["total_pnl"] = round(stats["total_pnl"] + trade["pnl"], 2)
    stats["total_risked"] = round(stats["total_risked"] + trade["size_usdc"], 2)

    if trade["won"]:
        stats["wins"] += 1
        if stats["streak_type"] == "W":
            stats["current_streak"] += 1
        else:
            stats["streak_type"] = "W"
            stats["current_streak"] = 1
    else:
        stats["losses"] += 1
        if stats["streak_type"] == "L":
            stats["current_streak"] += 1
        else:
            stats["streak_type"] = "L"
            stats["current_streak"] = 1

    if stats["total_pnl"] > stats["high_water_mark"]:
        stats["high_water_mark"] = stats["total_pnl"]

    stats["current_drawdown"] = round(stats["high_water_mark"] - stats["total_pnl"], 2)
    if stats["current_drawdown"] > stats["max_drawdown"]:
        stats["max_drawdown"] = stats["current_drawdown"]


# ── Strategy loop ─────────────────────────────────────────────────────────

async def strategy_loop(
    state: DaemonState, executor: Executor, resolver, events: EventLogger,
    max_risk: float = MAX_RISK,
):
    # BASE is a paper benchmark only — never trades live. Keeps the comparison
    # honest when the live daemon is running.
    base_executor: Executor = PaperExecutor()
    """Main strategy loop. Runs at 1 Hz.

    - Detects market rollover
    - Enters positions at ENTRY_OFFSET..ENTRY_CUTOFF (base) or via EnhancedStrategy
    - Resolves at market expiry (T+300)
    - Delegates order placement / fill bookkeeping to `executor`
    """
    logger.info(
        "Strategy loop started — enhanced=%s, base=paper (benchmark only), max_risk=%.2f",
        executor.mode, max_risk,
    )
    enh = EnhancedStrategy(max_risk=max_risk)
    market_ctx: MarketCtx | None = None

    def refresh_market_ctx() -> MarketCtx | None:
        if not state.slug or not state.t_zero or not state.strike:
            return None
        yes = no = None
        tick = 0.01
        if resolver is not None:
            tokens = resolver.resolve(state.slug)
            if tokens is not None:
                yes = tokens.yes_token_id
                no = tokens.no_token_id
                tick = tokens.tick_size
        return MarketCtx(
            slug=state.slug,
            t_zero=state.t_zero,
            strike=state.strike,
            yes_token_id=yes,
            no_token_id=no,
            tick_size=tick,
        )

    while True:
        try:
            now = time.time()
            rolled = state.detect_market()

            # ── On rollover, resolve any leftover positions ──
            if rolled:
                prev_ctx = market_ctx  # settlement belongs to the OLD market
                if state.base_position is not None:
                    if state.btc_price > 0 and prev_ctx is not None:
                        result = base_executor.resolve(
                            state.base_position, prev_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.base_stats, trade)
                            state.base_trades.append(trade)
                            if len(state.base_trades) > MAX_CLOSED_TRADES:
                                state.base_trades = state.base_trades[-MAX_CLOSED_TRADES:]
                            outcome = "WIN" if trade["won"] else "LOSS"
                            events.log("resolve", strategy="base", trade=trade, trigger="rollover")
                            logger.info(
                                "BASE RESOLVED [%s] %s %s PnL=%+.2f",
                                outcome, trade["side"], trade["slug"], trade["pnl"],
                            )
                    else:
                        logger.warning(
                            "Cannot resolve base position %s -- no BTC price or ctx",
                            state.base_position.get("slug"),
                        )
                    state.base_position = None

                if state.enh_position is not None:
                    if state.btc_price > 0 and prev_ctx is not None:
                        result = executor.resolve(
                            state.enh_position, prev_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.enh_stats, trade)
                            state.enh_trades.append(trade)
                            if len(state.enh_trades) > MAX_CLOSED_TRADES:
                                state.enh_trades = state.enh_trades[-MAX_CLOSED_TRADES:]
                            state.enh_extra["resolution_count"] += 1
                            state.enh_extra["last_exit_type"] = "RESOLUTION"
                            outcome = "WIN" if trade["won"] else "LOSS"
                            events.log("resolve", strategy="enhanced", trade=trade, trigger="rollover")
                            logger.info(
                                "ENH RESOLVED [%s] %s %s PnL=%+.2f",
                                outcome, trade["side"], trade["slug"], trade["pnl"],
                            )
                    else:
                        logger.warning(
                            "Cannot resolve enh position %s -- no BTC price or ctx",
                            state.enh_position.get("slug"),
                        )
                    state.enh_position = None

                # Reset enhanced strategy for new market
                enh.reset(t_zero=state.t_zero, strike=state.strike)
                market_ctx = refresh_market_ctx()
                events.log(
                    "market_rollover",
                    slug=state.slug, t_zero=state.t_zero, strike=state.strike,
                    yes_token_id=market_ctx.yes_token_id if market_ctx else None,
                    no_token_id=market_ctx.no_token_id if market_ctx else None,
                )

            # Ensure market_ctx is populated once market data is available.
            if market_ctx is None or market_ctx.slug != state.slug:
                market_ctx = refresh_market_ctx()

            offset = state.current_offset()
            if offset is None:
                await asyncio.sleep(1)
                continue

            # ── Enhanced strategy tick ──
            if (
                state.sigma and state.btc_price > 0
                and state.enh_position is None
                and market_ctx is not None
            ):
                market_up = state.market_price_up
                if market_up is not None:
                    enh_action = enh.on_tick(
                        state.btc_price, market_up, state.sigma, state.t_zero,
                        market_price_ts=state.market_price_ts,
                    )
                    if enh_action is not None:
                        action_type = enh_action.get("action")

                        if action_type in ("ENTER", "SQUEEZE_ENTER"):
                            src = "squeeze" if action_type == "SQUEEZE_ENTER" else "edge"
                            tz = enh_action.get("time_zone")
                            events.log(
                                "entry_signal",
                                strategy="enhanced", source=src, side=enh_action.get("side"),
                                entry_price=enh_action.get("entry_price"),
                                edge=enh_action.get("edge"),
                                size_usdc=enh_action.get("size_usdc"),
                                time_zone=tz, spike_score=enh_action.get("spike_score"),
                                offset=offset, slug=state.slug,
                            )
                            result = executor.enter(
                                enh_action, market_ctx, now, source=src,
                            )
                            if result is not None:
                                state.enh_position = result.to_position_dict()
                                if src == "edge":
                                    state.enh_extra["edge_trades"] += 1
                                else:
                                    state.enh_extra["squeeze_trades"] += 1
                                    state.enh_extra["squeeze_active"] = True
                                state.enh_extra["last_source"] = src
                                state.enh_extra["last_time_zone"] = tz
                                events.log(
                                    "entry_filled",
                                    strategy="enhanced", source=src, position=result.to_position_dict(),
                                    order_id=result.order_id, token_id=result.token_id,
                                )
                                logger.info(
                                    "ENH ENTRY %s %s @%.3f edge=%.3f $%.2f src=%s tz=%s",
                                    result.side, result.slug, result.entry_price,
                                    result.edge, result.size_usdc, src, tz,
                                )
                            else:
                                events.log(
                                    "entry_rejected",
                                    strategy="enhanced", source=src, side=enh_action.get("side"),
                                    slug=state.slug,
                                )

            # ── Enhanced: check profit grabber / resolution ──
            if (
                state.enh_position is not None and state.sigma
                and state.btc_price > 0 and market_ctx is not None
            ):
                enh_action = enh.on_tick(
                    state.btc_price, state.market_price_up or 0.5, state.sigma,
                    state.t_zero, market_price_ts=state.market_price_ts,
                )
                if enh_action is not None:
                    action_type = enh_action.get("action")

                    if action_type in ("EXIT_TP", "EXIT_SL"):
                        result = executor.exit(
                            state.enh_position, enh_action, market_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.enh_stats, trade)
                            state.enh_trades.append(trade)
                            if len(state.enh_trades) > MAX_CLOSED_TRADES:
                                state.enh_trades = state.enh_trades[-MAX_CLOSED_TRADES:]
                            if result.exit_type == "TP":
                                state.enh_extra["tp_count"] += 1
                            else:
                                state.enh_extra["sl_count"] += 1
                            state.enh_extra["last_exit_type"] = result.exit_type
                            state.enh_position = None
                            events.log("exit_filled", strategy="enhanced", trade=trade)
                            logger.info(
                                "ENH %s %s %s PnL=%+.2f hold=%.0fs",
                                result.exit_type, result.side, result.slug,
                                result.pnl, result.hold_time_s or 0,
                            )
                        else:
                            events.log(
                                "exit_rejected",
                                strategy="enhanced", action=action_type,
                                position=state.enh_position,
                            )

                    elif action_type == "RESOLVE":
                        result = executor.resolve(
                            state.enh_position, market_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.enh_stats, trade)
                            state.enh_trades.append(trade)
                            if len(state.enh_trades) > MAX_CLOSED_TRADES:
                                state.enh_trades = state.enh_trades[-MAX_CLOSED_TRADES:]
                            state.enh_extra["resolution_count"] += 1
                            state.enh_extra["last_exit_type"] = "RESOLUTION"
                            outcome = "WIN" if trade["won"] else "LOSS"
                            events.log("resolve", strategy="enhanced", trade=trade)
                            logger.info(
                                "ENH RESOLVED [%s] %s %s PnL=%+.2f",
                                outcome, trade["side"], trade["slug"], trade["pnl"],
                            )
                        state.enh_position = None

            # Update squeeze state
            if enh.squeeze is not None:
                state.enh_extra["squeeze_active"] = enh.squeeze.is_squeeze_candidate
                state.enh_extra["spike_score"] = round(enh.squeeze.spike_score, 2)

            # ── Base strategy: simple edge entry at T+120..T+150 ──
            if ENTRY_OFFSET <= offset <= ENTRY_CUTOFF and state.base_position is None:
                if (
                    state.base_fair_price is not None
                    and state.market_price_up is not None
                    and state.strike is not None
                    and market_ctx is not None
                ):
                    fair = state.base_fair_price
                    market = state.market_price_up
                    edge = abs(fair - market)

                    if edge >= EDGE_MIN:
                        if fair > market:
                            side = "Up"
                            entry_price = market
                        else:
                            side = "Down"
                            entry_price = 1.0 - market

                        if entry_price > 0:
                            size_usdc, size_shares = compute_position_size(
                                edge, entry_price, max_risk=max_risk,
                            )
                            if size_usdc > 0:
                                base_action = {
                                    "action": "ENTER",
                                    "side": side,
                                    "entry_price": entry_price,
                                    "size_usdc": size_usdc,
                                    "size_shares": size_shares,
                                    "edge": edge,
                                    "fair": fair,
                                    "market": market,
                                }
                                events.log(
                                    "entry_signal", strategy="base", source="base",
                                    side=side, entry_price=entry_price,
                                    edge=edge, size_usdc=size_usdc, fair=fair,
                                    market=market, offset=offset, slug=state.slug,
                                )
                                result = base_executor.enter(
                                    base_action, market_ctx, now, source="base",
                                )
                                if result is not None:
                                    state.base_position = result.to_position_dict()
                                    events.log(
                                        "entry_filled", strategy="base",
                                        position=result.to_position_dict(),
                                    )
                                    logger.info(
                                        "BASE ENTRY %s %s @%.3f fair=%.3f edge=%.3f $%.2f",
                                        result.side, result.slug, result.entry_price,
                                        fair, edge, result.size_usdc,
                                    )
                                else:
                                    events.log(
                                        "entry_rejected", strategy="base", side=side,
                                        slug=state.slug,
                                    )

            # ── Base: resolve at T+300 (paper only) ──
            if state.base_position is not None and offset >= MARKET_DURATION:
                if state.btc_price > 0 and market_ctx is not None:
                    result = base_executor.resolve(
                        state.base_position, market_ctx, now,
                        btc_price=state.btc_price,
                    )
                    if result is not None:
                        trade = result.to_trade_dict()
                        update_stats(state.base_stats, trade)
                        state.base_trades.append(trade)
                        if len(state.base_trades) > MAX_CLOSED_TRADES:
                            state.base_trades = state.base_trades[-MAX_CLOSED_TRADES:]
                        outcome = "WIN" if trade["won"] else "LOSS"
                        events.log("resolve", strategy="base", trade=trade)
                        logger.info(
                            "BASE RESOLVED [%s] %s %s PnL=%+.2f",
                            outcome, trade["side"], trade["slug"], trade["pnl"],
                        )
                else:
                    logger.warning(
                        "Cannot resolve base position %s -- no BTC price or ctx",
                        state.base_position.get("slug"),
                    )
                state.base_position = None

            # Periodic reconcile (no-op in paper mode).
            executor.reconcile(now)

            await asyncio.sleep(1)

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in strategy loop")
            await asyncio.sleep(1)


# ── State persister ──────────────────────────────────────────────────────

async def state_persister(state: DaemonState):
    """Write state to JSON every 5 seconds."""
    while True:
        try:
            data = state.to_dict()
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.replace(STATE_FILE)
        except Exception:
            logger.exception("Failed to persist state")
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return


# ── Main entrypoints ─────────────────────────────────────────────────────

async def run():
    state = DaemonState()
    ewma = EWMA()
    state.detect_market()
    executor, resolver, risk = build_executor()
    events = EventLogger(EVENTS_FILE)
    events.log(
        "startup", mode=executor.mode,
        max_trade_size=risk.config.max_trade_size_usdc,
        max_daily_loss=risk.config.max_daily_loss_usdc,
        dry_run=os.environ.get("POLYMARKET_DRY_RUN", "0") == "1",
    )

    logger.info("daemon_base_v1 starting -- PID %d (dual strategy mode)", os.getpid())
    logger.info("  BASE: edge_min=%.2f edge_max=%.2f entry=%d-%ds", EDGE_MIN, EDGE_MAX, ENTRY_OFFSET, ENTRY_CUTOFF)
    logger.info("  ENHANCED: time-zones + profit-grabber + squeeze-detector")

    effective_max = risk.config.max_trade_size_usdc
    tasks = [
        asyncio.create_task(binance_feed(state, ewma)),
        asyncio.create_task(rtds_feed(state)),
        asyncio.create_task(
            strategy_loop(state, executor, resolver, events, max_risk=effective_max)
        ),
        asyncio.create_task(state_persister(state)),
    ]

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        stop.set()
        for t in tasks:
            t.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        # handle_signal cancelled the tasks; wait briefly for them to unwind.
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_S)
        if pending:
            logger.warning(
                "Shutdown: %d task(s) did not exit in %.0fs; forcing",
                len(pending), SHUTDOWN_TIMEOUT_S,
            )
    finally:
        events.log("shutdown")
        events.close()
        logger.info("daemon_base_v1 stopped")
        PID_FILE.unlink(missing_ok=True)
        if any(not t.done() for t in tasks):
            # asyncio cleanup won't complete; force process exit so the next
            # launch_daemon.sh run isn't blocked by the startup guard.
            logger.error("Tasks still hung after timeout; calling os._exit")
            os._exit(1)


def _another_daemon_running() -> int | None:
    """Return PID of an already-running daemon, else None.

    Two checks: (1) PID file points at a live process, (2) any other process
    on this machine matches our argv. The argv check catches daemons started
    without a PID file (manual launches, crashed launchers).
    """
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if pid != os.getpid() and _pid_alive(pid):
                return pid
        except (ValueError, OSError):
            pass
    # Scan /proc for another process running this script. Only count python
    # interpreters — sudo / ip-netns / shell wrappers also have the full
    # daemon argv in their cmdline (because they're its ancestors), and our
    # own parent chain would match if we just substring-checked.
    self_pid = os.getpid()
    project_dir = Path(__file__).resolve().parent.name
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == self_pid:
                continue
            try:
                comm = (entry / "comm").read_text().strip()
            except OSError:
                continue
            if not comm.startswith("python"):
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode()
            except OSError:
                continue
            if "daemon_base_v1.py" in cmdline and project_dir in cmdline:
                return pid
    except OSError:
        pass
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return pid > 0  # PermissionError = exists but not ours
    except OSError:
        return False


def main():
    setup_logging()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    other = _another_daemon_running()
    if other is not None:
        msg = (
            f"Refusing to start: daemon_base_v1.py is already running as PID {other}. "
            f"Stop it first (kill {other}) or remove {PID_FILE} if it's stale."
        )
        logger.error(msg)
        print(f"[daemon_base_v1] {msg}", file=sys.stderr)
        sys.exit(1)

    PID_FILE.write_text(str(os.getpid()))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
