#!/usr/bin/env python3
"""Live Textual TUI dashboard for daemon_base_v1 (read-only).

Reads daemon_state/state.json, events.jsonl, daemon.log -- never writes.
Renders at ~2 Hz via a single _tick() that reads state.json once per frame.
Launch:  conda activate polymarket-env && python3 scripts/dashboard.py
Ctrl-C exits cleanly; launch_daemon.sh stops the daemon on exit.
"""
from __future__ import annotations

import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# When run as `python3 tui/python/dashboard.py`, sys.path[0] is tui/python/,
# so the repo-root `active_bots` package isn't importable. Add the repo root
# before touching anything from active_bots.
# tui/python/dashboard.py -> repo root is three parents up (python -> tui -> repo).
REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import requests
from rich.text import Text
from textual_plotext import PlotextPlot
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Static, DataTable, RichLog
from textual.worker import Worker, WorkerState

from active_bots.execution.token_resolver import TokenResolver  # noqa: E402
STATE_DIR   = REPO / "daemon_state"
STATE_FILE  = STATE_DIR / "state.json"
LOG_FILE    = STATE_DIR / "daemon.log"
EVENTS_FILE = STATE_DIR / "events.jsonl"
KILL_FILE   = STATE_DIR / "KILL"

RECENT_TRADES_CAP = 5
ACTION_CAP = 50
PRICE_HISTORY_CAP = 300       # ~5 min at 1 Hz state writes
PNL_SERIES_CAP = 2000         # per-strategy decimation cap
PNL_VISIBLE_WINDOW = 300      # PnL chart plots only the latest N points

CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def read_json(path: Path) -> dict | None:
    """Read a JSON file; return None on missing/invalid."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def tail_lines(path: Path, n: int = 8) -> list[str]:
    """Return the last n lines of a file as a list of str (decoded)."""
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 4096
            buf = b""
            while size > 0 and buf.count(b"\n") <= n:
                read = min(block, size)
                size -= read
                f.seek(size)
                buf = f.read(read) + buf
            return [line for line in buf.decode(errors="replace").splitlines()[-n:]]
    except OSError:
        return []


def pnl_color(v: float) -> str:
    """Return a rich style string for a signed pnl number."""
    if v > 0:
        return "bold green"
    if v < 0:
        return "bold red"
    return "white"


def fmt_secs(s: float | None) -> str:
    """Format seconds as M:SS, '-' on None."""
    if s is None:
        return "-"
    return f"{int(s // 60):d}:{int(s % 60):02d}"


def _ts_to_hms(ts: list[float]) -> list[str]:
    """Convert unix timestamps to HH:MM:SS strings for plotext date_form."""
    return [datetime.fromtimestamp(t).strftime("%H:%M:%S") for t in ts]


def _action_from_event(ev: dict) -> dict | None:
    """Convert a daemon event into a compact action dict for the orders log.

    Returns None for events that aren't trade actions.
    """
    t = ev.get("type")
    ts = ev.get("ts", 0)
    strategy = ev.get("strategy") or ""
    if t == "entry_filled":
        pos = ev.get("position") or {}
        return {
            "ts": ts, "kind": "BUY", "strategy": strategy,
            "side": pos.get("side", "?"),
            "price": pos.get("entry_price", 0.0),
            "size_usdc": pos.get("size_usdc", 0.0),
            "pnl": None, "exit_type": None, "won": None,
        }
    if t == "exit_filled":
        tr = ev.get("trade") or {}
        return {
            "ts": ts, "kind": "SELL", "strategy": strategy,
            "side": tr.get("side", "?"),
            "price": tr.get("exit_price", 0.0),
            "size_usdc": tr.get("size_usdc", 0.0),
            "pnl": tr.get("pnl", 0.0),
            "exit_type": tr.get("exit_type", ""),
            "won": None,
        }
    if t == "resolve":
        tr = ev.get("trade") or {}
        return {
            "ts": ts, "kind": "RES", "strategy": strategy,
            "side": tr.get("side", "?"),
            "price": tr.get("exit_price", 0.0),
            "size_usdc": tr.get("size_usdc", 0.0),
            "pnl": tr.get("pnl", 0.0),
            "exit_type": tr.get("exit_type", "RESOLUTION"),
            "won": bool(tr.get("won")),
        }
    return None


def _compute_stats(stats: dict) -> dict:
    """Derive display-friendly numbers from a strategy stats blob."""
    total = stats.get("total_trades", 0) or 0
    wins = stats.get("wins", 0) or 0
    losses = stats.get("losses", 0) or 0
    total_pnl = stats.get("total_pnl", 0.0) or 0.0
    total_risked = stats.get("total_risked", 0.0) or 0.0
    wr = 100.0 * wins / total if total else 0.0
    roi = (total_pnl / total_risked * 100) if total_risked else 0.0
    return {
        "total": total, "wins": wins, "losses": losses,
        "total_pnl": total_pnl, "total_risked": total_risked,
        "wr": wr, "roi": roi,
        "max_drawdown": stats.get("max_drawdown", 0) or 0,
        "streak": stats.get("current_streak", 0) or 0,
        "streak_type": stats.get("streak_type") or "",
    }


def _side_text(side: str) -> Text:
    if side == "Up":
        return Text("Up  ", style="green")
    if side == "Down":
        return Text("Down", style="red")
    return Text(f"{side:<4}", style="white")


def _kind_text(kind: str) -> Text:
    if kind == "BUY":
        return Text("BUY ", style="bold bright_green")
    if kind == "SELL":
        return Text("SELL", style="bold bright_yellow")
    if kind == "RES":
        return Text("RES ", style="bold cyan")
    return Text(f"{kind:<4}", style="white")


def _render_action_line(a: dict) -> Text:
    """Render one action dict (from EventsTailer) as a single Text line."""
    ts = a.get("ts", 0)
    tm = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
    line = Text()
    line.append(tm, style="dim")
    line.append("  ")
    line.append_text(_kind_text(a.get("kind", "?")))
    line.append(" ")
    line.append_text(_side_text(a.get("side", "?")))
    line.append(" ")

    kind = a.get("kind")
    if kind == "BUY":
        price = a.get("price") or 0.0
        size = a.get("size_usdc") or 0.0
        line.append(f"@ ${price:.2f}", style="white")
        line.append(f"   (${size:.2f})", style="dim")
    elif kind == "SELL":
        price = a.get("price") or 0.0
        pnl = a.get("pnl") or 0.0
        line.append(f"@ ${price:.2f}", style="white")
        line.append("   ")
        line.append(f"{pnl:+.2f}", style=pnl_color(pnl))
        line.append("   ")
        line.append(str(a.get("exit_type") or ""), style="dim")
    elif kind == "RES":
        won = a.get("won")
        pnl = a.get("pnl") or 0.0
        if won is True:
            line.append("WON ", style="bold green")
        elif won is False:
            line.append("LOST", style="bold red")
        else:
            line.append("RES ", style="cyan")
        line.append("   ")
        line.append(f"{pnl:+.2f}", style=pnl_color(pnl))
    return line


MARKET_DURATION = 300


@dataclass
class OrderbookSnapshot:
    """One snapshot of the YES-token orderbook for the current market."""
    status: str                                                          # ok | empty | error
    bids: list[tuple[float, float]] = field(default_factory=list)        # (price, size) desc
    asks: list[tuple[float, float]] = field(default_factory=list)        # (price, size) asc
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    mid: float = 0.0
    error: str | None = None


class OrderbookPoller:
    """Poll the Polymarket CLOB /book endpoint for one token id.

    2 s base cadence; backs off to 10 s after `failure_threshold` consecutive
    failures; returns to base on the first success. Read-only over HTTPS;
    network failures are recorded into the snapshot and never raise.
    """

    def __init__(
        self,
        token_id: str,
        *,
        session: requests.Session | None = None,
        base_interval: float = 2.0,
        backoff_interval: float = 10.0,
        failure_threshold: int = 3,
        timeout: float = 3.0,
    ) -> None:
        self.token_id = token_id
        self._session = session or requests.Session()
        self.base_interval = base_interval
        self.backoff_interval = backoff_interval
        self.failure_threshold = failure_threshold
        self._timeout = timeout
        self.current_interval = base_interval
        self._consecutive_failures = 0
        self.last_snapshot: OrderbookSnapshot | None = None

    def poll_once(self) -> OrderbookSnapshot:
        try:
            r = self._session.get(
                CLOB_BOOK_URL,
                params={"token_id": self.token_id},
                timeout=self._timeout,
            )
            r.raise_for_status()
            payload = r.json()
        except requests.Timeout:
            return self._record_failure("timeout")
        except requests.RequestException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            return self._record_failure(str(code) if code else e.__class__.__name__)
        except ValueError:
            return self._record_failure("bad-json")

        bids = [(float(b["price"]), float(b["size"]))
                for b in (payload.get("bids") or [])]
        asks = [(float(a["price"]), float(a["size"]))
                for a in (payload.get("asks") or [])]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        spread = (best_ask - best_bid) if (best_bid and best_ask) else 0.0
        mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0

        self._consecutive_failures = 0
        self.current_interval = self.base_interval
        snap = OrderbookSnapshot(
            status="ok" if (bids or asks) else "empty",
            bids=bids, asks=asks,
            best_bid=best_bid, best_ask=best_ask,
            spread=spread, mid=mid,
        )
        self.last_snapshot = snap
        return snap

    def _record_failure(self, reason: str) -> OrderbookSnapshot:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self.current_interval = self.backoff_interval
        snap = OrderbookSnapshot(status="error", error=reason)
        self.last_snapshot = snap
        return snap


class EventsTailer:
    """Tail daemon_state/events.jsonl by byte offset; accumulate per-strategy PnL.

    Read-only on the file. Resets state on truncation/rotation. Idempotent
    on repeated update() calls when no new events have arrived.
    """

    def __init__(self, path: Path, cap: int = 200) -> None:
        self.path = path
        self.events: deque[dict] = deque(maxlen=cap)
        self.base_actions: deque[dict] = deque(maxlen=ACTION_CAP)
        self.enh_actions: deque[dict] = deque(maxlen=ACTION_CAP)
        self.refined_actions: deque[dict] = deque(maxlen=ACTION_CAP)
        self.unified_actions: deque[dict] = deque(maxlen=ACTION_CAP)
        self.pnl_series: dict[str, list[tuple[float, float]]] = {}
        self._cum: dict[str, float] = {}
        self._pos = 0

    def _dispatch(self, ev: dict) -> None:
        action = _action_from_event(ev)
        if action is not None:
            strat = action.get("strategy")
            if strat == "base":
                self.base_actions.append(action)
            elif strat == "enhanced":
                self.enh_actions.append(action)
            elif strat == "refined":
                self.refined_actions.append(action)
            self.unified_actions.append(action)

        # Per-strategy cumulative PnL -- only realized events count.
        t = ev.get("type")
        if t in ("exit_filled", "resolve"):
            strat = ev.get("strategy") or ""
            pnl = ((ev.get("trade") or {}).get("pnl")) or 0.0
            self._cum[strat] = self._cum.get(strat, 0.0) + float(pnl)
            self.pnl_series.setdefault(strat, []).append(
                (float(ev.get("ts", 0.0)), self._cum[strat])
            )
            self._decimate_if_needed(strat)

    def _decimate_if_needed(self, strat: str) -> None:
        series = self.pnl_series.get(strat) or []
        if len(series) <= PNL_SERIES_CAP:
            return
        # Keep first, last, and evenly-spaced middle. Never drop the running tail.
        first, last = series[0], series[-1]
        middle = series[1:-1]
        middle_count = PNL_SERIES_CAP - 2
        if middle_count <= 0 or not middle:
            self.pnl_series[strat] = [first, last]
            return
        step = max(1, len(middle) // middle_count)
        sampled = middle[::step][:middle_count]
        self.pnl_series[strat] = [first, *sampled, last]

    def update(self) -> None:
        if not self.path.exists():
            return
        try:
            size = self.path.stat().st_size
            if size < self._pos:
                # File truncated - replay from the top.
                self._pos = 0
                self._cum.clear()
                self.pnl_series.clear()
            with self.path.open("rb") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
            # Defer any trailing partial line (daemon mid-write) until next tick.
            if data and not data.endswith(b"\n"):
                last_nl = data.rfind(b"\n")
                if last_nl == -1:
                    # Full chunk is a partial line; rewind fully, try again next tick.
                    self._pos -= len(data)
                    return
                self._pos -= (len(data) - last_nl - 1)
                data = data[: last_nl + 1]
            for line in data.decode(errors="replace").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                except ValueError:
                    continue
                self.events.append(ev)
                self._dispatch(ev)
        except OSError:
            return


class HeaderWidget(Static):
    def render_state(self, state: dict | None) -> None:
        if state is None:
            self.update("[dim]waiting for daemon_state/state.json[/]")
            return
        conn = state.get("connections") or {}
        t_zero = state.get("t_zero") or 0
        elapsed = time.time() - t_zero if t_zero else 0
        bar_w = 40
        filled = int(bar_w * min(elapsed / MARKET_DURATION, 1.0)) if t_zero else 0
        bar = "█" * filled + "░" * (bar_w - filled)
        kill = KILL_FILE.exists()

        line1 = Text()
        line1.append("BTC ", style="bold #22d3ee")
        line1.append(f"${(state.get('btc_price') or 0):>10,.2f}  ", style="bold")
        line1.append(f"sigma {(state.get('sigma') or 0)*100:.2f}%  ", style="magenta")
        line1.append(f"strike ${state.get('strike') or 0:,.0f}", style="#94a3b8")

        line2 = Text()
        line2.append(state.get("slug") or "-", style="yellow")
        line2.append(f"  [{bar}]  ", style="#94a3b8")
        line2.append(f"t+{int(elapsed)}s / {MARKET_DURATION}s  ", style="#22d3ee")
        line2.append("binance ", style="#94a3b8")
        line2.append("●", style="green" if conn.get("binance") else "red")
        line2.append("  rtds ", style="#94a3b8")
        line2.append("●", style="green" if conn.get("rtds") else "red")
        line2.append("  ")
        line2.append(" KILL " if kill else "  ok  ",
                     style="bold white on #d946ef" if kill else "dim green")

        self.update(Text("\n").join([line1, line2]))


class LiveCurvesWidget(Container):
    """Hero UP/DOWN prices + BTC chart + market/fair overlay chart.

    Maintains rolling deques per series, capped at PRICE_HISTORY_CAP.
    Resets all deques on market rollover (t_zero change).
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="hero-prices")
        yield PlotextPlot(id="chart-btc")
        yield PlotextPlot(id="chart-mkt")

    def on_mount(self) -> None:
        self._btc: deque[tuple[float, float]] = deque(maxlen=PRICE_HISTORY_CAP)
        self._mkt: deque[tuple[float, float]] = deque(maxlen=PRICE_HISTORY_CAP)
        self._fair: deque[tuple[float, float]] = deque(maxlen=PRICE_HISTORY_CAP)
        self._last_t_zero: float | None = None

    def render_state(self, state: dict | None) -> None:
        hero = self.query_one("#hero-prices", Static)
        if state is None:
            hero.update("[dim]waiting for daemon[/]")
            return

        # Reset deques on market rollover.
        t_zero = state.get("t_zero")
        if t_zero != self._last_t_zero:
            self._btc.clear()
            self._mkt.clear()
            self._fair.clear()
            self._last_t_zero = t_zero

        now = time.time()
        btc = state.get("btc_price")
        if isinstance(btc, (int, float)) and btc > 0:
            self._btc.append((now, float(btc)))
        mkt = state.get("market_price_up")
        if isinstance(mkt, (int, float)):
            self._mkt.append((now, float(mkt)))
        fair = (state.get("refined") or {}).get("fair_price")
        if isinstance(fair, (int, float)):
            self._fair.append((now, float(fair)))

        self._render_hero(mkt, hero)
        self._render_btc_chart()
        self._render_mkt_chart()

    def _render_hero(self, mkt, hero: Static) -> None:
        if not isinstance(mkt, (int, float)):
            line = Text()
            line.append("UP  -    ", style="dim")
            line.append("DOWN  -", style="dim")
            hero.update(line)
            return
        up = max(0.0, min(1.0, float(mkt)))
        line = Text()
        line.append(f"UP  ${up:.2f}", style="bold #4ade80")
        line.append("       ")
        line.append(f"DOWN  ${1 - up:.2f}", style="bold #f87171")
        hero.update(line)

    def _render_btc_chart(self) -> None:
        plot = self.query_one("#chart-btc", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("pro")
        plt.title("BTC USD")
        plt.date_form("H:M:S")
        if self._btc:
            xs = _ts_to_hms([t for t, _ in self._btc])
            ys = [v for _, v in self._btc]
            plt.plot(xs, ys, color="cyan", marker="braille")
            lo, hi = min(ys), max(ys)
            if lo == hi:
                lo, hi = lo - 1.0, hi + 1.0
            else:
                # 10% of range with a $0.50 floor so flat windows stay readable
                pad = (hi - lo) * 0.1 + 0.5
                lo, hi = lo - pad, hi + pad
            plt.ylim(lo, hi)
            # 4 evenly-spaced ticks, integer dollars (sub-dollar spread
            # collapses to repeated labels -- intentional, slope > level)
            ticks = [lo + (hi - lo) * i / 3 for i in range(4)]
            plt.yticks(ticks, [f"${v:,.0f}" for v in ticks])
        plot.refresh()

    def _render_mkt_chart(self) -> None:
        plot = self.query_one("#chart-mkt", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("pro")
        plt.date_form("H:M:S")
        if self._mkt:
            xs = _ts_to_hms([t for t, _ in self._mkt])
            ys = [v for _, v in self._mkt]
            plt.plot(xs, ys, color="white", marker="braille", label="mkt")
        if self._fair:
            xs = _ts_to_hms([t for t, _ in self._fair])
            ys = [v for _, v in self._fair]
            plt.plot(xs, ys, color="yellow", marker="braille", label="fair")
            plt.title("market (white) vs fair (yellow)")
        else:
            # Surface the missing series in the title -- plotext rejects
            # empty-series legend entries, so we can't stub the legend.
            plt.title("market (white) vs fair (yellow) -- fair: waiting")
        plt.ylim(0.0, 1.0)
        plt.yticks([0.0, 0.25, 0.5, 0.75, 1.0],
                   ["0.00", "0.25", "0.50", "0.75", "1.00"])
        plot.refresh()


class MainStrategyWidget(Container):
    TRADES_COLUMNS = ("t", "side", "in->out", "size", "pnl", "ROI", "why", "hold")

    def compose(self) -> ComposeResult:
        yield Static("", id="refined-headline")
        yield Static("", id="refined-secondary")
        yield PlotextPlot(id="chart-pnl")
        yield Static("no position", id="refined-position")
        yield DataTable(id="refined-trades", zebra_stripes=False,
                        cursor_type="none")

    def on_mount(self) -> None:
        tbl = self.query_one("#refined-trades", DataTable)
        for col in self.TRADES_COLUMNS:
            tbl.add_column(col, key=col)

    def render_state(
        self,
        state: dict | None,
        pnl_series: list[tuple[float, float]],
    ) -> None:
        if state is None:
            self.query_one("#refined-headline", Static).update(
                "[dim]waiting for state.json[/]"
            )
            return

        blob = state.get("refined") or {}
        stats = _compute_stats(blob.get("stats") or {})
        extra = blob.get("extra") or {}
        fair = blob.get("fair_price")
        mkt = state.get("market_price_up")

        self._render_headline(state, stats)
        self._render_secondary(fair, mkt, extra)
        self._render_pnl_chart(pnl_series)
        self._render_position(blob.get("open_position"), mkt)
        self._render_trades(blob.get("closed_trades") or [])

    # --- pieces --------------------------------------------------------

    def _render_headline(self, state: dict, stats: dict) -> None:
        t_zero = state.get("t_zero") or 0
        slug = state.get("slug") or ""
        line1 = Text()
        if t_zero:
            line1.append(
                "Market: BTC "
                + time.strftime("%Y-%m-%d T-T+5m", time.localtime(t_zero)),
                style="bold",
            )
        else:
            line1.append(f"Market: {slug}", style="bold")

        line2 = Text()
        line2.append(f"PnL {stats['total_pnl']:+.2f}  ",
                     style=f"bold {pnl_color(stats['total_pnl'])}")
        line2.append(f"ROI {stats['roi']:+.1f}%  ",
                     style=pnl_color(stats['roi']))
        line2.append(f"{stats['total']} tr  ", style="bold")
        line2.append(f"W={stats['wins']} L={stats['losses']} {stats['wr']:.0f}%  ",
                     style="#94a3b8")
        line2.append(f"DD ${stats['max_drawdown']:.2f}  ", style="red")
        st = stats['streak_type']
        st_style = "green" if st == "W" else "red" if st == "L" else "#94a3b8"
        line2.append(f"streak {stats['streak']} {st or '-'}", style=st_style)

        self.query_one("#refined-headline", Static).update(
            Text("\n").join([line1, line2])
        )

    def _render_secondary(self, fair, mkt, extra: dict) -> None:
        sec = Text()
        sec.append(f"fair {fair:.3f}  " if fair is not None else "fair -    ",
                   style="#eab308")
        sec.append(f"mkt {mkt:.3f}  " if mkt is not None else "mkt -    ",
                   style="#e2e8f0")
        if fair is not None and mkt is not None:
            d_ = fair - mkt
            sec.append(f"edge {d_:+.3f}  ",
                       style="green" if abs(d_) >= 0.1 else "#94a3b8")
            side = "Up" if d_ > 0 else "Down" if d_ < 0 else "-"
            side_style = "green" if d_ > 0 else "red" if d_ < 0 else "#94a3b8"
            sec.append(f"side {side}  ", style=side_style)
        sec.append(
            f"TP/SL/Res {extra.get('tp_count',0)}/"
            f"{extra.get('sl_count',0)}/{extra.get('resolution_count',0)}",
            style="white",
        )
        self.query_one("#refined-secondary", Static).update(sec)

    def _render_pnl_chart(self, pnl_series) -> None:
        plot = self.query_one("#chart-pnl", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("pro")
        plt.date_form("H:M:S")
        if pnl_series:
            # Rolling visible window: last PNL_VISIBLE_WINDOW points.
            # Persistent series stays as-is; visible window is what's plotted.
            visible = list(pnl_series)[-PNL_VISIBLE_WINDOW:]
            ts = [t for t, _ in visible]
            ys = [v for _, v in visible]
            session_total = pnl_series[-1][1]
            colour = "green" if ys[-1] > 0 else "red" if ys[-1] < 0 else "white"
            plt.plot(_ts_to_hms(ts), ys, color=colour, marker="braille")
            lo, hi = min(ys), max(ys)
            if lo == hi:
                plt.ylim(lo - 0.5, hi + 0.5)
            else:
                pad = (hi - lo) * 0.1
                plt.ylim(lo - pad, hi + pad)
            plt.title(
                f"refined PnL  session ${session_total:+.2f}  "
                f"(viewing last {len(visible)})"
            )
        else:
            plt.title("refined PnL  (no trades)")
        plot.refresh()

    def _render_position(self, pos, mkt) -> None:
        if not pos:
            self.query_one("#refined-position", Static).update(
                Text("no position", style="dim")
            )
            return
        side = pos.get("side", "?")
        side_style = "bold green" if side == "Up" else "bold red"
        entry = pos.get("entry_price") or 0.0
        t = Text()
        t.append("OPEN ", style="bold yellow")
        t.append(f"{side}  ", style=side_style)
        t.append(f"entry {entry:.3f}  ", style="white")
        t.append(f"size ${pos.get('size_usdc', 0):.2f}  ", style="white")
        t.append(f"edge {pos.get('edge', 0):.3f}", style="cyan")
        if mkt is not None:
            realizable = mkt if side == "Up" else 1.0 - mkt
            favor = realizable - entry
            t.append(f"  realizable {realizable:.3f}  favor {favor:+.3f}",
                     style=pnl_color(favor))
        self.query_one("#refined-position", Static).update(t)

    def _render_trades(self, closed_trades) -> None:
        tbl = self.query_one("#refined-trades", DataTable)
        tbl.clear()
        trades = closed_trades[-RECENT_TRADES_CAP:]
        for tr in reversed(trades):
            ts = tr.get("resolved_time") or 0
            tm = time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"
            side = tr.get("side", "?")
            pnl = tr.get("pnl", 0) or 0
            size_usdc = tr.get("size_usdc", 0) or 0
            t_roi = (pnl / size_usdc * 100) if size_usdc else 0
            hold = tr.get("hold_time_s")
            tbl.add_row(
                tm,
                Text(side, style="green" if side == "Up" else "red"),
                f"{tr.get('entry_price', 0):.3f}->{tr.get('exit_price', 0):.3f}",
                f"${size_usdc:.0f}",
                Text(f"{pnl:+.2f}", style=pnl_color(pnl)),
                Text(f"{t_roi:+.1f}%", style=pnl_color(t_roi)),
                tr.get("exit_type", "") or "",
                f"{int(hold)}s" if hold else "-",
            )


class OrderbookWidget(Container):
    """Render the YES-token orderbook: asks desc, spread row, bids desc.

    Depth bars scale to the largest size in the visible book. Adaptive depth:
    the widget shows up to 10 levels per side, capped by available rows.
    """

    HARD_DEPTH_CAP = 10
    BAR_WIDTH = 20

    def compose(self) -> ComposeResult:
        yield Static("", id="book-hdr")
        yield DataTable(id="book-table", zebra_stripes=False, cursor_type="none")

    def on_mount(self) -> None:
        tbl = self.query_one("#book-table", DataTable)
        for col in ("side", "price", "size", "depth"):
            tbl.add_column(col, key=col)

    def render_snapshot(self, snap: OrderbookSnapshot | None) -> None:
        hdr = self.query_one("#book-hdr", Static)
        tbl = self.query_one("#book-table", DataTable)
        tbl.clear()

        if snap is None:
            hdr.update("[dim]book: no poll yet[/]")
            return
        if snap.status == "error":
            hdr.update(f"[dim]no book data (reason: {snap.error})[/]")
            return
        if snap.status == "empty":
            hdr.update("[dim]book empty[/]")
            return

        header = Text()
        header.append(f"bid {snap.best_bid:.3f}  ", style="green")
        header.append(f"ask {snap.best_ask:.3f}  ", style="red")
        header.append(f"spread {snap.spread:.3f}  mid {snap.mid:.3f}",
                      style="#94a3b8")
        hdr.update(header)

        # Adaptive depth: clamp to half of (table rows - 1 spread row).
        # Floor of 3 levels per side keeps the panel populated even on first
        # render before the layout has settled (tbl.size.height starts at 0).
        avail_rows = max(3, tbl.size.height)
        depth = max(3, min(self.HARD_DEPTH_CAP, (avail_rows - 1) // 2))

        asks_top = snap.asks[:depth][::-1]    # render highest ask first
        bids_top = snap.bids[:depth]
        sizes = [s for _, s in asks_top] + [s for _, s in bids_top]
        max_size = max(sizes) if sizes else 1.0

        def bar(size: float) -> str:
            n = int(self.BAR_WIDTH * (size / max_size)) if max_size else 0
            return "#" * n  # ASCII bar; CSS colours via Text style

        for px, sz in asks_top:
            tbl.add_row(
                Text("ASK", style="red"),
                f"{px:.3f}", f"{sz:.1f}",
                Text(bar(sz), style="red"),
            )
        tbl.add_row(
            Text("---", style="#94a3b8"),
            Text("spread", style="#94a3b8"),
            f"{snap.spread:.3f}",
            "",
        )
        for px, sz in bids_top:
            tbl.add_row(
                Text("BID", style="green"),
                f"{px:.3f}", f"{sz:.1f}",
                Text(bar(sz), style="green"),
            )


class BaselinesWidget(Static):
    """Two-line paper-benchmark banner: BASE + ENHANCED stats."""

    def render_state(self, state: dict | None) -> None:
        if state is None:
            self.update("[dim]baselines: waiting[/]")
            return
        lines: list[Text] = []
        for key, label in (("base", "BASE    "), ("enhanced", "ENHANCED")):
            blob = state.get(key) or {}
            stats = _compute_stats(blob.get("stats") or {})
            line = Text()
            line.append(f"{label}  ", style="bold")
            line.append(f"PnL {stats['total_pnl']:+.2f}  ",
                        style=pnl_color(stats['total_pnl']))
            line.append(f"ROI {stats['roi']:+.1f}%  ",
                        style=pnl_color(stats['roi']))
            line.append(f"{stats['total']} tr W={stats['wins']} L={stats['losses']}  ",
                        style="#94a3b8")
            line.append(f"DD ${stats['max_drawdown']:.2f}", style="red")
            lines.append(line)
        self.update(Text("\n").join(lines))


class OrdersLogWidget(RichLog):
    """Streaming log of refined-strategy actions, newest at bottom.

    Tracks already-written actions by fingerprint so repeated ingest() of the
    same EventsTailer deque does not duplicate lines.
    """

    DEFAULT_CSS = ""  # rely on dashboard.tcss for colours

    SEEN_CAP = ACTION_CAP * 10  # bound dedup memory at long-lived sessions

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("max_lines", ACTION_CAP)
        kwargs.setdefault("wrap", False)
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)
        self._seen: deque[int] = deque(maxlen=self.SEEN_CAP)
        self._seen_set: set[int] = set()

    def ingest(self, actions) -> None:
        for a in actions:
            fp = hash((
                a.get("ts"),
                a.get("kind"),
                a.get("strategy"),
                round((a.get("price") or 0.0), 4),
            ))
            if fp in self._seen_set:
                continue
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(fp)
            self._seen_set.add(fp)
            self.write(_render_action_line(a))


class DashboardApp(App):
    CSS_PATH = "dashboard.tcss"
    BINDINGS = [Binding("ctrl+c", "quit", "Quit"), Binding("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        with Container(id="root"):
            yield HeaderWidget(id="header")
            yield LiveCurvesWidget(id="live")
            yield MainStrategyWidget(id="main")
            yield OrderbookWidget(id="book")
            with Container(id="bottom-right"):
                yield BaselinesWidget(id="baselines")
                yield OrdersLogWidget(id="orders-log", max_lines=ACTION_CAP)

    def on_mount(self) -> None:
        self.tailer = EventsTailer(EVENTS_FILE)
        self._resolver = TokenResolver()
        self._poller: OrderbookPoller | None = None
        self._poller_slug: str | None = None
        self._last_book_poll = 0.0
        self._book_worker_inflight = False
        self.set_interval(0.5, self._tick)

    def _ensure_poller(self, slug: str | None) -> None:
        if not slug:
            self._poller = None
            self._poller_slug = None
            return
        if slug == self._poller_slug:
            return  # already attempted this slug (success or fail)
        self._poller_slug = slug
        try:
            tokens = self._resolver.resolve(slug)
        except Exception:
            tokens = None
        if tokens is None:
            self._poller = None
            return
        self._poller = OrderbookPoller(tokens.yes_token_id)

    def _maybe_poll_book(self) -> "OrderbookSnapshot | None":
        """Return the latest snapshot. Trigger a worker poll if cadence elapsed.

        Polling runs in a background thread so the 3 s HTTP timeout never
        blocks the Textual event loop.
        """
        if self._poller is None:
            return None
        now = time.time()
        if (now - self._last_book_poll >= self._poller.current_interval
                and not self._book_worker_inflight):
            self._last_book_poll = now
            self._book_worker_inflight = True
            self.run_worker(
                self._poll_book_worker,
                name="book-poll",
                thread=True,
                exclusive=True,
            )
        return self._poller.last_snapshot

    def _poll_book_worker(self) -> None:
        """Background-thread function: one HTTP call to the CLOB /book endpoint."""
        poller = self._poller
        if poller is None:
            return
        try:
            poller.poll_once()
        finally:
            self._book_worker_inflight = False

    def _tick(self) -> None:
        state = read_json(STATE_FILE)
        self.tailer.update()
        self._ensure_poller(state.get("slug") if state else None)
        snap = self._maybe_poll_book()
        self.query_one(HeaderWidget).render_state(state)
        self.query_one(LiveCurvesWidget).render_state(state)
        self.query_one(MainStrategyWidget).render_state(
            state,
            self.tailer.pnl_series.get("refined", []),
        )
        self.query_one(OrderbookWidget).render_snapshot(snap)
        self.query_one(BaselinesWidget).render_state(state)
        self.query_one(OrdersLogWidget).ingest(self.tailer.refined_actions)


def main() -> int:
    if not STATE_DIR.exists():
        print(f"[dashboard] {STATE_DIR} missing - start the daemon first")
        return 2
    DashboardApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
