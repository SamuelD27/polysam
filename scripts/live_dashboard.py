#!/usr/bin/env python3
"""Live web dashboard for daemon_base_v1 (aiohttp + websocket).

Replaces the retired streamlit dashboard. Tails daemon_state files and
streams deltas to connected browsers via a WebSocket. Frontend uses
TradingView Lightweight Charts for in-place live appends.

    python scripts/live_dashboard.py --port 3006

Open http://localhost:3006.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType, web

# ── Constants ────────────────────────────────────────────────────────────
PORT_DEFAULT = 3006
REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "daemon_state"
STATE_FILE = STATE_DIR / "state.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
HISTORY_FILE = STATE_DIR / "dashboard_history.jsonl"

HISTORY_DISK_CAP = 5000
_HISTORY_AVG_LINE_BYTES = 140
_HISTORY_ROTATE_HYSTERESIS = 1.5
_ROTATE_EVERY_N_TICKS = 30
_rotate_tick = 0

_PERSIST_WARNED = False

EVENTS_BOOTSTRAP = 50      # events sent on new WS connect

# Tailer cadences
STATE_POLL_HZ = 10
EVENTS_POLL_HZ = 20

COL_GREEN = "#00ff88"
COL_AMBER = "#ffb300"
COL_RED   = "#ff4466"
COL_CYAN  = "#00d4ff"
COL_DIM   = "#888888"
COL_BG    = "#0a0a0a"
COL_PANEL = "#1a1a1a"
COL_TEXT  = "#e0e0e0"


# ── HTML template ────────────────────────────────────────────────────────
INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<title>polymarket-hustle</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root {
    --green: __GREEN__; --amber: __AMBER__; --red: __RED__; --cyan: __CYAN__;
    --dim: __DIM__; --bg: __BG__; --panel: __PANEL__; --text: __TEXT__;
  }
  html, body { margin:0; padding:0; background:var(--bg); color:var(--text);
    font-family: 'JetBrains Mono', Menlo, Consolas, monospace; font-size:13px; }
  .wrap { padding: 1rem 1rem; max-width: none; margin: 0; }
  .panel { background:var(--panel); border:1px solid #2a2a2a; padding:0.9rem 1rem; margin-bottom:1rem; }
  .panel-title { color:var(--dim); text-transform:lowercase; letter-spacing:0.08em;
    font-size:0.75rem; margin-bottom:0.4rem; border-bottom:1px solid #2a2a2a; padding-bottom:0.25rem; }
  .hbar { display:flex; gap:1.5rem; align-items:center; }
  .hbar .lbl { color:var(--dim); text-transform:lowercase; margin-right:0.3rem; }
  .hbar .val { color:var(--text); }
  .hbar .grow { flex:1; }
  .dot { display:inline-block; width:8px; height:8px; margin:0 3px; }
  .dot-on { background:var(--green); }
  .dot-off { background:var(--red); }
  .kill-ok { color:var(--dim); border:1px solid #222; padding:0 6px; }
  .kill-on { color:var(--red); background:var(--amber); padding:0 6px; font-weight:bold; }
  .big-price { font-size:48px; font-weight:700; letter-spacing:0.03em; line-height:1.0; padding:0.4rem 0; }
  .big-price-up { color:var(--green); }
  .big-price-down { color:var(--red); }
  .big-price-label { font-size:0.75rem; color:var(--dim); text-transform:lowercase; letter-spacing:0.08em; }
  .two-col { display:grid; grid-template-columns: 1fr 1fr; gap:1rem; }
  .chart { height: 500px; }
  .spark { height: 80px; }
  .stale { border:1px solid var(--red); background:#2a0b13; color:var(--red); font-size:13px; }
  #prog { height:3px; background:#222; }
  #prog > div { height:100%; background:var(--cyan); width:0%; transition: width 0.3s linear; }
  .stream { max-height:520px; overflow-y:auto; font-size:14px; line-height:1.55; white-space:pre; }
  .stream .line { white-space:pre; padding:2px 0; }
  table { font-size:12.5px; border-collapse:collapse; width:100%; }
  td { padding:1px 10px 1px 0; }
  .metric-row { display:grid; grid-template-columns: repeat(3, 1fr); gap:0.6rem; }
  .metric { background:var(--panel); border:1px solid #2a2a2a; padding:0.6rem 0.85rem; }
  .metric .lbl { color:var(--dim); font-size:0.75rem; text-transform:lowercase; letter-spacing:0.05em; }
  .metric .val { font-size:1.3rem; }
  .win-buttons { display:flex; gap:0.3rem; margin-bottom:0.3rem; }
  .win-buttons button { background:var(--panel); color:var(--text); border:1px solid #222;
    font-family:inherit; font-size:12px; padding:2px 8px; cursor:pointer; }
  .win-buttons button.active { color:var(--cyan); border-color:var(--cyan); }
  .curve-toggles { display:flex; gap:1rem; font-size:12px; color:var(--text); margin-bottom:0.3rem; align-items:center; }
  .curve-toggles label { cursor:pointer; display:inline-flex; align-items:center; gap:4px; }
  .sw { display:inline-block; width:14px; height:3px; vertical-align:middle; }
  .sw-dashed { background: repeating-linear-gradient(90deg, currentColor 0 4px, transparent 4px 7px); height:3px; }
  .sw-binance         { background: var(--cyan); }
  .sw-polymarket_up   { background: var(--green); }
  .sw-polymarket_down { background: var(--red); }
  .sw-fair_base       { color: var(--amber); }
  .sw-fair_enhanced   { color: #bbbbbb; }
  pre { background:var(--panel); border:1px solid #222; padding:0.5rem 0.75rem;
    font-family:inherit; font-size:12px; line-height:1.4; max-height:340px; overflow-y:auto; margin:0; }
</style>
</head>
<body>
<div class="wrap">
  <div id="stale" class="panel stale" style="display:none"></div>
  <div class="panel hbar" id="header">
    <span><span class="lbl">btc</span><span class="val" id="h-btc">-</span></span>
    <span><span class="lbl">sigma</span><span class="val" style="color:var(--amber)" id="h-sigma">-</span></span>
    <span><span class="lbl">strike</span><span class="val" style="color:var(--dim)" id="h-strike">-</span></span>
    <span><span class="lbl">market</span><span class="val" style="color:var(--cyan)" id="h-slug">-</span></span>
    <span><span class="lbl">cycle</span><span class="val" id="h-cycle">-</span></span>
    <span class="grow"></span>
    <span><span class="lbl">binance</span><span class="dot dot-off" id="h-binance"></span></span>
    <span><span class="lbl">rtds</span><span class="dot dot-off" id="h-rtds"></span></span>
    <span class="kill-ok" id="h-kill">ok</span>
  </div>
  <div id="prog"><div id="prog-fill"></div></div>

  <div class="two-col">
    <div class="panel"><div class="big-price-label">UP</div>
      <div class="big-price big-price-up" id="price-up">-</div></div>
    <div class="panel"><div class="big-price-label">DOWN</div>
      <div class="big-price big-price-down" id="price-down">-</div></div>
  </div>

  <div class="panel">
    <div class="win-buttons" id="win-buttons">
      <button data-win="1m">1m</button>
      <button data-win="5m" class="active">5m</button>
      <button data-win="15m">15m</button>
      <button data-win="1h">1h</button>
      <button data-win="all">all</button>
    </div>
    <div class="two-col">
      <div>
        <div class="panel-title">btc price (usd)</div>
        <div class="curve-toggles">
          <label><input type="checkbox" data-curve="binance" checked><span class="sw sw-binance"></span>binance</label>
        </div>
        <div id="chart-btc" class="chart"></div>
      </div>
      <div>
        <div class="panel-title">market / fair (0..1)</div>
        <div class="curve-toggles">
          <label><input type="checkbox" data-curve="polymarket_up" checked><span class="sw sw-polymarket_up"></span>polymarket_up</label>
          <label><input type="checkbox" data-curve="polymarket_down" checked><span class="sw sw-polymarket_down"></span>polymarket_down</label>
          <label><input type="checkbox" data-curve="fair_base" checked><span class="sw sw-dashed sw-fair_base"></span>fair_base</label>
          <label><input type="checkbox" data-curve="fair_enhanced"><span class="sw sw-dashed sw-fair_enhanced"></span>fair_enhanced</label>
        </div>
        <div id="chart-mkt" class="chart"></div>
      </div>
    </div>
  </div>

  <div class="two-col">
    <div class="panel" id="strat-base"><div class="panel-title">base (paper)</div></div>
    <div class="panel" id="strat-enh"><div class="panel-title">enhanced (live-capable)</div></div>
  </div>

  <div class="panel"><div class="panel-title">enhanced extras</div>
    <div id="extras" class="hbar"></div></div>

  <div class="panel"><div class="panel-title">unified stream</div>
    <div class="stream" id="stream">(no stream yet)</div></div>
</div>

<script>
  const WS_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  const STALE_THRESHOLD_S = 15.0;
  const MARKET_DURATION = 300;

  let lastState = null;
  let ws;

  function $(id) { return document.getElementById(id); }
  function fmtUSD(v) { return '$' + v.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}); }
  function fmtSecs(s) {
    const m = Math.max(0, Math.floor(s/60)), r = Math.max(0, Math.floor(s%60));
    return m + ':' + r.toString().padStart(2,'0');
  }

  const PALETTE = {
    binance:         '__CYAN__',
    polymarket_up:   '__GREEN__',
    polymarket_down: '__RED__',
    fair_base:       '__AMBER__',
    fair_enhanced:   '#bbbbbb',
  };

  const WINDOW_SECS = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, 'all': null };
  let currentWindow = '5m';

  function mkChart(container) {
    const chart = LightweightCharts.createChart(container, {
      // autoSize uses ResizeObserver so the chart tracks its container on
      // viewport / panel-layout changes; without it the chart is locked to
      // whatever width the container had at createChart() time.
      autoSize: true,
      layout: {
        background: { type: 'solid', color: '__PANEL__' },
        textColor: '__TEXT__',
        fontFamily: "'JetBrains Mono', Menlo, Consolas, monospace",
        fontSize: 11,
      },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      rightPriceScale: { borderColor: '#222' },
      timeScale: { timeVisible: true, secondsVisible: true, borderColor: '#222', rightOffset: 2 },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      handleScroll: true,
      handleScale: true,
    });
    return chart;
  }

  const btcChart = mkChart(document.getElementById('chart-btc'));
  const mktChart = mkChart(document.getElementById('chart-mkt'));

  // Belt-and-braces: explicitly resize on window events in case autoSize isn't
  // honored by the CDN version.
  window.addEventListener('resize', () => {
    for (const c of [btcChart, mktChart]) {
      const el = c.chartElement ? c.chartElement().parentElement : null;
      if (el) c.applyOptions({ width: el.clientWidth, height: 500 });
    }
  });
  // Run a single sync after layout settles so the charts claim full width
  // even if createChart happened before CSS finished applying.
  requestAnimationFrame(() => {
    for (const id of ['chart-btc', 'chart-mkt']) {
      const el = document.getElementById(id);
      if (!el) continue;
      const chart = (id === 'chart-btc') ? btcChart : mktChart;
      chart.applyOptions({ width: el.clientWidth, height: 500 });
    }
  });

  const series = {
    binance:         btcChart.addLineSeries({ color: PALETTE.binance,         lineWidth: 2, priceFormat: { type: 'price', precision: 2, minMove: 0.01 } }),
    polymarket_up:   mktChart.addLineSeries({ color: PALETTE.polymarket_up,   lineWidth: 2, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } }),
    polymarket_down: mktChart.addLineSeries({ color: PALETTE.polymarket_down, lineWidth: 2, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } }),
    fair_base:       mktChart.addLineSeries({ color: PALETTE.fair_base,       lineWidth: 2, lineStyle: 2, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } }),
    fair_enhanced:   mktChart.addLineSeries({ color: PALETTE.fair_enhanced,   lineWidth: 2, lineStyle: 2, priceFormat: { type: 'price', precision: 3, minMove: 0.001 }, visible: false }),
  };

  // Client-side ring buffer for each curve; supports window-selector rescoping.
  const buf = { binance: [], polymarket_up: [], polymarket_down: [], fair_base: [], fair_enhanced: [] };
  const BUF_MAX = 5000;

  function pushSample(key, time, value) {
    const arr = buf[key];
    if (arr.length && arr[arr.length - 1].time === time) {
      arr[arr.length - 1].value = value;
    } else {
      arr.push({ time, value });
      if (arr.length > BUF_MAX) arr.splice(0, arr.length - BUF_MAX);
    }
    series[key].update({ time, value });
  }

  function latestBufTime() {
    let t = 0;
    for (const k of Object.keys(buf)) {
      const arr = buf[k];
      if (arr.length) t = Math.max(t, arr[arr.length - 1].time);
    }
    return t;
  }

  function applyWindow() {
    const secs = WINDOW_SECS[currentWindow];
    if (secs == null) {
      btcChart.timeScale().fitContent();
      mktChart.timeScale().fitContent();
      return;
    }
    // Anchor the right edge to the latest data point (+ small pad), not to
    // wall-clock now — otherwise there's an always-visible empty strip on
    // the right equal to the gap between the last sample and now.
    const latest = latestBufTime() || (Date.now() / 1000);
    const pad = Math.max(2, Math.floor(secs * 0.02));
    const to = latest + pad;
    btcChart.timeScale().setVisibleRange({ from: to - secs, to });
    mktChart.timeScale().setVisibleRange({ from: to - secs, to });
  }
  setInterval(applyWindow, 1000);

  document.querySelectorAll('#win-buttons button').forEach(b => {
    b.onclick = () => {
      currentWindow = b.dataset.win;
      document.querySelectorAll('#win-buttons button').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      applyWindow();
    };
  });

  document.querySelectorAll('.curve-toggles input').forEach(cb => {
    cb.onchange = () => {
      const key = cb.dataset.curve;
      if (series[key]) series[key].applyOptions({ visible: cb.checked });
    };
  });

  async function bootstrapHistory() {
    try {
      const rows = await fetch('/history').then(r => r.json());
      const seeded = { binance: [], polymarket_up: [], polymarket_down: [], fair_base: [], fair_enhanced: [] };
      for (const r of rows) {
        const t = Math.floor(r.ts);
        if (typeof r.btc_price === 'number')         seeded.binance.push({ time: t, value: r.btc_price });
        if (typeof r.market_price_up === 'number')   seeded.polymarket_up.push({ time: t, value: r.market_price_up });
        if (typeof r.market_price_down === 'number') seeded.polymarket_down.push({ time: t, value: r.market_price_down });
        if (typeof r.fair_base === 'number')         seeded.fair_base.push({ time: t, value: r.fair_base });
        if (typeof r.fair_enh === 'number')          seeded.fair_enhanced.push({ time: t, value: r.fair_enh });
      }
      for (const k of Object.keys(seeded)) {
        if (seeded[k].length) {
          series[k].setData(seeded[k]);
          buf[k] = seeded[k].slice();
        }
      }
    } catch (e) { /* ignore bootstrap failure */ }
  }

  function pnlColor(v) { return v > 0 ? 'var(--green)' : v < 0 ? 'var(--red)' : 'var(--text)'; }

  function mtmPnl(pos, mktUp) {
    if (!pos || typeof mktUp !== 'number' || !isFinite(mktUp)) return null;
    const entry = pos.entry_price, shares = pos.size_shares, size = pos.size_usdc;
    if (![entry, shares, size].every(v => typeof v === 'number' && isFinite(v))) return null;
    if (size <= 0) return null;
    const up = Math.max(0, Math.min(1, mktUp));
    let exitPx;
    if (pos.side === 'Up') exitPx = up;
    else if (pos.side === 'Down') exitPx = 1 - up;
    else return null;
    const pnl = (exitPx - entry) * shares;
    return { pnl, pct: pnl / size * 100 };
  }

  const sparkCharts = {};
  function renderSparkline(containerId, closedTrades) {
    const container = document.getElementById(containerId);
    if (!container) return;
    let cum = 0;
    const pts = [];
    for (const t of closedTrades) {
      if (typeof t.pnl !== 'number' || typeof t.resolved_time !== 'number') continue;
      cum += t.pnl;
      pts.push({ time: Math.floor(t.resolved_time), value: cum });
    }
    if (!pts.length) {
      container.innerHTML = '<span style="color:var(--dim)">no trades yet</span>';
      return;
    }
    const last = pts[pts.length - 1].value;
    const color = last >= 0 ? '__GREEN__' : '__RED__';
    if (sparkCharts[containerId]) {
      sparkCharts[containerId].series.setData(pts);
      sparkCharts[containerId].series.applyOptions({ color });
      return;
    }
    container.innerHTML = '';
    const chart = LightweightCharts.createChart(container, {
      autoSize: true,
      layout: {
        background: { type: 'solid', color: '__PANEL__' },
        textColor: '__TEXT__',
        fontFamily: "'JetBrains Mono', Menlo, Consolas, monospace",
        fontSize: 10,
      },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      rightPriceScale: { visible: false },
      timeScale: { visible: false },
      handleScroll: false, handleScale: false,
      height: 80,
    });
    const line = chart.addLineSeries({ color, lineWidth: 1.5 });
    line.setData(pts);
    sparkCharts[containerId] = { chart, series: line };
  }

  function renderStrategyPanel(containerId, title, subtitle, blob, mktUp, key) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const stats = blob.stats || {};
    const pos = blob.open_position;
    const trades = (blob.closed_trades || []).slice(-5).reverse();
    const fair = blob.fair_price;

    const total_pnl = Number(stats.total_pnl ?? 0);
    const total    = Number(stats.total_trades ?? 0);
    const wins     = Number(stats.wins ?? 0);
    const losses   = Number(stats.losses ?? 0);
    const wr       = total ? 100 * wins / total : 0;
    const risked   = Number(stats.total_risked ?? 0);
    const mdd      = Number(stats.max_drawdown ?? 0);
    const streak   = Number(stats.current_streak ?? 0);
    const stype    = stats.streak_type || '';

    let html = '<div class="panel-title">' + title + (subtitle ? ' <span style="color:var(--dim)">' + subtitle + '</span>' : '') + '</div>';
    html += '<div class="metric-row">'
      + '<div class="metric"><div class="lbl">pnl</div><div class="val" style="color:' + pnlColor(total_pnl) + '">' + total_pnl.toFixed(2) + '</div></div>'
      + '<div class="metric"><div class="lbl">trades</div><div class="val">' + total + '  W' + wins + '/L' + losses + '</div></div>'
      + '<div class="metric"><div class="lbl">winrate</div><div class="val">' + wr.toFixed(0) + '%</div></div>'
      + '</div>';
    html += '<div class="metric-row" style="margin-top:0.3rem">'
      + '<div class="metric"><div class="lbl">risked</div><div class="val">$' + risked.toFixed(0) + '</div></div>'
      + '<div class="metric"><div class="lbl">max dd</div><div class="val">' + mdd.toFixed(2) + '</div></div>'
      + '<div class="metric"><div class="lbl">streak</div><div class="val">' + (stype ? streak + ' ' + stype : '-') + '</div></div>'
      + '</div>';
    html += '<div class="metric-row" style="margin-top:0.3rem">'
      + '<div class="metric"><div class="lbl">fair</div><div class="val">' + (typeof fair === 'number' ? fair.toFixed(3) : '-') + '</div></div>'
      + '<div></div><div></div></div>';

    html += '<div class="panel-title" style="margin-top:0.5rem">cumulative pnl</div>';
    html += '<div id="spark-' + key + '" class="spark"></div>';

    html += '<div class="panel-title">open position</div>';
    if (pos) {
      const sideCol = pos.side === 'Up' ? 'var(--green)' : pos.side === 'Down' ? 'var(--red)' : 'var(--text)';
      const mtm = mtmPnl(pos, mktUp);
      const mtmCell = mtm
        ? '<span style="color:' + pnlColor(mtm.pnl) + '">' + (mtm.pnl >= 0 ? '+' : '') + mtm.pnl.toFixed(2) + '</span> '
          + '<span style="color:var(--dim)">(' + (mtm.pct >= 0 ? '+' : '') + mtm.pct.toFixed(1) + '%)</span>'
        : '<span style="color:var(--dim)">-</span>';
      html += '<table>'
        + '<tr><td style="color:var(--dim)">side</td><td><span style="color:' + sideCol + ';font-weight:bold">' + (pos.side || '?') + '</span></td></tr>'
        + '<tr><td style="color:var(--dim)">entry px</td><td>' + Number(pos.entry_price ?? 0).toFixed(3) + '</td></tr>'
        + '<tr><td style="color:var(--dim)">size usdc</td><td>$' + Number(pos.size_usdc ?? 0).toFixed(2) + '</td></tr>'
        + '<tr><td style="color:var(--dim)">size shares</td><td>' + Number(pos.size_shares ?? 0).toFixed(1) + '</td></tr>'
        + '<tr><td style="color:var(--dim)">edge</td><td>' + Number(pos.edge ?? 0).toFixed(3) + '</td></tr>'
        + '<tr><td style="color:var(--dim)">source/zone</td><td>' + (pos.source || '-') + '/' + (pos.time_zone || '-') + '</td></tr>'
        + '<tr><td style="color:var(--dim)">mtm</td><td>' + mtmCell + '</td></tr>'
        + '</table>';
    } else {
      html += '<span style="color:var(--dim)">no position</span>';
    }

    html += '<div class="panel-title">recent trades</div>';
    if (trades.length) {
      html += '<table>'
        + '<tr style="color:var(--dim);font-size:11px;text-transform:lowercase">'
        + '<td>t</td><td>side</td><td>px in&rarr;out</td><td>pnl</td><td>why</td></tr>';
      for (const t of trades) {
        const tm = t.resolved_time ? new Date(t.resolved_time * 1000).toTimeString().slice(0, 5) : '--:--';
        const sideCol = t.side === 'Up' ? 'var(--green)' : t.side === 'Down' ? 'var(--red)' : 'var(--text)';
        const pnl = Number(t.pnl || 0);
        html += '<tr>'
          + '<td style="color:var(--dim)">' + tm + '</td>'
          + '<td style="color:' + sideCol + '">' + (t.side || '?') + '</td>'
          + '<td>' + Number(t.entry_price || 0).toFixed(3) + '&rarr;' + Number(t.exit_price || 0).toFixed(3) + '</td>'
          + '<td style="color:' + pnlColor(pnl) + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '</td>'
          + '<td style="color:var(--dim)">' + (t.exit_type || '') + '</td>'
          + '</tr>';
      }
      html += '</table>';
    } else {
      html += '<span style="color:var(--dim)">no trades yet</span>';
    }

    // Orders log — Task 7 fills this.
    html += '<div class="panel-title">orders</div>';
    html += '<div class="stream" id="orders-' + key + '">(none yet)</div>';

    el.innerHTML = html;
    // innerHTML replaced the spark container; drop the cached chart ref
    // (it's bound to the now-detached old node) so renderSparkline rebuilds.
    delete sparkCharts['spark-' + key];
    renderSparkline('spark-' + key, blob.closed_trades || []);
  }

  function renderExtras(enh, mktUp) {
    const x = enh.extra || {};
    const fmt = v => (typeof v === 'number' && isFinite(v) ? v.toFixed(2) : '-');
    document.getElementById('extras').innerHTML = ''
      + '<span><span class="lbl">squeeze</span><span class="val" style="color:' + (x.squeeze_active ? 'var(--amber)' : 'var(--dim)') + '">' + (x.squeeze_active ? 'active' : '-') + '</span></span>'
      + '<span><span class="lbl">spike</span><span class="val">' + fmt(x.spike_score) + '</span></span>'
      + '<span><span class="lbl">tp/sl/res</span><span class="val">' + (x.tp_count || 0) + '/' + (x.sl_count || 0) + '/' + (x.resolution_count || 0) + '</span></span>'
      + '<span><span class="lbl">edge/sq</span><span class="val">' + (x.edge_trades || 0) + '/' + (x.squeeze_trades || 0) + '</span></span>'
      + '<span><span class="lbl">last exit</span><span class="val">' + (x.last_exit_type || '-') + '</span></span>'
      + '<span><span class="lbl">market up</span><span class="val">' + (typeof mktUp === 'number' ? mktUp.toFixed(3) : '-') + '</span></span>';
  }

  function renderStrategies(d) {
    const mktUp = d.market_price_up;
    renderStrategyPanel('strat-base', 'base', '(paper)', d.base || {}, mktUp, 'base');
    renderStrategyPanel('strat-enh',  'enhanced', '(live-capable)', d.enhanced || {}, mktUp, 'enhanced');
    renderExtras(d.enhanced || {}, mktUp);
  }

  const STREAM_CAP = 15;

  function eventToLine(ev) {
    const t = ev.type;
    const strat = ev.strategy || '';
    const ts = ev.ts || 0;
    const tm = ts ? new Date(ts * 1000).toTimeString().slice(0, 8) : '--:--:--';
    const prefix = strat === 'enhanced' ? '<span style="color:var(--cyan)">[E]</span>'
                 : strat === 'base'     ? '<span>[B]</span>'
                 : '<span style="color:var(--dim)">[?]</span>';
    const sideHtml = side => side === 'Up'
      ? '<span style="color:var(--green)">Up  </span>'
      : side === 'Down'
        ? '<span style="color:var(--red)">Down</span>'
        : '<span>' + (side || '?') + '</span>';
    if (t === 'entry_filled') {
      const p = ev.position || {};
      return '<span style="color:var(--dim)">' + tm + '</span>  '
        + prefix + ' <span style="color:var(--green);font-weight:bold">BUY </span> '
        + sideHtml(p.side) + ' @ $' + Number(p.entry_price || 0).toFixed(2)
        + '   <span style="color:var(--dim)">($' + Number(p.size_usdc || 0).toFixed(2) + ')</span>';
    }
    if (t === 'exit_filled' || t === 'resolve') {
      const tr = ev.trade || {};
      const pnl = Number(tr.pnl || 0);
      const pnlCol = pnl > 0 ? 'var(--green)' : pnl < 0 ? 'var(--red)' : 'var(--text)';
      const pnlHtml = '<span style="color:' + pnlCol + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '</span>';
      if (t === 'resolve') {
        const won = tr.won;
        const wonHtml = won === true ? '<span style="color:var(--green);font-weight:bold">WON </span>'
                      : won === false ? '<span style="color:var(--red);font-weight:bold">LOST</span>'
                      : '<span style="color:var(--cyan)">RES </span>';
        return '<span style="color:var(--dim)">' + tm + '</span>  '
          + prefix + ' <span style="color:var(--cyan);font-weight:bold">RES </span> '
          + sideHtml(tr.side) + ' ' + wonHtml + '   ' + pnlHtml;
      }
      return '<span style="color:var(--dim)">' + tm + '</span>  '
        + prefix + ' <span style="color:var(--amber);font-weight:bold">SELL</span> '
        + sideHtml(tr.side) + ' @ $' + Number(tr.exit_price || 0).toFixed(2)
        + '   ' + pnlHtml
        + '   <span style="color:var(--dim)">' + (tr.exit_type || '') + '</span>';
    }
    return null;
  }

  function prependLine(container, html) {
    const div = document.createElement('div');
    div.className = 'line';
    div.innerHTML = html;
    container.insertBefore(div, container.firstChild);
    while (container.children.length > STREAM_CAP) container.removeChild(container.lastChild);
  }

  function handleEvent(ev) {
    const html = eventToLine(ev);
    if (!html) return;
    const stream = document.getElementById('stream');
    if (stream && stream.textContent.trim() === '(no stream yet)') stream.textContent = '';
    if (stream) prependLine(stream, html);
    const strat = ev.strategy;
    if (strat === 'base' || strat === 'enhanced') {
      const ol = document.getElementById('orders-' + strat);
      if (ol) {
        if (ol.textContent.trim() === '(none yet)') ol.textContent = '';
        prependLine(ol, html);
      }
    }
  }

  function applyState(d) {
    lastState = d;
    $('h-btc').textContent    = (typeof d.btc_price === 'number') ? fmtUSD(d.btc_price) : '-';
    $('h-sigma').textContent  = (typeof d.sigma === 'number') ? (d.sigma*100).toFixed(2) + '%' : '-';
    $('h-strike').textContent = (typeof d.strike === 'number') ? '$' + Math.round(d.strike).toLocaleString('en-US') : '-';
    $('h-slug').textContent   = d.slug || '-';
    const conn = d.connections || {};
    $('h-binance').className = 'dot ' + (conn.binance ? 'dot-on' : 'dot-off');
    $('h-rtds').className    = 'dot ' + (conn.rtds    ? 'dot-on' : 'dot-off');
    if (typeof d.market_price_up === 'number') {
      const up = Math.max(0, Math.min(1, d.market_price_up));
      $('price-up').textContent   = '$' + up.toFixed(2);
      $('price-down').textContent = '$' + (1 - up).toFixed(2);
    } else {
      $('price-up').textContent = '-';
      $('price-down').textContent = '-';
    }
    const kill = d._kill === true;
    $('h-kill').className = kill ? 'kill-on' : 'kill-ok';
    $('h-kill').textContent = kill ? 'KILL' : 'ok';

    // Live chart appends. Use btc_ts if available, else last_update, else now.
    const t = (typeof d.btc_ts === 'number') ? Math.floor(d.btc_ts)
            : (typeof d.last_update === 'number') ? Math.floor(d.last_update)
            : Math.floor(Date.now() / 1000);
    if (typeof d.btc_price === 'number') pushSample('binance', t, d.btc_price);
    if (typeof d.market_price_up === 'number') {
      const up = Math.max(0, Math.min(1, d.market_price_up));
      pushSample('polymarket_up', t, up);
      pushSample('polymarket_down', t, 1 - up);
    }
    const fb = d.base && d.base.fair_price;
    const fe = d.enhanced && d.enhanced.fair_price;
    if (typeof fb === 'number') pushSample('fair_base', t, fb);
    if (typeof fe === 'number') pushSample('fair_enhanced', t, fe);

    renderStrategies(d);
  }

  function tickCycle() {
    if (!lastState || !lastState.t_zero) {
      $('h-cycle').textContent = '-';
      $('prog-fill').style.width = '0%';
      $('stale').style.display = 'none';
      return;
    }
    const elapsed = Math.max(0, Date.now()/1000 - lastState.t_zero);
    const remaining = Math.max(0, MARKET_DURATION - elapsed);
    $('h-cycle').textContent = fmtSecs(remaining);
    $('prog-fill').style.width = Math.min(100, (elapsed / MARKET_DURATION) * 100).toFixed(1) + '%';

    if (typeof lastState.last_update === 'number') {
      const age = (Date.now()/1000) - lastState.last_update;
      const banner = $('stale');
      if (age > STALE_THRESHOLD_S) {
        banner.style.display = 'block';
        banner.innerHTML = '<span style="font-weight:bold">STALE</span> state.json last updated '
          + age.toFixed(0) + 's ago (threshold ' + STALE_THRESHOLD_S.toFixed(0)
          + 's) - daemon may be hung';
      } else {
        banner.style.display = 'none';
      }
    }
  }
  setInterval(tickCycle, 200);

  function handleMessage(msg) {
    if (msg.type === 'state') applyState(msg.data);
    else if (msg.type === 'event') handleEvent(msg.data);
  }

  function connect() {
    ws = new WebSocket(WS_URL);
    ws.onmessage = ev => { try { handleMessage(JSON.parse(ev.data)); } catch (e) {} };
    ws.onclose = () => setTimeout(connect, 1000);
    ws.onerror = () => ws && ws.close();
  }
  bootstrapHistory().then(connect);
</script>
</body></html>
"""

INDEX_HTML = (
    INDEX_HTML
    .replace("__GREEN__", COL_GREEN)
    .replace("__AMBER__", COL_AMBER)
    .replace("__RED__", COL_RED)
    .replace("__CYAN__", COL_CYAN)
    .replace("__DIM__", COL_DIM)
    .replace("__BG__", COL_BG)
    .replace("__PANEL__", COL_PANEL)
    .replace("__TEXT__", COL_TEXT)
)


# ── Tailers + helpers ─────────────────────────────────────────────────────
def bootstrap_events_from_file() -> list[dict]:
    """Read the last EVENTS_BOOTSTRAP events from disk on startup."""
    if not EVENTS_FILE.exists():
        return []
    try:
        with EVENTS_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            buf = b""
            while size > 0 and buf.count(b"\n") <= EVENTS_BOOTSTRAP:
                read = min(block, size)
                size -= read
                f.seek(size)
                buf = f.read(read) + buf
    except OSError:
        return []
    out: list[dict] = []
    for line in buf.decode(errors="replace").splitlines()[-EVENTS_BOOTSTRAP:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _warn_persist_once(op: str, err: Exception) -> None:
    global _PERSIST_WARNED
    if _PERSIST_WARNED:
        return
    _PERSIST_WARNED = True
    print(f"[live] history persistence failed ({op}): {err!r}",
          file=sys.stderr, flush=True)


def _history_row_from_state(data: dict) -> dict | None:
    """Project a state snapshot into a single chart-history row."""
    btc = data.get("btc_price")
    mp_up = data.get("market_price_up")
    mp_down = (1.0 - mp_up) if isinstance(mp_up, (int, float)) else None
    base = data.get("base") or {}
    enh = data.get("enhanced") or {}
    return {
        "ts": float(data.get("last_update") or time.time()),
        "btc_price": float(btc) if isinstance(btc, (int, float)) else None,
        "market_price_up": float(mp_up) if isinstance(mp_up, (int, float)) else None,
        "market_price_down": float(mp_down) if mp_down is not None else None,
        "fair_base": float(base.get("fair_price")) if isinstance(base.get("fair_price"), (int, float)) else None,
        "fair_enh": float(enh.get("fair_price")) if isinstance(enh.get("fair_price"), (int, float)) else None,
    }


def append_history_to_disk(row: dict) -> None:
    try:
        with HISTORY_FILE.open("ab") as f:
            f.write(json.dumps(row).encode() + b"\n")
    except OSError as e:
        _warn_persist_once("append", e)


def rotate_history_if_needed() -> None:
    """Rotate HISTORY_FILE if it exceeds cap. Throttled to every N ticks."""
    global _rotate_tick
    _rotate_tick = (_rotate_tick + 1) % _ROTATE_EVERY_N_TICKS
    if _rotate_tick != 0:
        return
    try:
        size = HISTORY_FILE.stat().st_size
    except OSError:
        return
    est = size // _HISTORY_AVG_LINE_BYTES
    if est <= int(HISTORY_DISK_CAP * _HISTORY_ROTATE_HYSTERESIS):
        return
    rows = load_history_from_disk()
    tmp = HISTORY_FILE.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("wb") as f:
            for r in rows[-HISTORY_DISK_CAP:]:
                f.write(json.dumps(r).encode() + b"\n")
        os.replace(tmp, HISTORY_FILE)
    except OSError as e:
        _warn_persist_once("rotate", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def load_history_from_disk() -> list[dict]:
    """Return the last HISTORY_DISK_CAP rows from HISTORY_FILE as dicts."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with HISTORY_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 4096
            buf = b""
            while size > 0 and buf.count(b"\n") <= HISTORY_DISK_CAP:
                read = min(block, size)
                size -= read
                f.seek(size)
                buf = f.read(read) + buf
    except OSError:
        return []
    out: list[dict] = []
    for line in buf.decode(errors="replace").splitlines()[-HISTORY_DISK_CAP:]:
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


async def state_tailer(app: web.Application) -> None:
    """Poll STATE_FILE mtime at STATE_POLL_HZ; broadcast on change."""
    interval = 1.0 / STATE_POLL_HZ
    last_mtime = 0.0
    while True:
        try:
            st = STATE_FILE.stat()
        except OSError:
            await asyncio.sleep(interval)
            continue
        if st.st_mtime != last_mtime:
            last_mtime = st.st_mtime
            try:
                data = json.loads(STATE_FILE.read_text())
            except (OSError, ValueError):
                await asyncio.sleep(interval)
                continue  # file mid-write; retry next tick
            app["last_state"] = data
            await broadcast(app, {
                "type": "state",
                "data": {**data, "_kill": (STATE_DIR / "KILL").exists()},
            })
            row = _history_row_from_state(data)
            if row is not None and app.get("_last_persisted_ts") != row["ts"]:
                append_history_to_disk(row)
                app["_last_persisted_ts"] = row["ts"]
                rotate_history_if_needed()
        await asyncio.sleep(interval)


async def events_tailer(app: web.Application) -> None:
    """Tail EVENTS_FILE from current EOF; broadcast each new event."""
    interval = 1.0 / EVENTS_POLL_HZ
    pos = EVENTS_FILE.stat().st_size if EVENTS_FILE.exists() else 0
    app["events_pos"] = pos
    while True:
        try:
            st = EVENTS_FILE.stat()
        except OSError:
            await asyncio.sleep(interval)
            continue
        size = st.st_size
        if size < pos:
            pos = 0  # rotated/truncated
        if size > pos:
            try:
                with EVENTS_FILE.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                await asyncio.sleep(interval)
                continue
            app["events_pos"] = pos
            for line in chunk.decode(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                app["recent_events"].append(ev)
                if len(app["recent_events"]) > 1000:
                    del app["recent_events"][: len(app["recent_events"]) - 1000]
                await broadcast(app, {"type": "event", "data": ev})
        await asyncio.sleep(interval)


# ── Routes ───────────────────────────────────────────────────────────────
async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def handle_history(request: web.Request) -> web.Response:
    return web.json_response(load_history_from_disk())


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)
    app = request.app
    app["clients"].add(ws)
    try:
        # Client may drop mid-bootstrap (flaky network); don't let a send
        # failure abort the handler — cleanup still happens in `finally`.
        try:
            if app.get("last_state") is not None:
                await ws.send_json({
                    "type": "state",
                    "data": {**app["last_state"], "_kill": (STATE_DIR / "KILL").exists()},
                })
            for ev in app.get("recent_events", [])[-EVENTS_BOOTSTRAP:]:
                await ws.send_json({"type": "event", "data": ev})
        except (ConnectionResetError, RuntimeError):
            return ws
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        app["clients"].discard(ws)
    return ws


async def broadcast(app: web.Application, payload: dict) -> None:
    """Send one JSON message to every live client."""
    data = json.dumps(payload)
    stale: list[web.WebSocketResponse] = []
    for ws in app["clients"]:
        try:
            await ws.send_str(data)
        except (ConnectionResetError, RuntimeError):
            # RuntimeError: "WebSocket connection is closed" after client abort.
            # Any send error on a dead client must not propagate to producer tasks.
            stale.append(ws)
    for ws in stale:
        app["clients"].discard(ws)


# ── App lifecycle ────────────────────────────────────────────────────────
async def on_startup(app: web.Application) -> None:
    app["clients"] = set()
    app["last_state"] = None
    app["_last_persisted_ts"] = None
    app["recent_events"] = bootstrap_events_from_file()
    app["tasks"] = [
        asyncio.create_task(state_tailer(app), name="state_tailer"),
        asyncio.create_task(events_tailer(app), name="events_tailer"),
    ]


async def on_cleanup(app: web.Application) -> None:
    for t in app.get("tasks", []):
        t.cancel()
    for t in app.get("tasks", []):
        try:
            await t
        except asyncio.CancelledError:
            pass
    for ws in list(app["clients"]):
        await ws.close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", handle_index)
    app.router.add_get("/history", handle_history)
    app.router.add_get("/ws", handle_ws)
    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    # Default to loopback; pass --host 0.0.0.0 to expose on LAN.
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if not STATE_DIR.exists():
        print(f"[live] WARNING: {STATE_DIR} missing; dashboard will show waiting state",
              file=sys.stderr)

    print(f"[live] serving http://{args.host}:{args.port}", flush=True)
    web.run_app(build_app(), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
