#!/usr/bin/env python3
"""Live TUI dashboard for daemon_base_v1.

Reads daemon_state/state.json (refreshed by the daemon every 5s) and
daemon_state/daemon.log + events.jsonl (streamed live), renders a single
full-screen layout that refreshes at ~2 Hz.

Run in its own terminal alongside the daemon:

    conda activate polymarket-env
    python3 scripts/dashboard.py

Ctrl-C to exit.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "daemon_state"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "daemon.log"
EVENTS_FILE = STATE_DIR / "events.jsonl"
KILL_FILE = STATE_DIR / "KILL"

MARKET_DURATION = 300
REFRESH_HZ = 2

SPARK_CHARS = " ▁▂▃▄▅▆▇█"
SPARK_MIN_WIDTH = 20
SPARK_MAX_WIDTH = 120
PRICE_HISTORY_CAP = 80
ACTION_CAP = 50
LOG_TAIL_LINES = 10
RECENT_TRADES_CAP = 5


@dataclass(frozen=True)
class TailerSnapshot:
    base_actions: tuple[dict, ...]
    enh_actions: tuple[dict, ...]
    unified_actions: tuple[dict, ...]
    events: tuple[dict, ...]


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def tail_lines(path: Path, n: int = 8) -> list[str]:
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
    if v > 0:
        return "bold green"
    if v < 0:
        return "bold red"
    return "white"


def conn_pill(ok: bool) -> Text:
    return Text("●", style="green") if ok else Text("●", style="red")


def fmt_secs(s: float) -> str:
    if s is None:
        return "–"
    return f"{int(s // 60):d}:{int(s % 60):02d}"


def build_header(d: dict) -> Panel:
    conn = d.get("connections", {})
    binance_ok = bool(conn.get("binance"))
    rtds_ok = bool(conn.get("rtds"))

    t_zero = d.get("t_zero") or 0
    elapsed = time.time() - t_zero if t_zero else 0
    remaining = max(0.0, MARKET_DURATION - elapsed)
    bar_width = 40
    filled = int(bar_width * min(elapsed / MARKET_DURATION, 1.0))
    bar = "█" * filled + "░" * (bar_width - filled)

    kill = "KILL" if KILL_FILE.exists() else " ok "
    kill_style = "bold red on yellow" if KILL_FILE.exists() else "dim green"

    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(justify="left")
    t.add_column(justify="center")
    t.add_column(justify="right")

    left = Text()
    left.append("BTC  ", style="bold cyan")
    left.append(f"${d.get('btc_price', 0):>10,.2f}", style="bold")
    left.append("  σ=")
    left.append(f"{(d.get('sigma') or 0)*100:.2f}%", style="magenta")
    left.append(f"  strike ${d.get('strike') or 0:,.0f}", style="dim")

    center = Text()
    center.append("Market  ", style="bold")
    center.append(d.get("slug") or "–", style="yellow")
    center.append(f"  [{bar}]  ", style="dim")
    center.append(f"{fmt_secs(remaining)} left", style="cyan")

    right = Text()
    right.append("binance ", style="dim")
    right.append_text(conn_pill(binance_ok))
    right.append("  rtds ", style="dim")
    right.append_text(conn_pill(rtds_ok))
    right.append("  [")
    right.append(kill, style=kill_style)
    right.append("]")

    t.add_row(left, center, right)
    return Panel(t, border_style="bright_blue", title="polymarket-hustle", title_align="left")


def _action_from_event(ev: dict) -> dict | None:
    """Convert an event into a compact action dict for the order logs.

    Returns None for events that aren't trade actions.
    """
    t = ev.get("type")
    ts = ev.get("ts", 0)
    strategy = ev.get("strategy") or ""
    if t == "entry_filled":
        pos = ev.get("position") or {}
        return {
            "ts": ts,
            "kind": "BUY",
            "strategy": strategy,
            "side": pos.get("side", "?"),
            "price": pos.get("entry_price", 0.0),
            "size_usdc": pos.get("size_usdc", 0.0),
            "pnl": None,
            "exit_type": None,
            "won": None,
        }
    if t == "exit_filled":
        tr = ev.get("trade") or {}
        return {
            "ts": ts,
            "kind": "SELL",
            "strategy": strategy,
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
            "ts": ts,
            "kind": "RES",
            "strategy": strategy,
            "side": tr.get("side", "?"),
            "price": tr.get("exit_price", 0.0),
            "size_usdc": tr.get("size_usdc", 0.0),
            "pnl": tr.get("pnl", 0.0),
            "exit_type": tr.get("exit_type", "RESOLUTION"),
            "won": bool(tr.get("won")),
        }
    return None


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


def _render_action_line(a: dict, *, prefix: Text | None = None) -> Text:
    """Render one action as a single Text line."""
    ts = a.get("ts", 0)
    tm = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
    line = Text()
    line.append(tm, style="dim")
    line.append("  ")
    if prefix is not None:
        line.append_text(prefix)
        line.append(" ")
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


def build_orders_panel(actions: tuple[dict, ...] | deque[dict]) -> Panel:
    if not actions:
        body: Text | Group = Text("no orders yet", style="dim")
    else:
        lines = [_render_action_line(a) for a in list(actions)]
        body = Group(*lines)
    return Panel(body, title="orders", border_style="grey37", title_align="left")


def build_strategy_panel(name: str, blob: dict,
                         actions: tuple[dict, ...] | deque[dict],
                         open_pos_key: str = "open_position") -> Panel:
    stats = blob.get("stats", {})
    pos = blob.get(open_pos_key)
    trades = blob.get("closed_trades", [])[-RECENT_TRADES_CAP:]

    total_pnl = stats.get("total_pnl", 0.0)
    total = stats.get("total_trades", 0) or 0
    wins = stats.get("wins", 0) or 0
    losses = stats.get("losses", 0) or 0
    wr = 100.0 * wins / total if total else 0.0
    total_risked = stats.get("total_risked", 0.0) or 0.0
    roi = (total_pnl / total_risked * 100) if total_risked else 0.0

    summary = Table.grid(expand=True, padding=(0, 1))
    summary.add_column(justify="left")
    summary.add_column(justify="right")
    summary.add_row(
        Text("PnL", style="dim"),
        Text(f"{total_pnl:+.2f}", style=pnl_color(total_pnl)),
    )
    summary.add_row(
        Text("trades", style="dim"),
        Text(f"{total}  W={wins} L={losses}  {wr:.0f}%", style=""),
    )
    summary.add_row(
        Text("risked", style="dim"),
        Text(f"${total_risked:.0f}  ROI={roi:+.1f}%", style=pnl_color(roi)),
    )
    summary.add_row(
        Text("drawdown", style="dim"),
        Text(f"{stats.get('max_drawdown', 0):.2f}", style="red"),
    )
    streak_type = stats.get("streak_type") or ""
    streak = stats.get("current_streak", 0) or 0
    s_style = "green" if streak_type == "W" else "red" if streak_type == "L" else "dim"
    summary.add_row(
        Text("streak", style="dim"),
        Text(f"{streak} {streak_type}" if streak_type else "–", style=s_style),
    )

    fair = blob.get("fair_price")
    summary.add_row(
        Text("fair", style="dim"),
        Text(f"{fair:.3f}" if fair is not None else "–", style="cyan"),
    )

    # Open position
    if pos:
        pos_tbl = Table.grid(expand=True, padding=(0, 1))
        pos_tbl.add_column(justify="left")
        pos_tbl.add_column(justify="right")
        side = pos.get("side", "?")
        side_style = "bold green" if side == "Up" else "bold red"
        pos_tbl.add_row("OPEN", Text(side, style=side_style))
        pos_tbl.add_row("entry px", f"{pos.get('entry_price', 0):.3f}")
        pos_tbl.add_row("size", f"${pos.get('size_usdc', 0):.2f}  ({pos.get('size_shares', 0):.1f} sh)")
        pos_tbl.add_row("edge", f"{pos.get('edge', 0):.3f}")
        if pos.get("source"):
            pos_tbl.add_row("source", f"{pos.get('source')}/{pos.get('time_zone') or '–'}")
        pos_panel = Panel(pos_tbl, title="position", border_style="yellow", title_align="left")
    else:
        pos_panel = Panel(Align.center(Text("no position", style="dim")), title="position",
                          border_style="grey37", title_align="left")

    # Recent trades (capped at RECENT_TRADES_CAP)
    trade_tbl = Table(box=None, show_header=True, expand=True, padding=(0, 1))
    trade_tbl.add_column("t", style="dim", width=6)
    trade_tbl.add_column("side", width=5)
    trade_tbl.add_column("px in→out", width=14)
    trade_tbl.add_column("pnl", justify="right", width=8)
    trade_tbl.add_column("why", width=6)
    for t in reversed(trades):
        ts = t.get("resolved_time", 0)
        tm = time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"
        side = t.get("side", "?")
        side_style = "green" if side == "Up" else "red"
        pnl = t.get("pnl", 0)
        trade_tbl.add_row(
            tm,
            Text(side, style=side_style),
            f"{t.get('entry_price', 0):.3f}→{t.get('exit_price', 0):.3f}",
            Text(f"{pnl:+.2f}", style=pnl_color(pnl)),
            t.get("exit_type", ""),
        )
    if not trades:
        trade_tbl.add_row("–", "–", "–", "–", "–")

    orders_panel = build_orders_panel(actions)

    body = Group(
        summary,
        pos_panel,
        Panel(trade_tbl, title="recent trades", border_style="grey37", title_align="left"),
        orders_panel,
    )
    if name == "ENHANCED":
        title = "[bold cyan]ENHANCED[/] [yellow](live-capable)[/]"
        border = "cyan"
    else:
        title = "[bold white]BASE[/] [dim](paper benchmark)[/]"
        border = "white"
    return Panel(body, title=title, border_style=border, title_align="left")


def build_extras_panel(d: dict) -> Panel:
    extra = d.get("enhanced", {}).get("extra", {}) or {}
    market_up = d.get("market_price_up")
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(justify="left")
    tbl.add_column(justify="left")
    tbl.add_column(justify="left")
    tbl.add_column(justify="left")
    tbl.add_row(
        Text("market Up", style="dim"),
        Text(f"{market_up:.3f}" if market_up is not None else "–",
             style="yellow"),
        Text("squeeze", style="dim"),
        Text("active" if extra.get("squeeze_active") else "—",
             style="magenta" if extra.get("squeeze_active") else "dim"),
    )
    tbl.add_row(
        Text("spike", style="dim"),
        f"{extra.get('spike_score', 0):.2f}",
        Text("last exit", style="dim"),
        str(extra.get("last_exit_type") or "–"),
    )
    tbl.add_row(
        Text("tp/sl/res", style="dim"),
        f"{extra.get('tp_count', 0)}/{extra.get('sl_count', 0)}/{extra.get('resolution_count', 0)}",
        Text("edge/sq", style="dim"),
        f"{extra.get('edge_trades', 0)}/{extra.get('squeeze_trades', 0)}",
    )
    return Panel(tbl, title="enhanced extras", border_style="magenta", title_align="left")


def build_log_panel(lines: list[str]) -> Panel:
    body = Text("\n".join(lines), style="grey70", overflow="ellipsis")
    return Panel(body, title=f"daemon.log (last {LOG_TAIL_LINES})",
                 border_style="grey37", title_align="left")


def _render_prices_line(market_up: float | None) -> Text:
    line = Text(no_wrap=True, overflow="crop")
    if market_up is None:
        line.append("UP   —  ", style="dim")
        line.append(" " * 7)
        line.append("DOWN   —", style="dim")
        return line
    up = max(0.0, min(1.0, float(market_up)))
    down = 1.0 - up
    line.append(f"UP  ${up:.2f}", style="bold bright_green")
    line.append(" " * 7)
    line.append(f"DOWN  ${down:.2f}", style="bold bright_red")
    return line


def _render_sparkline(prices: deque[float], width: int) -> Text:
    line = Text()
    if not prices:
        line.append("no price history", style="dim")
        return line
    data = list(prices)[-width:]
    lo = min(data)
    hi = max(data)
    if hi == lo:
        bars = "▄" * len(data)
    else:
        span = hi - lo
        n = len(SPARK_CHARS) - 1
        bars = "".join(
            SPARK_CHARS[max(0, min(n, int((v - lo) / span * n)))]
            for v in data
        )
    line.append(f"${lo:,.0f}  ", style="dim")
    line.append(bars, style="bright_cyan")
    line.append(f"  ${hi:,.0f}", style="dim")
    return line


def _render_stream_line(a: dict) -> Text:
    strat = a.get("strategy") or ""
    if strat == "enhanced":
        prefix = Text("[E]", style="cyan")
    elif strat == "base":
        prefix = Text("[B]", style="white")
    else:
        prefix = Text("[?]", style="dim")
    return _render_action_line(a, prefix=prefix)


def build_live_panel(d: dict, prices: deque[float],
                     unified: tuple[dict, ...] | deque[dict],
                     *, console_width: int) -> Panel:
    market_up = d.get("market_price_up") if d else None
    prices_line = Align.center(_render_prices_line(market_up))

    # Live panel occupies ~2/3 of total console width; subtract ~14 chars for
    # panel padding/borders and the "$lo  " / "  $hi" labels on the sparkline.
    spark_width = max(
        SPARK_MIN_WIDTH,
        min(SPARK_MAX_WIDTH, console_width * 2 // 3 - 6 - 14),
    )
    spark_line = Align.center(_render_sparkline(prices, width=spark_width))

    stream = list(unified)[-5:]
    if stream:
        stream_body: Text | Group = Group(*(_render_stream_line(a) for a in stream))
    else:
        stream_body = Text("no stream yet", style="dim")
    stream_panel = Panel(stream_body, title="stream", border_style="grey37",
                         title_align="left", padding=(0, 1))

    body = Group(prices_line, Text(""), spark_line, Text(""), stream_panel)
    return Panel(body, title="live", border_style="bright_blue", title_align="left")


def render(d: dict, log_lines: list[str], snap: TailerSnapshot,
           prices: deque[float], *, console_width: int) -> Layout:
    if d is None:
        return Layout(Panel(Align.center(Text("waiting for daemon_state/state.json…",
                                               style="dim")), border_style="red"))

    layout = Layout(name="root")
    layout.split(
        Layout(build_header(d), size=3, name="header"),
        Layout(name="body"),
        Layout(build_extras_panel(d), size=5, name="extras"),
        Layout(name="footer", size=14),
    )
    layout["body"].split_row(
        Layout(build_strategy_panel("BASE", d.get("base", {}),
                                    snap.base_actions), name="base"),
        Layout(build_strategy_panel("ENHANCED", d.get("enhanced", {}),
                                    snap.enh_actions), name="enh"),
    )
    layout["footer"].split_row(
        Layout(build_log_panel(log_lines), name="log", ratio=1),
        Layout(build_live_panel(d, prices, snap.unified_actions,
                                console_width=console_width),
               name="live", ratio=2),
    )
    return layout


class EventsTailer:
    def __init__(self, path: Path, cap: int = 200):
        self.path = path
        self.events: deque[dict] = deque(maxlen=cap)
        self.base_actions: deque[dict] = deque(maxlen=ACTION_CAP)
        self.enh_actions: deque[dict] = deque(maxlen=ACTION_CAP)
        self.unified_actions: deque[dict] = deque(maxlen=ACTION_CAP)
        self._pos = 0

    def _dispatch(self, ev: dict) -> None:
        action = _action_from_event(ev)
        if action is None:
            return
        strat = action.get("strategy")
        if strat == "base":
            self.base_actions.append(action)
        elif strat == "enhanced":
            self.enh_actions.append(action)
        self.unified_actions.append(action)

    def update(self) -> None:
        if not self.path.exists():
            return
        try:
            size = self.path.stat().st_size
            if size < self._pos:
                self._pos = 0  # file rotated/truncated
            with self.path.open("rb") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
            for line in data.decode(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                self.events.append(ev)
                self._dispatch(ev)
        except OSError:
            return

    def snapshot(self) -> TailerSnapshot:
        return TailerSnapshot(
            base_actions=tuple(self.base_actions),
            enh_actions=tuple(self.enh_actions),
            unified_actions=tuple(self.unified_actions),
            events=tuple(self.events),
        )


def main() -> int:
    console = Console()
    if not STATE_DIR.exists():
        console.print(f"[red]{STATE_DIR} missing — start the daemon first[/]")
        return 2

    tailer = EventsTailer(EVENTS_FILE)
    prices: deque[float] = deque(maxlen=PRICE_HISTORY_CAP)
    with Live(console=console, refresh_per_second=REFRESH_HZ, screen=True) as live:
        while True:
            d = read_json(STATE_FILE)
            log_lines = tail_lines(LOG_FILE, LOG_TAIL_LINES)
            tailer.update()
            snap = tailer.snapshot()
            if d:
                btc = d.get("btc_price")
                if isinstance(btc, (int, float)) and btc > 0:
                    prices.append(float(btc))
            live.update(render(d, log_lines, snap, prices,
                               console_width=console.size.width))
            time.sleep(1.0 / REFRESH_HZ)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[dashboard] exit")
