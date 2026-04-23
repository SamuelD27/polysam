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

REPO = Path(__file__).resolve().parent.parent
STATE_DIR   = REPO / "daemon_state"
STATE_FILE  = STATE_DIR / "state.json"
LOG_FILE    = STATE_DIR / "daemon.log"
EVENTS_FILE = STATE_DIR / "events.jsonl"
KILL_FILE   = STATE_DIR / "KILL"

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
                yield OrdersLogWidget(id="orders-log", max_lines=50)


def main() -> int:
    if not STATE_DIR.exists():
        print(f"[dashboard] {STATE_DIR} missing - start the daemon first")
        return 2
    DashboardApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
