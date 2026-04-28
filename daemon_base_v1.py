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

import requests

try:
    import websockets
except ImportError:
    print("pip install websockets")
    exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from active_bots.enhanced_strategy import EnhancedStrategy
from active_bots.refined_strategy import RefinedStrategy
from active_bots.execution import Executor, MarketCtx
from active_bots.execution.event_logger import EventLogger
from active_bots.execution.live_book_state import LiveBookState, MarketBooks
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
WS_CLOB_BOOK = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BOOK_WS_PING_INTERVAL_S = 15.0
BOOK_WS_PING_TIMEOUT_S = 10.0
BOOK_WS_STALE_S = 30.0
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
SLUG_PREFIX = "btc-updown-5m-"

# RTDS robustness knobs. The library-level ping/pong catches dead peers
# (Layer 1); the stale-frame watchdog catches server-side delivery stalls
# that keep the TCP socket alive but deliver no frames (Layer 2).
RTDS_PING_INTERVAL_S = 15.0
RTDS_PING_TIMEOUT_S = 10.0
RTDS_STALE_S = 60.0
# Grace window around rollover: accept trades whose embedded t_zero is the
# current cycle OR the cycle that just ended. This absorbs in-flight RTDS
# events from the prior market without polluting the new cycle's price,
# because we discard them once `state.market_price_up` is already set for
# the new cycle (see rtds_feed).
RTDS_ROLLOVER_GRACE_S = 5.0

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


def fetch_portfolio_size_usdc(client) -> float | None:
    """Query the CLOB for the funder's free USDC collateral.

    Returns float USD balance, or None if the query fails, the import is
    missing, or the balance is zero/negative (treat as 'can't use this as
    portfolio size'). Never raises.
    """
    try:
        from py_clob_client.clob_types import (  # type: ignore
            AssetType,
            BalanceAllowanceParams,
        )
    except ImportError as exc:  # pragma: no cover — manual-exercise path
        logger.warning("fetch_portfolio_size_usdc: py_clob_client unavailable: %s", exc)
        return None

    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        balance = client.get_balance_allowance(params)
        raw_bal = float(balance.get("balance") or 0)
    except Exception as exc:  # noqa: BLE001 — defensive: any failure → None
        logger.warning("fetch_portfolio_size_usdc: query failed: %s", exc)
        return None

    usdc = raw_bal / 1e6
    if usdc <= 0:
        return None
    return usdc


def compute_effective_max_risk(
    *, portfolio_override: float | None = None,
) -> tuple[float, str]:
    """Resolve per-trade max bet from env. Returns (value, source).

    Order:
      1. PORTFOLIO_SIZE_USDC env * MAX_BET_PCT (source "env")
      2. portfolio_override arg (if >0) * MAX_BET_PCT (source "autodetect")
      3. MAX_TRADE_SIZE_USDC absolute ceiling (wins via min() against 1/2,
         or standalone as source "absolute")
      4. Fallback to MAX_RISK (100.0), source "default"

    Defensive parsing: invalid (non-float, non-positive) env values are
    treated as unset and logged. MAX_BET_PCT > 1.0 is also rejected as
    nonsense. When MAX_BET_PCT is invalid but a portfolio size is valid,
    we fall back to the default pct (DEFAULT_MAX_BET_PCT = 0.20) so a bad
    pct does not disable portfolio sizing entirely.

    The absolute cap (MAX_TRADE_SIZE_USDC) applies regardless of portfolio
    source: env-set or autodetected portfolios both get min()'d against it.
    When the abs cap wins over an autodetected portfolio, the source label
    flips to "absolute" (matches existing env-portfolio behavior).
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
        portfolio_value = portfolio
        source = "env"
    elif portfolio_override is not None and portfolio_override > 0:
        portfolio_value = portfolio_override
        source = "autodetect"
    else:
        portfolio_value = None

    if portfolio_value is not None:
        pct = _env_float("MAX_BET_PCT", DEFAULT_MAX_BET_PCT)
        if pct is None or pct <= 0 or pct > 1.0:
            if pct is not None:
                logger.warning(
                    "MAX_BET_PCT=%s out of range (0, 1]; using default %.2f",
                    pct, DEFAULT_MAX_BET_PCT,
                )
            pct = DEFAULT_MAX_BET_PCT
        candidate = portfolio_value * pct

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

    POLYMARKET_MODE=paper  -> PaperExecutor (no network, no client build)
    POLYMARKET_MODE=live   -> LiveExecutor (real CLOB orders)
    POLYMARKET_DRY_RUN=1   -> PaperExecutor (overrides mode)

    In live mode (regardless of dry_run) we try to build the CLOB client
    so we can auto-detect the funder's USDC balance and use it as the
    portfolio size. Client build failures (no creds, no py-clob-client,
    SG geo-block reaching clob.polymarket.com without the polybot VPN
    netns) are non-fatal: we fall back to env/default sizing and, if the
    mode was `live`, route to paper for safety.
    """
    mode = os.environ.get("POLYMARKET_MODE", "paper").lower()
    dry_run = os.environ.get("POLYMARKET_DRY_RUN", "0") == "1"

    # Attempt client build + autodetect only if mode is live. Paper users
    # have no funder to query, and we don't want to drag in the VPN/client
    # dependency for strategy-only comparison runs.
    client = None
    client_build_error: str | None = None
    autodetected_portfolio: float | None = None
    if mode == "live":
        try:
            from active_bots.execution.clob_client_factory import (
                ClobFactoryError,
                build_client,
            )
            client = build_client()
        except ClobFactoryError as e:
            client_build_error = str(e)
            logger.warning(
                "autodetect: could not build CLOB client (%s); "
                "falling back to env/default portfolio size",
                e,
            )
        except Exception as e:  # noqa: BLE001 — defensive: any import/init failure
            client_build_error = f"unexpected error: {e}"
            logger.warning(
                "autodetect: unexpected error building CLOB client (%s); "
                "falling back to env/default portfolio size",
                e,
            )

        if client is not None:
            autodetected_portfolio = fetch_portfolio_size_usdc(client)

    effective_max, source = compute_effective_max_risk(
        portfolio_override=autodetected_portfolio,
    )

    max_daily_loss = _env_float("MAX_DAILY_LOSS_USDC", 300.0)
    if max_daily_loss is None or max_daily_loss <= 0:
        max_daily_loss = 300.0

    # 0 disables. Default 30 matches the operator's preferred session cap.
    max_session_loss = _env_float("MAX_SESSION_LOSS_USDC", 30.0)
    if max_session_loss is None or max_session_loss < 0:
        max_session_loss = 30.0

    min_trade_size = _env_float("MIN_TRADE_SIZE_USDC", 10.0)
    if min_trade_size is None or min_trade_size < 0:
        min_trade_size = 10.0

    risk_cfg = RiskConfig(
        max_daily_loss_usdc=max_daily_loss,
        max_session_loss_usdc=max_session_loss,
        max_trade_size_usdc=effective_max,
        min_trade_size_usdc=min_trade_size,
        kill_switch_file=Path(
            os.environ.get("KILL_SWITCH_FILE", "daemon_state/KILL")
        ),
    )
    risk = RiskManager(risk_cfg)

    portfolio_str = (
        f"${autodetected_portfolio:.2f}" if autodetected_portfolio is not None
        else "<none>"
    )

    # Paper: POLYMARKET_MODE=paper. Real paper-mode session with no live
    # credentials and no CLOB client build.
    if mode != "live":
        logger.info(
            "executor=paper (POLYMARKET_MODE=%s) portfolio=%s max_trade_size=$%.2f (%s)",
            mode, portfolio_str, effective_max, source,
        )
        return PaperExecutor(), None, risk

    # mode == "live" with client-build failure → real safety net back to paper.
    if client is None:
        logger.error(
            "LIVE MODE REQUESTED BUT FAILED TO INIT CLIENT: %s",
            client_build_error,
        )
        logger.error("Falling back to paper mode for safety")
        logger.info(
            "executor=paper (live-fallback) portfolio=%s max_trade_size=$%.2f (%s)",
            portfolio_str, effective_max, source,
        )
        return PaperExecutor(), None, risk

    # mode == "live" with DRY_RUN=1 → DryRunExecutor: PaperExecutor fill math
    # (realistic fills at strategy's requested mid; ProfitGrabber TP/SL fire
    # at real prices; PnL is meaningful) wrapped with LiveExecutor-shaped
    # metadata (order_id='dry-run-<ms>', token_id from MarketCtx). Does NOT
    # exercise live_executor.py — that path is exercised only the first time
    # the operator runs LIVE_MODE with POLYMARKET_DRY_RUN unset.
    if dry_run:
        from active_bots.execution.dry_run_executor import DryRunExecutor
        from active_bots.execution.token_resolver import TokenResolver
        resolver = TokenResolver()
        funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
        logger.info(
            "executor=live-dryrun (paper fills + live-shape metadata; no CLOB posts) "
            "funder=%s portfolio=%s max_trade_size=$%.2f (%s)",
            funder, portfolio_str, effective_max, source,
        )
        return DryRunExecutor(), resolver, risk

    from active_bots.execution.live_executor import LiveExecutor
    from active_bots.execution.reconciler import Reconciler
    from active_bots.execution.token_resolver import TokenResolver

    funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
    resolver = TokenResolver()
    reconciler = Reconciler(funder_address=funder) if funder else None

    logger.info(
        "executor=live funder=%s portfolio=%s max_trade_size=$%.2f (%s) "
        "daily_loss=$%.0f session_loss=$%.0f",
        funder, portfolio_str, effective_max, source,
        risk_cfg.max_daily_loss_usdc, risk_cfg.max_session_loss_usdc,
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
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S",
    ))
    # Attach to the root logger so child loggers (execution.live, execution.risk,
    # execution.events, execution.reconciler, etc.) all write to daemon.log.
    # Previously the handler was only on the "daemon_base_v1" logger, so live
    # executor warnings/errors went to stderr (lost under the TUI) and never
    # reached the log — making live-mode rejections look mysterious.
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
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
        # Wall-clock of the last frame received from RTDS (regardless of
        # whether it matched our slug). Used by the stale-frame watchdog.
        self.rtds_last_msg_ts = 0.0

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

        # Refined strategy state (session champion — live-capable)
        self.refined_fair_price = None
        self.refined_position = None
        self.refined_trades = []
        self.refined_stats = {
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
        self.refined_extra = {
            "last_exit_type": None,
            "last_time_zone": None,
            "last_source": None,
            "tp_count": 0,
            "sl_count": 0,
            "resolution_count": 0,
            "edge_trades": 0,
        }

        self.connections = {"binance": False, "rtds": False}

        # Per-slug book registry — populated by clob_book_feed.
        # books_by_slug[slug] = MarketBooks(yes=..., no=...)
        self.books_by_slug: dict[str, "MarketBooks"] = {}
        # Per-asset_id metadata for the WS dispatcher
        # token_index[asset_id] = {"slug": str, "side": "yes"|"no", "book": LiveBookState}
        self.token_index: dict[str, dict] = {}
        self.connections["clob_book"] = False

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
            self.refined_fair_price = None
            return old_t_zero is not None
        return False

    def recompute_fair(self):
        if not all([self.btc_price, self.strike, self.sigma]):
            return
        tr = self.time_remaining()
        fair = fair_price_up(self.btc_price, self.strike, self.sigma, tr)
        self.base_fair_price = fair
        self.enh_fair_price = fair
        self.refined_fair_price = fair

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
            "rtds_last_msg_ts": self.rtds_last_msg_ts,
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
            "refined": {
                "fair_price": self.refined_fair_price,
                "open_position": self.refined_position,
                "closed_trades": self.refined_trades,
                "stats": self.refined_stats,
                "extra": self.refined_extra,
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


def _gamma_fetch_yes_price(slug: str, timeout: float = 3.0) -> float | None:
    """Synchronous Gamma REST call for a slug's last YES-side price.

    Returns a float in [0, 1] or None on any failure / missing data. Never
    raises — callers fold None into "no warm-start available".
    """
    try:
        resp = requests.get(
            GAMMA_MARKETS_URL, params={"slug": slug}, timeout=timeout,
        )
        resp.raise_for_status()
        markets = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("gamma warm-start http failed for %s: %s", slug, e)
        return None
    if not markets:
        return None
    m = markets[0] if isinstance(markets, list) else markets
    raw = m.get("outcomePrices", "[]")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(prices, list) or not prices:
        return None
    try:
        yes = float(prices[0])
    except (TypeError, ValueError):
        return None
    if not 0.0 <= yes <= 1.0:
        return None
    return yes


async def warm_start_market_price(state: DaemonState, slug: str) -> None:
    """Prime state.market_price_up via Gamma REST without blocking the loop.

    Only overwrites if the WS hasn't already populated a price for the
    current slug. Runs in a thread so the synchronous requests call
    doesn't stall the event loop.
    """
    captured_t_zero = state.t_zero
    yes = await asyncio.to_thread(_gamma_fetch_yes_price, slug)
    # Guard against races: market may have rolled again while we were
    # waiting on the HTTP call.
    if state.t_zero != captured_t_zero:
        return
    if yes is None:
        return
    if state.market_price_up is not None:
        return
    state.market_price_up = yes
    state.market_price_ts = time.time()
    logger.info("RTDS warm-start slug=%s market_price_up=%.4f (gamma)", slug, yes)


async def _rtds_stale_watchdog(state: DaemonState, ws) -> None:
    """Force-close the RTDS socket if no frame has arrived in RTDS_STALE_S.

    The library's ping_interval/ping_timeout handles peer-dead cases. This
    watchdog handles the distinct "peer is up and acks pings, but their
    trade-forwarding pipeline has stopped sending us frames" case that
    sank us on 2026-04-22.
    """
    while True:
        await asyncio.sleep(10.0)
        last = state.rtds_last_msg_ts
        if last <= 0.0:
            continue
        gap = time.time() - last
        if gap > RTDS_STALE_S:
            logger.warning(
                "RTDS stale %.0fs (no frames, last=%s) — forcing reconnect",
                gap, time.strftime("%H:%M:%S", time.localtime(last)),
            )
            try:
                await ws.close(code=4000, reason="stale frames")
            except Exception:  # pylint: disable=broad-except
                pass
            return


async def rtds_feed(state: DaemonState):
    """Connect to Polymarket RTDS and update market price.

    Robustness:
      Layer 1 — library-level ping/pong catches dead peers.
      Layer 2 — stale-frame watchdog catches server-side delivery stalls.
      Layer 3 — a short t_zero grace window absorbs in-flight trades from
                the just-ended cycle during the rollover transition.
    """
    while True:
        try:
            async with websockets.connect(
                WS_RTDS,
                ping_interval=RTDS_PING_INTERVAL_S,
                ping_timeout=RTDS_PING_TIMEOUT_S,
                close_timeout=5.0,
            ) as ws:
                state.connections["rtds"] = True
                state.rtds_last_msg_ts = time.time()
                logger.info("RTDS connected")

                sub = json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "activity",
                        "type": "orders_matched",
                    }],
                }, separators=(",", ":"))
                await ws.send(sub)

                watchdog_task = asyncio.create_task(_rtds_stale_watchdog(state, ws))
                try:
                    async for raw in ws:
                        # Record frame arrival BEFORE any filter: the
                        # watchdog needs to know the socket is still
                        # flowing, even if nothing matches our slug.
                        state.rtds_last_msg_ts = time.time()

                        if not isinstance(raw, str):
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

                        # Accept current cycle OR the cycle that ended in
                        # the last RTDS_ROLLOVER_GRACE_S so in-flight
                        # trades from the prior market aren't dropped
                        # silently during the transition.
                        try:
                            trade_slug_tz = int(slug.split("-")[-1])
                        except (ValueError, TypeError):
                            continue
                        if trade_slug_tz != state.t_zero:
                            prev_tz = (
                                state.t_zero - MARKET_DURATION
                                if state.t_zero is not None
                                else None
                            )
                            if (
                                trade_slug_tz != prev_tz
                                or state.t_zero is None
                                or (time.time() - state.t_zero) > RTDS_ROLLOVER_GRACE_S
                                or state.market_price_up is not None
                            ):
                                continue

                        outcome = payload.get("outcome", "")
                        price = payload.get("price")
                        if price is None:
                            continue
                        try:
                            price = float(price)
                        except (TypeError, ValueError):
                            continue

                        if outcome in ("Up", "Yes"):
                            state.market_price_up = price
                        elif outcome in ("Down", "No"):
                            state.market_price_up = 1.0 - price
                        else:
                            continue

                        state.market_price_ts = time.time()
                finally:
                    watchdog_task.cancel()
                    try:
                        await asyncio.wait_for(watchdog_task, timeout=1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        pass

        except (websockets.ConnectionClosed, OSError) as e:
            state.connections["rtds"] = False
            logger.warning("RTDS disconnected: %s, reconnecting in 1s", e)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return


# ── CLOB book feed (walked-VWAP gate input) ────────────────────────────

def _dispatch_book_message(msg: dict, token_index: dict, ts_ms: int) -> None:
    """Apply one CLOB WS message to the per-asset token_index."""
    et = msg.get("event_type", "")
    if et in ("book", "snapshot"):
        aid = msg.get("asset_id")
        slot = token_index.get(aid)
        if not slot:
            return
        slot["book"].apply_snapshot(
            bids=msg.get("bids", []),
            asks=msg.get("asks", []),
            ts_ms=ts_ms,
        )
    elif et == "price_change":
        for delta in msg.get("price_changes", []):
            aid = delta.get("asset_id")
            slot = token_index.get(aid)
            if not slot:
                continue
            slot["book"].apply_delta(delta, ts_ms=ts_ms)
    elif et == "tick_size_change":
        aid = msg.get("asset_id")
        slot = token_index.get(aid)
        if not slot:
            return
        try:
            slot["book"].tick_size = float(msg.get("new_tick_size") or 0)
        except (TypeError, ValueError):
            pass
    # last_trade_price / best_bid_ask / etc. — ignored for the gate


async def clob_book_feed(state: DaemonState):
    """Subscribe to YES+NO books for the current+next slugs.

    Re-subscribes on rollover. State is fed via _dispatch_book_message into
    state.token_index; the strategy_loop reads via state.books_by_slug.
    """
    subscribed_slug: int | None = None  # t_zero we last subscribed to
    while True:
        try:
            # Wait until t_zero + market_ctx is set up.
            if state.t_zero is None or state.slug is None:
                await asyncio.sleep(1.0)
                continue
            # If t_zero rolled, rebuild token_index with fresh slugs.
            if state.t_zero != subscribed_slug:
                subscribed_slug = state.t_zero
                # Rebuild token_index from token_resolver lookups for current+next.
                new_index, new_books = await asyncio.to_thread(
                    _resolve_books_for_window, state,
                )
                state.token_index = new_index
                state.books_by_slug = new_books
                logger.info(
                    "CLOB book: subscribing to %d tokens for slugs %s",
                    len(new_index), list(new_books.keys()),
                )
            asset_ids = list(state.token_index.keys())
            if not asset_ids:
                await asyncio.sleep(1.0)
                continue
            async with websockets.connect(
                WS_CLOB_BOOK,
                ping_interval=BOOK_WS_PING_INTERVAL_S,
                ping_timeout=BOOK_WS_PING_TIMEOUT_S,
                close_timeout=5.0,
            ) as ws:
                state.connections["clob_book"] = True
                logger.info("CLOB book WS connected")
                sub = json.dumps(
                    {"assets_ids": asset_ids, "type": "market",
                     "custom_feature_enabled": True},
                    separators=(",", ":"),
                )
                await ws.send(sub)
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    msgs = payload if isinstance(payload, list) else [payload]
                    ts_ms = int(time.time() * 1000)
                    for m in msgs:
                        if isinstance(m, dict):
                            _dispatch_book_message(m, state.token_index, ts_ms)
        except (websockets.ConnectionClosed, OSError) as e:
            state.connections["clob_book"] = False
            logger.warning("CLOB book WS disconnected: %s; reconnecting in 2s", e)
            # Drop state so a fresh REST/WS reprime is implicit.
            for slot in state.token_index.values():
                slot["book"].has_baseline = False
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            return


def _resolve_books_for_window(state: DaemonState):
    """Build a token_index + books_by_slug for current+next 5-min slugs.

    Mirrors scripts/scrape_book.py's discovery shape but reuses the daemon's
    existing TokenResolver — no Gamma re-fetch — for the slugs we already
    know. Returns ({asset_id: {slug,side,book}}, {slug: MarketBooks}).
    """
    # Lazy import to avoid coupling the dispatcher tests to the resolver.
    from active_bots.execution.token_resolver import TokenResolver
    resolver = TokenResolver()
    slugs = []
    if state.slug:
        slugs.append(state.slug)
    next_t0 = (state.t_zero or 0) + MARKET_DURATION
    next_slug = SLUG_PREFIX + str(int(next_t0))
    slugs.append(next_slug)
    token_index: dict[str, dict] = {}
    books_by_slug: dict[str, MarketBooks] = {}
    for slug in slugs:
        tokens = resolver.resolve(slug)
        if tokens is None:
            continue
        yes = LiveBookState(tick_size=float(tokens.tick_size or 0.01))
        no = LiveBookState(tick_size=float(tokens.tick_size or 0.01))
        books_by_slug[slug] = MarketBooks(yes=yes, no=no)
        if tokens.yes_token_id:
            token_index[tokens.yes_token_id] = {"slug": slug, "side": "yes", "book": yes}
        if tokens.no_token_id:
            token_index[tokens.no_token_id] = {"slug": slug, "side": "no", "book": no}
    return token_index, books_by_slug


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
    state: DaemonState, executor: Executor, resolver, risk: RiskManager,
    events: EventLogger, max_risk: float = MAX_RISK,
):
    # BASE and ENHANCED are paper benchmarks — never trade live. REFINED is
    # the session champion (squeeze off + tighter TP) and uses the main
    # executor, which may be live or paper depending on env.
    base_executor: Executor = PaperExecutor()
    enh_executor: Executor = PaperExecutor()
    """Main strategy loop. Runs at 1 Hz.

    - Detects market rollover
    - Enters positions at ENTRY_OFFSET..ENTRY_CUTOFF (base) or via strategies
    - Resolves at market expiry (T+300)
    - Delegates order placement / fill bookkeeping to the right executor
    """
    logger.info(
        "Strategy loop started — refined=%s, enhanced=paper, base=paper (benchmarks), max_risk=%.2f",
        executor.mode, max_risk,
    )
    enh = EnhancedStrategy(max_risk=max_risk)
    refined = RefinedStrategy(max_risk=max_risk)
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
                        result = enh_executor.resolve(
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

                if state.refined_position is not None:
                    if state.btc_price > 0 and prev_ctx is not None:
                        result = executor.resolve(
                            state.refined_position, prev_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.refined_stats, trade)
                            risk.record_trade(trade["pnl"], trade.get("resolved_time"))
                            state.refined_trades.append(trade)
                            if len(state.refined_trades) > MAX_CLOSED_TRADES:
                                state.refined_trades = state.refined_trades[-MAX_CLOSED_TRADES:]
                            state.refined_extra["resolution_count"] += 1
                            state.refined_extra["last_exit_type"] = "RESOLUTION"
                            outcome = "WIN" if trade["won"] else "LOSS"
                            events.log("resolve", strategy="refined", trade=trade, trigger="rollover")
                            logger.info(
                                "REFINED RESOLVED [%s] %s %s PnL=%+.2f",
                                outcome, trade["side"], trade["slug"], trade["pnl"],
                            )
                    else:
                        logger.warning(
                            "Cannot resolve refined position %s -- no BTC price or ctx",
                            state.refined_position.get("slug"),
                        )
                    state.refined_position = None

                # Reset strategies for new market
                enh.reset(t_zero=state.t_zero, strike=state.strike)
                refined.reset(t_zero=state.t_zero, strike=state.strike)
                market_ctx = refresh_market_ctx()
                events.log(
                    "market_rollover",
                    slug=state.slug, t_zero=state.t_zero, strike=state.strike,
                    yes_token_id=market_ctx.yes_token_id if market_ctx else None,
                    no_token_id=market_ctx.no_token_id if market_ctx else None,
                )
                # Warm-start market_price_up from Gamma REST so quiet
                # markets don't block on the first WS trade event. Runs
                # in a thread and no-ops if the WS wins the race.
                if state.slug:
                    asyncio.create_task(warm_start_market_price(state, state.slug))

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
                            result = enh_executor.enter(
                                enh_action, market_ctx, now, source=src,
                            )
                            if result is not None:
                                # Stamp ACK moment: "response observed" rather
                                # than the pre-call `now`. Feeds reconcile.py
                                # §6.3 predicate and latency empirical fit.
                                result.ack_ts = time.time()
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
                                    fill_details=result.fill_details,
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
                        result = enh_executor.exit(
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
                            events.log(
                                "exit_filled", strategy="enhanced", trade=trade,
                                fill_details=result.fill_details,
                            )
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
                        result = enh_executor.resolve(
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

            # ── Refined strategy tick ──
            if (
                state.sigma and state.btc_price > 0
                and state.refined_position is None
                and market_ctx is not None
            ):
                market_up = state.market_price_up
                if market_up is not None:
                    ref_action = refined.on_tick(
                        state.btc_price, market_up, state.sigma, state.t_zero,
                        market_price_ts=state.market_price_ts,
                    )
                    if ref_action is not None:
                        action_type = ref_action.get("action")

                        if action_type == "ENTER":
                            tz = ref_action.get("time_zone")
                            events.log(
                                "entry_signal",
                                strategy="refined", source="edge", side=ref_action.get("side"),
                                entry_price=ref_action.get("entry_price"),
                                edge=ref_action.get("edge"),
                                size_usdc=ref_action.get("size_usdc"),
                                time_zone=tz, offset=offset, slug=state.slug,
                            )
                            result = executor.enter(
                                ref_action, market_ctx, now, source="edge",
                            )
                            if result is not None:
                                result.ack_ts = time.time()
                                state.refined_position = result.to_position_dict()
                                state.refined_extra["edge_trades"] += 1
                                state.refined_extra["last_source"] = "edge"
                                state.refined_extra["last_time_zone"] = tz
                                events.log(
                                    "entry_filled",
                                    strategy="refined", source="edge",
                                    position=result.to_position_dict(),
                                    order_id=result.order_id, token_id=result.token_id,
                                    fill_details=result.fill_details,
                                )
                                logger.info(
                                    "REFINED ENTRY %s %s @%.3f edge=%.3f $%.2f tz=%s",
                                    result.side, result.slug, result.entry_price,
                                    result.edge, result.size_usdc, tz,
                                )
                            else:
                                events.log(
                                    "entry_rejected",
                                    strategy="refined", source="edge", side=ref_action.get("side"),
                                    slug=state.slug,
                                )

            # ── Refined: check profit grabber / resolution ──
            if (
                state.refined_position is not None and state.sigma
                and state.btc_price > 0 and market_ctx is not None
            ):
                ref_action = refined.on_tick(
                    state.btc_price, state.market_price_up or 0.5, state.sigma,
                    state.t_zero, market_price_ts=state.market_price_ts,
                )
                if ref_action is not None:
                    action_type = ref_action.get("action")

                    if action_type in ("EXIT_TP", "EXIT_SL"):
                        result = executor.exit(
                            state.refined_position, ref_action, market_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.refined_stats, trade)
                            risk.record_trade(trade["pnl"], trade.get("resolved_time"))
                            state.refined_trades.append(trade)
                            if len(state.refined_trades) > MAX_CLOSED_TRADES:
                                state.refined_trades = state.refined_trades[-MAX_CLOSED_TRADES:]
                            if result.exit_type == "TP":
                                state.refined_extra["tp_count"] += 1
                            else:
                                state.refined_extra["sl_count"] += 1
                            state.refined_extra["last_exit_type"] = result.exit_type
                            state.refined_position = None
                            events.log(
                                "exit_filled", strategy="refined", trade=trade,
                                fill_details=result.fill_details,
                            )
                            logger.info(
                                "REFINED %s %s %s PnL=%+.2f hold=%.0fs",
                                result.exit_type, result.side, result.slug,
                                result.pnl, result.hold_time_s or 0,
                            )
                        else:
                            events.log(
                                "exit_rejected",
                                strategy="refined", action=action_type,
                                position=state.refined_position,
                            )

                    elif action_type == "RESOLVE":
                        result = executor.resolve(
                            state.refined_position, market_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.refined_stats, trade)
                            risk.record_trade(trade["pnl"], trade.get("resolved_time"))
                            state.refined_trades.append(trade)
                            if len(state.refined_trades) > MAX_CLOSED_TRADES:
                                state.refined_trades = state.refined_trades[-MAX_CLOSED_TRADES:]
                            state.refined_extra["resolution_count"] += 1
                            state.refined_extra["last_exit_type"] = "RESOLUTION"
                            outcome = "WIN" if trade["won"] else "LOSS"
                            events.log("resolve", strategy="refined", trade=trade)
                            logger.info(
                                "REFINED RESOLVED [%s] %s %s PnL=%+.2f",
                                outcome, trade["side"], trade["slug"], trade["pnl"],
                            )
                        state.refined_position = None

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
                                    result.ack_ts = time.time()
                                    state.base_position = result.to_position_dict()
                                    events.log(
                                        "entry_filled", strategy="base",
                                        position=result.to_position_dict(),
                                        fill_details=result.fill_details,
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
        asyncio.create_task(clob_book_feed(state)),
        asyncio.create_task(
            strategy_loop(state, executor, resolver, risk, events, max_risk=effective_max)
        ),
        asyncio.create_task(state_persister(state)),
    ]
    # Warm-start the current market's price once at boot so the first
    # tick of strategy_loop doesn't wait for the first RTDS trade event.
    if state.slug:
        tasks.append(asyncio.create_task(warm_start_market_price(state, state.slug)))

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
