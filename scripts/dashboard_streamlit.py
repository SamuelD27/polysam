#!/usr/bin/env python3
"""Streamlit web dashboard for daemon_base_v1.

Mirrors scripts/dashboard.py (Rich TUI) and extends it with filterable
charts. Reads daemon_state/state.json, events.jsonl and daemon.log.

Run standalone:

    streamlit run scripts/dashboard_streamlit.py \\
        --server.port 3006 --server.headless true \\
        --server.fileWatcherType none --browser.gatherUsageStats false

Served on http://localhost:3006 when launched via launch_daemon.sh.
"""

from __future__ import annotations

import json
import time
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Constants ────────────────────────────────────────────────────────────
PORT = 3006
REFRESH_MS = 2000
HISTORY_CAP = 1000
EVENTS_CAP = 500
LOG_TAIL = 30
MARKET_DURATION = 300  # 5-minute cycle

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "daemon_state"
STATE_FILE = STATE_DIR / "state.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
LOG_FILE = STATE_DIR / "daemon.log"

# Terminal palette
COL_GREEN = "#00ff88"
COL_AMBER = "#ffb300"
COL_RED = "#ff4466"
COL_CYAN = "#00d4ff"
COL_DIM = "#888888"
COL_BG = "#0a0a0a"
COL_PANEL = "#1a1a1a"
COL_TEXT = "#e0e0e0"


# ── Data reading utilities ───────────────────────────────────────────────
def read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def tail_log(n: int) -> list[str]:
    if not LOG_FILE.exists():
        return []
    try:
        with LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 4096
            buf = b""
            while size > 0 and buf.count(b"\n") <= n:
                read = min(block, size)
                size -= read
                f.seek(size)
                buf = f.read(read) + buf
            return buf.decode(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def read_new_events(pos: int) -> tuple[list[dict], int]:
    """Incremental read from events.jsonl starting at byte offset pos.

    Returns (new_events, new_pos). Handles rotation/truncation by
    resetting pos to 0 if the file shrank.
    """
    if not EVENTS_FILE.exists():
        return [], pos
    try:
        size = EVENTS_FILE.stat().st_size
        if size < pos:
            pos = 0
        with EVENTS_FILE.open("rb") as f:
            f.seek(pos)
            data = f.read()
            new_pos = f.tell()
    except OSError:
        return [], pos
    out: list[dict] = []
    for line in data.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out, new_pos


def append_to_history(state: dict, history: list[dict]) -> None:
    """Append one chart-row sample from the current state snapshot."""
    btc = state.get("btc_price")
    mp_up = state.get("market_price_up")
    mp_down = (1.0 - mp_up) if isinstance(mp_up, (int, float)) else None
    base = state.get("base") or {}
    enh = state.get("enhanced") or {}
    row = {
        "ts": float(state.get("last_update") or time.time()),
        "btc_price": float(btc) if isinstance(btc, (int, float)) else None,
        "market_price_up": float(mp_up) if isinstance(mp_up, (int, float)) else None,
        "market_price_down": float(mp_down) if mp_down is not None else None,
        "fair_base": float(base.get("fair_price")) if isinstance(base.get("fair_price"), (int, float)) else None,
        "fair_enh": float(enh.get("fair_price")) if isinstance(enh.get("fair_price"), (int, float)) else None,
    }
    # Dedupe: if the last row has identical ts, replace; else append.
    if history and history[-1].get("ts") == row["ts"]:
        history[-1] = row
    else:
        history.append(row)
    # Cap
    if len(history) > HISTORY_CAP:
        del history[: len(history) - HISTORY_CAP]


# ── Event → action extraction (mirrors the TUI) ──────────────────────────
def action_from_event(ev: dict) -> dict | None:
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


def _side_html(side: str) -> str:
    if side == "Up":
        return f'<span style="color:{COL_GREEN}">Up  </span>'
    if side == "Down":
        return f'<span style="color:{COL_RED}">Down</span>'
    return f'<span>{escape(str(side)):<4}</span>'


def _kind_html(kind: str) -> str:
    if kind == "BUY":
        return f'<span style="color:{COL_GREEN};font-weight:bold">BUY </span>'
    if kind == "SELL":
        return f'<span style="color:{COL_AMBER};font-weight:bold">SELL</span>'
    if kind == "RES":
        return f'<span style="color:{COL_CYAN};font-weight:bold">RES </span>'
    return f'<span>{escape(str(kind))}</span>'


def _pnl_html(pnl: float) -> str:
    col = COL_GREEN if pnl > 0 else (COL_RED if pnl < 0 else COL_TEXT)
    return f'<span style="color:{col}">{pnl:+.2f}</span>'


def action_to_html(a: dict, prefix: str | None = None) -> str:
    ts = a.get("ts", 0)
    tm = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
    parts = [f'<span style="color:{COL_DIM}">{tm}</span>  ']
    if prefix:
        parts.append(prefix + " ")
    parts.append(_kind_html(a.get("kind", "?")))
    parts.append(" ")
    parts.append(_side_html(a.get("side", "?")))
    parts.append(" ")
    kind = a.get("kind")
    if kind == "BUY":
        price = a.get("price") or 0.0
        size = a.get("size_usdc") or 0.0
        parts.append(f'@ ${price:.2f}')
        parts.append(f'   <span style="color:{COL_DIM}">(${size:.2f})</span>')
    elif kind == "SELL":
        price = a.get("price") or 0.0
        pnl = a.get("pnl") or 0.0
        parts.append(f'@ ${price:.2f}   ')
        parts.append(_pnl_html(pnl))
        parts.append(f'   <span style="color:{COL_DIM}">{escape(str(a.get("exit_type") or ""))}</span>')
    elif kind == "RES":
        won = a.get("won")
        pnl = a.get("pnl") or 0.0
        if won is True:
            parts.append(f'<span style="color:{COL_GREEN};font-weight:bold">WON </span>')
        elif won is False:
            parts.append(f'<span style="color:{COL_RED};font-weight:bold">LOST</span>')
        else:
            parts.append(f'<span style="color:{COL_CYAN}">RES </span>')
        parts.append("   ")
        parts.append(_pnl_html(pnl))
    return "".join(parts)


# ── CSS — terminal aesthetic ─────────────────────────────────────────────
CSS = f"""
<style>
/* global */
html, body, [class*="css"] {{
    font-family: 'JetBrains Mono', 'Menlo', 'Consolas', monospace !important;
}}
.stApp {{
    background-color: {COL_BG} !important;
    color: {COL_TEXT};
}}
section[data-testid="stSidebar"], .stSidebar {{
    background-color: {COL_PANEL} !important;
}}
.main .block-container {{
    padding-top: 0.8rem;
    padding-bottom: 0.8rem;
    max-width: 100%;
}}
h1, h2, h3, h4, h5, h6 {{
    color: {COL_TEXT} !important;
    font-family: 'JetBrains Mono', 'Menlo', 'Consolas', monospace !important;
    font-weight: 600;
    letter-spacing: 0.02em;
    line-height: 1.2;
    margin-top: 0.4rem;
    margin-bottom: 0.4rem;
}}
hr {{ border-color: #222; margin: 0.5rem 0; }}
/* metrics */
[data-testid="stMetric"] {{
    background-color: {COL_PANEL};
    padding: 0.5rem 0.75rem;
    border: 1px solid #222;
    border-radius: 0;
}}
[data-testid="stMetricLabel"] {{
    color: {COL_DIM} !important;
    font-size: 0.75rem;
    text-transform: lowercase;
    letter-spacing: 0.05em;
}}
[data-testid="stMetricValue"] {{
    font-family: 'JetBrains Mono', 'Menlo', 'Consolas', monospace !important;
    font-size: 1.3rem;
    color: {COL_TEXT};
}}
/* big prices */
.big-price {{
    font-family: 'JetBrains Mono', 'Menlo', 'Consolas', monospace;
    font-size: 48px;
    font-weight: 700;
    letter-spacing: 0.03em;
    padding: 0.4rem 0;
    line-height: 1.0;
}}
.big-price-up   {{ color: {COL_GREEN}; }}
.big-price-down {{ color: {COL_RED}; }}
.big-price-label {{
    font-size: 0.75rem;
    color: {COL_DIM};
    text-transform: lowercase;
    letter-spacing: 0.08em;
}}
/* panels */
.panel {{
    background-color: {COL_PANEL};
    border: 1px solid #222;
    border-radius: 0;
    padding: 0.5rem 0.75rem;
    margin-bottom: 0.5rem;
}}
.panel-title {{
    color: {COL_DIM};
    text-transform: lowercase;
    letter-spacing: 0.08em;
    font-size: 0.75rem;
    margin-bottom: 0.3rem;
    border-bottom: 1px solid #222;
    padding-bottom: 0.2rem;
}}
.orders-log {{
    background-color: {COL_PANEL};
    border: 1px solid #222;
    border-radius: 0;
    padding: 0.5rem 0.75rem;
    font-family: 'JetBrains Mono', 'Menlo', 'Consolas', monospace;
    font-size: 12.5px;
    line-height: 1.45;
    white-space: pre;
    overflow-x: auto;
    max-height: 340px;
    overflow-y: auto;
}}
.orders-log .line {{ white-space: pre; }}
/* dataframes */
[data-testid="stDataFrame"] {{
    background-color: {COL_PANEL};
    border: 1px solid #222;
    border-radius: 0;
}}
/* code blocks */
code, pre, [data-testid="stCodeBlock"] pre {{
    background-color: {COL_PANEL} !important;
    border-radius: 0 !important;
    font-family: 'JetBrains Mono', 'Menlo', 'Consolas', monospace !important;
    font-size: 12px !important;
    line-height: 1.4 !important;
}}
/* progress bar */
[data-testid="stProgress"] > div > div > div > div {{
    background-color: {COL_CYAN};
    border-radius: 0;
}}
[data-testid="stProgress"] > div > div > div {{
    background-color: #222;
    border-radius: 0;
}}
/* multiselect chips */
[data-baseweb="tag"] {{
    border-radius: 0 !important;
    background-color: #222 !important;
}}
/* divider spacer */
.spacer-sm {{ height: 0.35rem; }}
/* dot */
.dot {{ display: inline-block; width: 8px; height: 8px; margin: 0 3px;
        border-radius: 0; }}
.dot-on  {{ background-color: {COL_GREEN}; }}
.dot-off {{ background-color: {COL_RED}; }}
/* header bar */
.hbar {{
    display: flex; gap: 1.5rem; align-items: center;
    background-color: {COL_PANEL};
    border: 1px solid #222;
    padding: 0.45rem 0.75rem;
    font-size: 13px;
    margin-bottom: 0.5rem;
}}
.hbar .lbl {{ color: {COL_DIM}; text-transform: lowercase; margin-right: 0.3rem; }}
.hbar .val {{ color: {COL_TEXT}; }}
.hbar .amb {{ color: {COL_AMBER}; }}
.hbar .grn {{ color: {COL_GREEN}; }}
.hbar .red {{ color: {COL_RED}; }}
.hbar .cyn {{ color: {COL_CYAN}; }}
.hbar .dim {{ color: {COL_DIM}; }}
.hbar .grow {{ flex: 1; }}
.hbar .kill-ok   {{ color: {COL_DIM}; border: 1px solid #222; padding: 0 6px; }}
.hbar .kill-on   {{ color: {COL_RED}; background: {COL_AMBER}; padding: 0 6px; font-weight: bold; }}
/* hide streamlit chrome */
[data-testid="stToolbar"], #MainMenu, footer, header {{ visibility: hidden; }}
</style>
"""


# ── Rendering helpers ────────────────────────────────────────────────────
def render_header(state: dict, kill_active: bool) -> None:
    conn = state.get("connections") or {}
    binance_ok = bool(conn.get("binance"))
    rtds_ok = bool(conn.get("rtds"))

    t_zero = state.get("t_zero") or 0
    elapsed = max(0.0, time.time() - t_zero) if t_zero else 0.0
    remaining = max(0.0, MARKET_DURATION - elapsed)
    frac = min(elapsed / MARKET_DURATION, 1.0) if t_zero else 0.0

    btc = state.get("btc_price") or 0
    sigma = state.get("sigma")
    slug = state.get("slug") or "-"
    strike = state.get("strike")

    def dot(ok: bool) -> str:
        return f'<span class="dot dot-{"on" if ok else "off"}"></span>'

    kill_html = ('<span class="kill-on">KILL</span>' if kill_active
                 else '<span class="kill-ok"> ok </span>')

    sigma_html = f"{sigma*100:.2f}%" if isinstance(sigma, (int, float)) else "-"
    strike_html = f"${strike:,.0f}" if isinstance(strike, (int, float)) else "-"

    st.markdown(
        f'<div class="hbar">'
        f'<span class="lbl">btc</span><span class="val">${btc:,.2f}</span>'
        f'<span class="lbl">σ</span><span class="amb">{sigma_html}</span>'
        f'<span class="lbl">strike</span><span class="dim">{strike_html}</span>'
        f'<span class="lbl">market</span><span class="cyn">{escape(slug)}</span>'
        f'<span class="lbl">cycle</span><span class="val">{int(remaining//60)}:{int(remaining%60):02d}</span>'
        f'<span class="grow"></span>'
        f'<span class="lbl">binance</span>{dot(binance_ok)}'
        f'<span class="lbl">rtds</span>{dot(rtds_ok)}'
        f'{kill_html}'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.progress(frac)


def render_big_prices(state: dict) -> None:
    mp_up = state.get("market_price_up")
    if isinstance(mp_up, (int, float)):
        up = max(0.0, min(1.0, float(mp_up)))
        dn = 1.0 - up
        up_s = f"${up:.2f}"
        dn_s = f"${dn:.2f}"
    else:
        up_s = "-"
        dn_s = "-"

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            f'<div class="panel">'
            f'<div class="big-price-label">UP</div>'
            f'<div class="big-price big-price-up">{up_s}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f'<div class="panel">'
            f'<div class="big-price-label">DOWN</div>'
            f'<div class="big-price big-price-down">{dn_s}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )


def render_charts(history: list[dict]) -> None:
    if not history:
        st.markdown(
            f'<div class="panel"><div class="panel-title">charts</div>'
            f'<span style="color:{COL_DIM}">accumulating data...</span></div>',
            unsafe_allow_html=True,
        )
        return

    df = pd.DataFrame(history)
    df["t"] = pd.to_datetime(df["ts"], unit="s")
    df = df.set_index("t")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown('<div class="panel-title">btc price (usd)</div>', unsafe_allow_html=True)
        btc_opts = ["binance"]
        btc_sel = st.multiselect(
            "btc curves", btc_opts, default=["binance"], key="btc_curves",
            label_visibility="collapsed",
        )
        cols_map = {"binance": "btc_price"}
        cols = [cols_map[k] for k in btc_sel if k in cols_map]
        btc_df = df[cols].dropna(how="all") if cols else pd.DataFrame()
        if cols and not btc_df.empty:
            btc_df = btc_df.rename(columns={v: k for k, v in cols_map.items()})
            st.line_chart(btc_df, height=260)
        else:
            st.markdown(
                f'<span style="color:{COL_DIM}">no data yet</span>',
                unsafe_allow_html=True,
            )

    with c2:
        st.markdown('<div class="panel-title">market / fair (0..1)</div>', unsafe_allow_html=True)
        mkt_opts = ["polymarket_up", "polymarket_down", "fair_base", "fair_enhanced"]
        mkt_default = ["polymarket_up", "polymarket_down", "fair_base"]
        mkt_sel = st.multiselect(
            "market curves", mkt_opts, default=mkt_default, key="mkt_curves",
            label_visibility="collapsed",
        )
        cols_map = {
            "polymarket_up": "market_price_up",
            "polymarket_down": "market_price_down",
            "fair_base": "fair_base",
            "fair_enhanced": "fair_enh",
        }
        cols = [cols_map[k] for k in mkt_sel if k in cols_map]
        mkt_df = df[cols].dropna(how="all") if cols else pd.DataFrame()
        if cols and not mkt_df.empty:
            rename = {cols_map[k]: k for k in mkt_sel if k in cols_map}
            mkt_df = mkt_df.rename(columns=rename)
            st.line_chart(mkt_df, height=260)
        else:
            st.markdown(
                f'<span style="color:{COL_DIM}">no data yet</span>',
                unsafe_allow_html=True,
            )


def render_strategy(name: str, blob: dict, events: list[dict]) -> None:
    stats = blob.get("stats") or {}
    total_pnl = float(stats.get("total_pnl", 0.0) or 0.0)
    total = int(stats.get("total_trades", 0) or 0)
    wins = int(stats.get("wins", 0) or 0)
    losses = int(stats.get("losses", 0) or 0)
    wr = (100.0 * wins / total) if total else 0.0
    risked = float(stats.get("total_risked", 0.0) or 0.0)
    mdd = float(stats.get("max_drawdown", 0.0) or 0.0)
    streak = int(stats.get("current_streak", 0) or 0)
    streak_type = stats.get("streak_type") or ""
    fair = blob.get("fair_price")

    st.markdown(
        f'<div class="panel-title">{escape(name.lower())}'
        f'{" (paper)" if name == "BASE" else " (live-capable)"}</div>',
        unsafe_allow_html=True,
    )

    m1, m2, m3 = st.columns(3)
    m1.metric("pnl", f"{total_pnl:+.2f}")
    m2.metric("trades", f"{total}  W{wins}/L{losses}")
    m3.metric("winrate", f"{wr:.0f}%")
    m4, m5, m6 = st.columns(3)
    m4.metric("risked", f"${risked:.0f}")
    m5.metric("max dd", f"{mdd:.2f}")
    m6.metric("streak", f"{streak} {streak_type}" if streak_type else "-")
    m7, _m8, _m9 = st.columns(3)
    m7.metric("fair", f"{fair:.3f}" if isinstance(fair, (int, float)) else "-")

    # Open position
    pos = blob.get("open_position")
    if pos:
        side = pos.get("side", "?")
        side_col = COL_GREEN if side == "Up" else COL_RED if side == "Down" else COL_TEXT
        rows = [
            ("side", f'<span style="color:{side_col};font-weight:bold">{escape(str(side))}</span>'),
            ("entry px", f'{float(pos.get("entry_price", 0)):.3f}'),
            ("size usdc", f'${float(pos.get("size_usdc", 0)):.2f}'),
            ("size shares", f'{float(pos.get("size_shares", 0)):.1f}'),
            ("edge", f'{float(pos.get("edge", 0)):.3f}'),
            ("source/zone",
             f'{escape(str(pos.get("source") or "-"))}/{escape(str(pos.get("time_zone") or "-"))}'),
        ]
        body = "".join(
            f'<tr><td style="color:{COL_DIM};padding-right:12px">{k}</td>'
            f'<td>{v}</td></tr>'
            for k, v in rows
        )
        st.markdown(
            f'<div class="panel"><div class="panel-title">open position</div>'
            f'<table style="font-size:13px">{body}</table></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="panel"><div class="panel-title">open position</div>'
            f'<span style="color:{COL_DIM}">no position</span></div>',
            unsafe_allow_html=True,
        )

    # Last 5 trades
    trades = (blob.get("closed_trades") or [])[-5:]
    if trades:
        rows = []
        for t in reversed(trades):
            ts = t.get("resolved_time", 0)
            tm = time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"
            side = t.get("side", "?")
            side_col = COL_GREEN if side == "Up" else COL_RED if side == "Down" else COL_TEXT
            pnl = float(t.get("pnl", 0) or 0)
            pnl_col = COL_GREEN if pnl > 0 else COL_RED if pnl < 0 else COL_TEXT
            rows.append(
                f'<tr>'
                f'<td style="color:{COL_DIM};padding-right:10px">{tm}</td>'
                f'<td style="color:{side_col};padding-right:10px">{escape(str(side))}</td>'
                f'<td style="padding-right:10px">{float(t.get("entry_price",0)):.3f}→{float(t.get("exit_price",0)):.3f}</td>'
                f'<td style="color:{pnl_col};padding-right:10px">{pnl:+.2f}</td>'
                f'<td style="color:{COL_DIM}">{escape(str(t.get("exit_type") or ""))}</td>'
                f'</tr>'
            )
        header = (
            f'<tr style="color:{COL_DIM};font-size:11px;text-transform:lowercase">'
            f'<td>t</td><td>side</td><td>px in→out</td><td>pnl</td><td>why</td>'
            f'</tr>'
        )
        st.markdown(
            f'<div class="panel"><div class="panel-title">recent trades</div>'
            f'<table style="font-size:12.5px;width:100%">{header}{"".join(rows)}</table></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="panel"><div class="panel-title">recent trades</div>'
            f'<span style="color:{COL_DIM}">no trades yet</span></div>',
            unsafe_allow_html=True,
        )

    # Orders log — last ~15 for this strategy
    strat_key = "base" if name == "BASE" else "enhanced"
    actions = []
    for ev in events:
        if (ev.get("strategy") or "") != strat_key:
            continue
        a = action_from_event(ev)
        if a is not None:
            actions.append(a)
    actions = actions[-15:]
    if actions:
        lines = "\n".join(
            f'<div class="line">{action_to_html(a)}</div>' for a in actions
        )
    else:
        lines = f'<span style="color:{COL_DIM}">no orders yet</span>'
    st.markdown(
        f'<div class="panel"><div class="panel-title">orders</div>'
        f'<div class="orders-log">{lines}</div></div>',
        unsafe_allow_html=True,
    )


def render_enhanced_extras(state: dict) -> None:
    enh = state.get("enhanced") or {}
    extra = enh.get("extra") or {}
    squeeze = extra.get("squeeze_active")
    spike = float(extra.get("spike_score", 0) or 0)
    tp = int(extra.get("tp_count", 0) or 0)
    sl = int(extra.get("sl_count", 0) or 0)
    res = int(extra.get("resolution_count", 0) or 0)
    edge_n = int(extra.get("edge_trades", 0) or 0)
    sq_n = int(extra.get("squeeze_trades", 0) or 0)
    last_exit = extra.get("last_exit_type") or "-"

    cols = st.columns(6)
    cols[0].metric("squeeze", "active" if squeeze else "-")
    cols[1].metric("spike", f"{spike:.2f}")
    cols[2].metric("tp/sl/res", f"{tp}/{sl}/{res}")
    cols[3].metric("edge/sq", f"{edge_n}/{sq_n}")
    cols[4].metric("last exit", str(last_exit))
    mp_up = state.get("market_price_up")
    cols[5].metric(
        "market up",
        f"{mp_up:.3f}" if isinstance(mp_up, (int, float)) else "-",
    )


def render_footer(events: list[dict], log_lines: list[str]) -> None:
    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown(
            f'<div class="panel-title">daemon.log (last {LOG_TAIL})</div>',
            unsafe_allow_html=True,
        )
        text = "\n".join(log_lines) if log_lines else "(no log yet)"
        st.code(text, language=None)
    with c2:
        st.markdown('<div class="panel-title">unified stream</div>', unsafe_allow_html=True)
        actions = []
        for ev in events:
            a = action_from_event(ev)
            if a is None:
                continue
            actions.append(a)
        actions = actions[-15:]
        if actions:
            lines_html = []
            for a in actions:
                strat = a.get("strategy") or ""
                if strat == "enhanced":
                    prefix = f'<span style="color:{COL_CYAN}">[E]</span>'
                elif strat == "base":
                    prefix = '<span>[B]</span>'
                else:
                    prefix = f'<span style="color:{COL_DIM}">[?]</span>'
                lines_html.append(f'<div class="line">{action_to_html(a, prefix=prefix)}</div>')
            body = "\n".join(lines_html)
        else:
            body = f'<span style="color:{COL_DIM}">no stream yet</span>'
        st.markdown(f'<div class="orders-log">{body}</div>', unsafe_allow_html=True)


# ── Main ─────────────────────────────────────────────────────────────────
def main() -> None:
    st.set_page_config(
        page_title="polymarket-hustle",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Inject CSS + auto-refresh meta tag.
    st.markdown(
        f'<meta http-equiv="refresh" content="{REFRESH_MS // 1000}">',
        unsafe_allow_html=True,
    )
    st.markdown(CSS, unsafe_allow_html=True)

    # Init session state
    if "history" not in st.session_state:
        st.session_state.history = []
    if "events" not in st.session_state:
        st.session_state.events = []
    if "events_pos" not in st.session_state:
        st.session_state.events_pos = 0

    # Read fresh
    state = read_state()
    log_lines = tail_log(LOG_TAIL)

    # Incremental events tail
    new_events, new_pos = read_new_events(st.session_state.events_pos)
    st.session_state.events_pos = new_pos
    if new_events:
        st.session_state.events.extend(new_events)
        if len(st.session_state.events) > EVENTS_CAP:
            del st.session_state.events[: len(st.session_state.events) - EVENTS_CAP]

    # History sample
    if state:
        append_to_history(state, st.session_state.history)

    # Sidebar — status + pointers
    with st.sidebar:
        st.markdown(
            f'<div style="color:{COL_CYAN};font-weight:bold;font-size:14px">'
            f'polymarket-hustle</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div style="color:{COL_DIM};font-size:11px">'
            f'port {PORT} &middot; refresh {REFRESH_MS}ms</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<hr/>', unsafe_allow_html=True)
        status = "online" if state else "waiting"
        status_col = COL_GREEN if state else COL_AMBER
        st.markdown(
            f'<div><span style="color:{COL_DIM}">daemon: </span>'
            f'<span style="color:{status_col}">{status}</span></div>',
            unsafe_allow_html=True,
        )
        if state:
            last = state.get("last_update")
            age = (time.time() - last) if isinstance(last, (int, float)) else None
            if age is not None:
                st.markdown(
                    f'<div><span style="color:{COL_DIM}">state age: </span>'
                    f'<span>{age:.1f}s</span></div>',
                    unsafe_allow_html=True,
                )
        st.markdown(
            f'<div style="color:{COL_DIM};font-size:11px;margin-top:12px">'
            f'history: {len(st.session_state.history)}/{HISTORY_CAP}<br/>'
            f'events: {len(st.session_state.events)}/{EVENTS_CAP}'
            f'</div>',
            unsafe_allow_html=True,
        )

    # Waiting placeholder — dashboard boots cleanly without daemon.
    if state is None:
        st.markdown(
            f'<div class="panel"><div class="panel-title">waiting</div>'
            f'<span style="color:{COL_AMBER}">waiting for {STATE_FILE} ... '
            f'start the daemon with ./launch_daemon.sh</span></div>',
            unsafe_allow_html=True,
        )
        render_charts(st.session_state.history)
        render_footer(st.session_state.events, log_lines)
        return

    kill_active = (STATE_DIR / "KILL").exists()

    # 1. Header
    render_header(state, kill_active)

    # 2. Big live prices
    render_big_prices(state)

    # 3. Charts
    render_charts(st.session_state.history)

    # 4. Two-column strategy section
    bc, ec = st.columns(2)
    with bc:
        render_strategy("BASE", state.get("base") or {}, st.session_state.events)
    with ec:
        render_strategy("ENHANCED", state.get("enhanced") or {}, st.session_state.events)

    # 5. Enhanced extras
    render_enhanced_extras(state)

    # 6. Footer
    render_footer(st.session_state.events, log_lines)


if __name__ == "__main__":
    main()
