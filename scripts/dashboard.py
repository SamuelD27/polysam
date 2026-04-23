#!/usr/bin/env python3
"""Live Textual TUI dashboard for daemon_base_v1 (read-only).

Reads daemon_state/state.json, events.jsonl, daemon.log -- never writes.
Renders at ~2 Hz via a single _tick() that reads state.json once per frame.
Launch:  conda activate polymarket-env && python3 scripts/dashboard.py
Ctrl-C exits cleanly; launch_daemon.sh stops the daemon on exit.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Static, DataTable, RichLog

import json

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


class HeaderWidget(Static):
    def on_mount(self) -> None:
        self.update("header - waiting for daemon")


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


def main() -> int:
    if not STATE_DIR.exists():
        print(f"[dashboard] {STATE_DIR} missing - start the daemon first")
        return 2
    DashboardApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
