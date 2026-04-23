#!/usr/bin/env python3
"""Live Textual TUI dashboard for daemon_base_v1 (read-only).

Reads daemon_state/state.json, events.jsonl, daemon.log -- never writes.
Renders at ~2 Hz via a single _tick() that reads state.json once per frame.
Launch:  conda activate polymarket-env && python3 scripts/dashboard.py
Ctrl-C exits cleanly; launch_daemon.sh stops the daemon on exit.
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Static, DataTable, RichLog

REPO = Path(__file__).resolve().parent.parent
STATE_DIR   = REPO / "daemon_state"
STATE_FILE  = STATE_DIR / "state.json"
LOG_FILE    = STATE_DIR / "daemon.log"
EVENTS_FILE = STATE_DIR / "events.jsonl"
KILL_FILE   = STATE_DIR / "KILL"

RECENT_TRADES_CAP = 5
ACTION_CAP = 50
PRICE_HISTORY_CAP = 300       # ~5 min at 1 Hz state writes
PNL_SERIES_CAP = 2000         # per-strategy decimation cap


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


def _strip_emoji(s: str) -> str:
    """Drop any non-ASCII characters except common box/block drawing.

    plotext occasionally injects unicode markers; this keeps our render ASCII-clean.
    """
    return "".join(
        ch for ch in s
        if ord(ch) < 128 or ch in "█░▁▂▃▄▅▆▇─│┌┐└┘├┤┬┴┼"
    )


MARKET_DURATION = 300


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

        # Per-strategy cumulative PnL — only realized events count.
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
        bar_w = 30
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
    def compose(self) -> ComposeResult:
        yield Static("UP - / DOWN -", id="hero-prices")
        yield Static("btc chart placeholder", id="chart-btc")
        yield Static("market/fair chart placeholder", id="chart-mkt")


class MainStrategyWidget(Container):
    def compose(self) -> ComposeResult:
        yield Static("refined headline placeholder", id="refined-headline")
        yield Static("refined secondary placeholder", id="refined-secondary")
        yield Static("pnl chart placeholder", id="chart-pnl")
        yield Static("no position", id="refined-position")
        yield DataTable(id="refined-trades")


class OrderbookWidget(Container):
    def compose(self) -> ComposeResult:
        yield Static("book header placeholder", id="book-hdr")
        yield DataTable(id="book-table")


class BaselinesWidget(Static):
    def on_mount(self) -> None:
        self.update("baselines placeholder")


class OrdersLogWidget(RichLog):
    pass


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
        self.set_interval(0.5, self._tick)

    def _tick(self) -> None:
        state = read_json(STATE_FILE)
        self.query_one(HeaderWidget).render_state(state)


def main() -> int:
    if not STATE_DIR.exists():
        print(f"[dashboard] {STATE_DIR} missing - start the daemon first")
        return 2
    DashboardApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
