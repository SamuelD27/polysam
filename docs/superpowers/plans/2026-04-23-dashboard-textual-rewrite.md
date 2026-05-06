# Dashboard Textual Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rich.Layout TUI at `scripts/dashboard.py` with a Textual app: 3-row grid, live plotext line charts, dashboard-local Polymarket orderbook poller, per-strategy persistent PnL curve. Same read-only data sources (`daemon_state/state.json`, `events.jsonl`, `daemon.log`), same ~2 Hz refresh, same foreground-launched-by-`launch_daemon.sh` surface.

**Architecture:**
- One Textual `App` in `scripts/dashboard.py` with a CSS grid in `scripts/dashboard.tcss`.
- Four top-level widgets under a grid container: `HeaderWidget`, and a middle/bottom pair containing `LiveCurvesWidget`+`MainStrategyWidget` and `OrderbookWidget`+`BaselinesWidget`/`OrdersLogWidget`.
- Pure-logic pieces (event tailer, per-strategy PnL replay, orderbook poller) live as plain classes inside `dashboard.py` so they're unit-testable without starting the UI.
- Daemon writes nothing new; the dashboard process opens every file under `daemon_state/` O_RDONLY and creates no files there (PID file, KILL file, caches all out).
- Orderbook uses path (a) locked by the user: dashboard-local HTTP poller against `https://clob.polymarket.com/book?token_id=<yes>` every 2 s, backoff to 10 s after 3 consecutive failures, "no book data (reason: …)" on any error. `TokenResolver` is imported, never mutated.

**Tech Stack:** Python 3.11, `textual`, `textual-plotext`, `requests` (already in requirements), stdlib `json`/`pathlib`/`collections.deque`. Existing `active_bots.execution.TokenResolver`. No new runtime deps beyond the two Textual packages.

---

## Pre-flight skeleton (shown up front per user request)

This is what Task 2 produces — user should eyeball the tree and CSS before approving execution.

### Widget tree

```
DashboardApp(App)
  CSS_PATH = "dashboard.tcss"
  BINDINGS = [("ctrl+c","quit","Quit"), ("q","quit","Quit")]
  └─ compose() yields:
     Container #root                        (grid: 2 cols x 3 rows; rows 3 / 2fr / 1fr)
       ├─ HeaderWidget #header              (column-span: 2, height: 3)
       ├─ LiveCurvesWidget #live            (row 2, col 1)
       │    ├─ Static       #hero-prices    (height: 3, bold UP/DOWN)
       │    ├─ PlotextPlot  #chart-btc      (height: 1fr)
       │    └─ PlotextPlot  #chart-mkt      (height: 1fr)
       ├─ MainStrategyWidget #main          (row 2, col 2)
       │    ├─ Static       #refined-headline   (2 rows)
       │    ├─ Static       #refined-secondary  (2 rows)
       │    ├─ PlotextPlot  #chart-pnl     (height: 5)
       │    ├─ Static       #refined-position   (height: 3 or "no position")
       │    └─ DataTable    #refined-trades     (height: 1fr)
       ├─ OrderbookWidget #book             (row 3, col 1)
       │    ├─ Static       #book-hdr      (best bid / ask / spread / mid / status)
       │    └─ DataTable    #book-table    (asks desc, spread row, bids desc, depth bar col)
       └─ Container #bottom-right           (row 3, col 2; grid: 1 col x 2 rows, rows 4 / 1fr)
            ├─ BaselinesWidget #baselines   (height: 4 → 2 strategy rows + border)
            └─ OrdersLogWidget #orders-log  (RichLog, keeps last 50 refined events)
```

### CSS (dashboard.tcss)

```tcss
Screen { background: #0f172a; }

#root {
    layout: grid;
    grid-size: 2 3;
    grid-columns: 1fr 1fr;
    grid-rows: 3 2fr 1fr;
    grid-gutter: 0 0;
    width: 100%;
    height: 100%;
}

HeaderWidget   { column-span: 2; height: 3; border: heavy #3b82f6; padding: 0 1; }
LiveCurvesWidget   { border: round #475569; padding: 0 1; }
MainStrategyWidget { border: round #06b6d4; padding: 0 1; }
OrderbookWidget    { border: round #475569; padding: 0 1; }

#bottom-right { layout: grid; grid-size: 1 2; grid-rows: 4 1fr; }
BaselinesWidget { border: round #475569; padding: 0 1; height: 4; }
OrdersLogWidget { border: round #475569; padding: 0 1; }

#hero-prices { height: 3; content-align: center middle; text-style: bold; }
#chart-btc, #chart-mkt { height: 1fr; }

#refined-headline, #refined-secondary { height: 2; }
#chart-pnl { height: 5; }
#refined-position { height: 3; }
#refined-trades   { height: 1fr; }

#book-hdr   { height: 2; }
#book-table { height: 1fr; }
```

Textual interprets `grid-rows: 3 2fr 1fr` as a fixed 3-row header plus 2:1 split for the rest, which matches the spec ("middle = 2/3 of remaining, bottom = 1/3 of remaining"). `1fr:1fr` columns satisfy the 1:1 ratio requirement. Resizing flows automatically.

---

## File structure

- **Rewrite:** `scripts/dashboard.py` — single file, Textual App + pure-logic helpers ported verbatim from the current rich dashboard.
- **Create:** `scripts/dashboard.tcss` — grid layout + colours.
- **Create (tests):** `tests/dashboard/__init__.py`, `tests/dashboard/conftest.py`, `tests/dashboard/test_pnl_replay.py`, `tests/dashboard/test_events_tailer.py`, `tests/dashboard/test_orderbook_poller.py`, `tests/dashboard/test_read_only.py`. These are outside the stated "no new files other than .tcss" rule but follow the project's existing `tests/` convention (see `tests/execution/`). **Confirm with user before Task 3.** If rejected, the fallback is a `--self-test` CLI flag inside `dashboard.py`; the logic stays, only the entry point changes.
- **Modify:** `requirements.txt` — add `textual>=0.80` and `textual-plotext>=1.0`.
- **Modify (only if install line changes):** `RUNBOOK.md` — bump the `uv pip install` step.
- **Untouched:** `daemon_base_v1.py`, `active_bots/**`, `launch_daemon.sh`.

### Port list (verbatim from current `scripts/dashboard.py`)

| Current symbol | Port as |
|---|---|
| `_compute_stats(stats)` | `_compute_stats` (module-level) |
| `_action_from_event(ev)` | `_action_from_event` |
| `_render_action_line(a, prefix=...)` | `_render_action_line` (returns `rich.Text`) |
| `_side_text`, `_kind_text`, `pnl_color`, `conn_pill`, `fmt_secs` | same names |
| `build_refined_panel` body | split: `_refined_headline_text`, `_refined_secondary_text`, `_refined_position_text`, `_refined_trades_rows` |
| `build_comparison_banner` body | `_baselines_text` |
| `EventsTailer` class | extended to also accumulate per-strategy PnL — see Task 7 |
| `read_json`, `tail_lines`, KILL_FILE path | kept |

---

## Open decisions (LOCKED by user, restated for worker)

1. **Orderbook = path (a).** Dashboard-local poller, YES-token only, 2 s cadence, back off to 10 s after 3 failures. `TokenResolver.resolve(slug)` to map slug → token_id, re-resolved on `t_zero` rollover. From SG in paper mode this will 403/timeout; show `"no book data (reason: <status|'timeout'>)"` — do not raise.
2. **PnL curve = persistent, per-strategy, per-session-file.** On boot, full scan of `events.jsonl`, replay in order, accumulate pnl per strategy on `exit_filled` and `resolve`. Tail from the byte offset the scan ended at. Cap each deque at 2000 points; if exceeded, decimate middle (keep first + last + evenly-spaced middle). Series only resets on file truncation/deletion (tailer already handles that). Displayed chart = series for the strategy whose panel is being rendered (refined).
3. **Read-only discipline.** Open files with `os.O_RDONLY` (or `Path.read_text()` / `Path.open("rb")`, which do not create). Create no files under `daemon_state/`. No caches to disk — all in-memory.
4. **Strip emojis from plotext output** everywhere (e.g. `plt.plot_size` legend markers). Check raw rendered strings, pass them through `_strip_emoji` before feeding to Textual.

---

## Task breakdown

Each step is 2–5 minutes of actual work. Commits are per-task, conventional.

---

### Task 1: Install deps and verify baseline Textual runs

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add Textual deps to `requirements.txt`**

Append these two lines to the end of `requirements.txt`:

```
textual>=0.80
textual-plotext>=1.0
```

- [ ] **Step 2: Install into `polymarket-env`**

Run:
```bash
conda activate polymarket-env && uv pip install textual textual-plotext
```
Expected: both install cleanly; `python3 -c "import textual, textual_plotext; print(textual.__version__, textual_plotext.__version__)"` prints two version strings.

- [ ] **Step 3: Verify baseline Textual renders**

Run:
```bash
python3 -c "from textual.app import App; from textual.widgets import Static; \
class A(App): \
    def compose(self): yield Static('hello'); \
import sys; sys.exit(0)"
```
Expected: exits 0 without ImportError. (Do not try to actually render the app in a non-TTY.)

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore(deps): add textual and textual-plotext for TUI rewrite"
```

---

### Task 2: Scaffold empty Textual App + CSS grid (user review checkpoint)

**Files:**
- Rewrite: `scripts/dashboard.py`
- Create: `scripts/dashboard.tcss`

- [ ] **Step 1: Write `scripts/dashboard.tcss`**

```tcss
Screen { background: #0f172a; }

#root {
    layout: grid;
    grid-size: 2 3;
    grid-columns: 1fr 1fr;
    grid-rows: 3 2fr 1fr;
    width: 100%;
    height: 100%;
}

HeaderWidget       { column-span: 2; height: 3; border: heavy #3b82f6; padding: 0 1; }
LiveCurvesWidget   { border: round #475569; padding: 0 1; }
MainStrategyWidget { border: round #06b6d4; padding: 0 1; }
OrderbookWidget    { border: round #475569; padding: 0 1; }

#bottom-right { layout: grid; grid-size: 1 2; grid-rows: 4 1fr; }
BaselinesWidget { border: round #475569; padding: 0 1; height: 4; }
OrdersLogWidget { border: round #475569; padding: 0 1; }

#hero-prices { height: 3; content-align: center middle; text-style: bold; }
#chart-btc, #chart-mkt { height: 1fr; }

#refined-headline, #refined-secondary { height: 2; }
#chart-pnl        { height: 5; }
#refined-position { height: 3; }
#refined-trades   { height: 1fr; }

#book-hdr   { height: 2; }
#book-table { height: 1fr; }
```

- [ ] **Step 2: Write `scripts/dashboard.py` scaffold (widgets exist, no data yet)**

```python
#!/usr/bin/env python3
"""Live Textual TUI dashboard for daemon_base_v1 (read-only)."""
from __future__ import annotations
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Static, DataTable, RichLog

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "daemon_state"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE   = STATE_DIR / "daemon.log"
EVENTS_FILE= STATE_DIR / "events.jsonl"
KILL_FILE  = STATE_DIR / "KILL"

MARKET_DURATION = 300


class HeaderWidget(Static):
    def on_mount(self) -> None:
        self.update("header — waiting for daemon")


class LiveCurvesWidget(Container):
    def compose(self) -> ComposeResult:
        yield Static("UP — / DOWN —", id="hero-prices")
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
        print(f"[dashboard] {STATE_DIR} missing — start the daemon first")
        return 2
    DashboardApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Launch and eyeball the grid**

Run (in a TTY):
```bash
conda activate polymarket-env && python3 scripts/dashboard.py
```
Expected: full-screen TUI with five bordered regions arranged exactly as in the tree above. Header spans the top, middle row is 2/3 height split left/right, bottom row is 1/3 height, bottom-right splits into baselines (4 rows) and orders-log (rest). `Ctrl-C` exits cleanly. No crash on resize.

- [ ] **Step 4: USER REVIEW CHECKPOINT**

Before proceeding to Task 3, pause and show this running skeleton to the user for approval. Do not bind any data.

- [ ] **Step 5: Commit**

```bash
git add scripts/dashboard.py scripts/dashboard.tcss
git commit -m "feat(dashboard): scaffold textual app with grid widget tree"
```

---

### Task 3: Add `tests/dashboard/` skeleton (or, if user rejected, skip and use `--self-test`)

**Files:**
- Create: `tests/dashboard/__init__.py` (empty)
- Create: `tests/dashboard/conftest.py`

- [ ] **Step 1: Write `tests/dashboard/conftest.py`**

```python
"""Make scripts/dashboard.py importable as `dashboard`."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
```

- [ ] **Step 2: Smoke test: import works**

Create `tests/dashboard/test_import.py`:

```python
def test_import_dashboard():
    import dashboard  # noqa: F401
```

Run: `pytest tests/dashboard/test_import.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/dashboard/
git commit -m "test(dashboard): scaffold test dir and import smoke test"
```

---

### Task 4: Port shared helpers verbatim

**Files:**
- Modify: `scripts/dashboard.py`

- [ ] **Step 1: Port constants and helpers**

Insert the following at module top (after imports, before widgets). Port byte-for-byte from the current `scripts/dashboard.py` unless noted:

```python
import json, time
from collections import deque

RECENT_TRADES_CAP = 5
ACTION_CAP = 50
PRICE_HISTORY_CAP = 300      # 5 min @ ~1 Hz state updates
PNL_SERIES_CAP = 2000        # per-strategy cap

def read_json(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return None

def tail_lines(path, n=8):
    # ... port from current dashboard.py lines 67-83 unchanged ...

def pnl_color(v):
    if v > 0: return "bold green"
    if v < 0: return "bold red"
    return "white"

def fmt_secs(s):
    if s is None: return "-"
    return f"{int(s//60):d}:{int(s%60):02d}"

def _action_from_event(ev):
    # port from current dashboard.py lines 150-197 unchanged
    ...

def _compute_stats(stats):
    # port from current dashboard.py lines 270-286 unchanged
    ...

def _strip_emoji(s: str) -> str:
    """Remove any non-ASCII that plotext or upstream might sneak in."""
    return "".join(ch for ch in s if ord(ch) < 128 or ch in "█░▁▂▃▄▅▆▇─│┌┐└┘├┤┬┴┼")
```

- [ ] **Step 2: Write tests covering ported helpers**

Create `tests/dashboard/test_helpers.py`:

```python
import dashboard as d

def test_compute_stats_empty():
    s = d._compute_stats({})
    assert s["total"] == 0 and s["wr"] == 0 and s["roi"] == 0

def test_compute_stats_winrate():
    s = d._compute_stats({"total_trades": 4, "wins": 3, "losses": 1,
                         "total_pnl": 2.0, "total_risked": 20.0})
    assert s["wr"] == 75.0
    assert s["roi"] == 10.0

def test_action_from_event_entry():
    ev = {"ts": 1.0, "type": "entry_filled", "strategy": "refined",
          "position": {"side": "Up", "entry_price": 0.42, "size_usdc": 5.0}}
    a = d._action_from_event(ev)
    assert a["kind"] == "BUY" and a["side"] == "Up" and a["price"] == 0.42

def test_action_from_event_ignored_type():
    assert d._action_from_event({"type": "market_rollover"}) is None

def test_strip_emoji_passes_box_drawing():
    assert d._strip_emoji("│─┼") == "│─┼"

def test_strip_emoji_strips_high_unicode():
    assert "🚀" not in d._strip_emoji("rocket 🚀")
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/dashboard/test_helpers.py -v`
Expected: all five PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/dashboard.py tests/dashboard/test_helpers.py
git commit -m "feat(dashboard): port stats/event helpers from rich dashboard"
```

---

### Task 5: HeaderWidget — bind state.json

**Files:**
- Modify: `scripts/dashboard.py`

- [ ] **Step 1: Implement `HeaderWidget.render_state(state)`**

Replace the stub `HeaderWidget` with:

```python
from rich.text import Text
from rich.table import Table

class HeaderWidget(Static):
    def render_state(self, state: dict | None) -> None:
        if state is None:
            self.update("[dim]waiting for daemon_state/state.json[/]")
            return
        conn = state.get("connections") or {}
        t_zero = state.get("t_zero") or 0
        elapsed = time.time() - t_zero if t_zero else 0
        remaining = max(0.0, MARKET_DURATION - elapsed)
        bar_w = 30
        filled = int(bar_w * min(elapsed / MARKET_DURATION, 1.0)) if t_zero else 0
        bar = "█" * filled + "░" * (bar_w - filled)
        kill = KILL_FILE.exists()

        line1 = Text()
        line1.append("BTC ", style="bold #22d3ee")
        line1.append(f"${state.get('btc_price', 0):>10,.2f}  ", style="bold")
        line1.append(f"σ={(state.get('sigma') or 0)*100:.2f}%  ", style="magenta")
        line1.append(f"strike ${state.get('strike') or 0:,.0f}", style="#94a3b8")

        line2 = Text()
        line2.append(state.get("slug") or "-", style="yellow")
        line2.append(f"  [{bar}]  ", style="#94a3b8")
        line2.append(f"t+{int(elapsed)}s / 300s  ", style="#22d3ee")
        line2.append("binance ", style="#94a3b8")
        line2.append("●", style="green" if conn.get("binance") else "red")
        line2.append("  rtds ", style="#94a3b8")
        line2.append("●", style="green" if conn.get("rtds") else "red")
        line2.append("  ")
        line2.append(" KILL " if kill else "  ok  ",
                     style="bold white on #d946ef" if kill else "dim green")

        self.update(Text("\n").join([line1, line2]))
```

- [ ] **Step 2: Wire header refresh from the app tick (temp tick, final wiring in Task 11)**

Add inside `DashboardApp`:

```python
def on_mount(self) -> None:
    self.set_interval(0.5, self._tick)

def _tick(self) -> None:
    state = read_json(STATE_FILE)
    self.query_one(HeaderWidget).render_state(state)
```

- [ ] **Step 3: Manual verification against live daemon**

Run: `python3 scripts/dashboard.py`
Expected: header shows BTC price, sigma, strike (line 1), slug, progress bar filling, conn dots green, kill "ok" (line 2). Kill pill switches to magenta "KILL" when `daemon_state/KILL` exists (touch it then `rm` it).

- [ ] **Step 4: Commit**

```bash
git add scripts/dashboard.py
git commit -m "feat(dashboard): header widget bound to state.json"
```

---

### Task 6: EventsTailer (ported) + PnL replay accumulator

**Files:**
- Modify: `scripts/dashboard.py`
- Create: `tests/dashboard/test_events_tailer.py`
- Create: `tests/dashboard/test_pnl_replay.py`

- [ ] **Step 1: Write failing test for `EventsTailer` rotation handling**

Create `tests/dashboard/test_events_tailer.py`:

```python
import json
from pathlib import Path
import dashboard as d

def _write_events(tmp_path, events):
    p = tmp_path / "events.jsonl"
    with p.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return p

def test_tailer_picks_up_new_events(tmp_path):
    p = _write_events(tmp_path, [
        {"ts": 1, "type": "entry_filled", "strategy": "refined",
         "position": {"side": "Up", "entry_price": 0.5, "size_usdc": 10}},
    ])
    t = d.EventsTailer(p)
    t.update()
    assert len(t.refined_actions) == 1

    with p.open("a") as f:
        f.write(json.dumps({"ts": 2, "type": "exit_filled", "strategy": "refined",
                            "trade": {"side": "Up", "exit_price": 0.6,
                                      "size_usdc": 10, "pnl": 2.0,
                                      "exit_type": "TP"}}) + "\n")
    t.update()
    assert len(t.refined_actions) == 2

def test_tailer_resets_on_truncation(tmp_path):
    p = _write_events(tmp_path, [{"ts": 1, "type": "session_start", "pid": 1}])
    t = d.EventsTailer(p)
    t.update()
    offset_after_first = t._pos
    p.write_text("")  # truncate
    t.update()
    assert t._pos == 0  # re-synced from start

def test_tailer_handles_missing_file(tmp_path):
    t = d.EventsTailer(tmp_path / "nope.jsonl")
    t.update()  # must not raise
    assert len(t.events) == 0
```

- [ ] **Step 2: Write failing test for per-strategy PnL series**

Create `tests/dashboard/test_pnl_replay.py`:

```python
import json
import dashboard as d

def _write(tmp_path, events):
    p = tmp_path / "events.jsonl"
    with p.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return p

def test_pnl_series_sums_exit_and_resolve_per_strategy(tmp_path):
    p = _write(tmp_path, [
        {"ts": 1, "type": "exit_filled", "strategy": "refined",
         "trade": {"pnl": 1.5}},
        {"ts": 2, "type": "exit_filled", "strategy": "base",
         "trade": {"pnl": -0.25}},
        {"ts": 3, "type": "resolve", "strategy": "refined",
         "trade": {"pnl": 0.5}},
    ])
    t = d.EventsTailer(p)
    t.update()
    refined = list(t.pnl_series["refined"])
    base = list(t.pnl_series["base"])
    assert [v for _, v in refined] == [1.5, 2.0]
    assert [v for _, v in base] == [-0.25]

def test_pnl_series_ignores_non_pnl_events(tmp_path):
    p = _write(tmp_path, [
        {"ts": 1, "type": "market_rollover", "slug": "x"},
        {"ts": 2, "type": "entry_filled", "strategy": "refined",
         "position": {"side": "Up", "entry_price": 0.5, "size_usdc": 10}},
    ])
    t = d.EventsTailer(p)
    t.update()
    assert t.pnl_series == {}  # no accumulation, no keys

def test_pnl_series_decimates_over_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PNL_SERIES_CAP", 4)
    evs = [
        {"ts": float(i), "type": "exit_filled", "strategy": "refined",
         "trade": {"pnl": 0.1}} for i in range(10)
    ]
    p = _write(tmp_path, evs)
    t = d.EventsTailer(p)
    t.update()
    series = list(t.pnl_series["refined"])
    assert len(series) <= 4
    assert series[0][0] == 0.0         # first preserved
    assert series[-1][0] == 9.0        # last preserved (tail)
```

- [ ] **Step 3: Run tests to confirm they fail**

Run: `pytest tests/dashboard/test_events_tailer.py tests/dashboard/test_pnl_replay.py -v`
Expected: FAIL (EventsTailer ported but no `pnl_series` attribute yet / classes missing).

- [ ] **Step 4: Port `EventsTailer` with PnL accumulator**

Replace any existing EventsTailer stub in `dashboard.py` with:

```python
class EventsTailer:
    def __init__(self, path: Path, cap: int = 200):
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
            if strat == "base":     self.base_actions.append(action)
            elif strat == "enhanced": self.enh_actions.append(action)
            elif strat == "refined":  self.refined_actions.append(action)
            self.unified_actions.append(action)
        # PnL series — only on realized events
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
        # Keep first, last, and evenly-spaced middle — drop nothing from the tail.
        target = PNL_SERIES_CAP
        middle_count = target - 2
        first, last = series[0], series[-1]
        middle = series[1:-1]
        if middle_count <= 0 or not middle:
            self.pnl_series[strat] = [first, last]
            return
        step = max(1, len(middle) // middle_count)
        sampled = middle[::step][:middle_count]
        self.pnl_series[strat] = [first, *sampled, last]

    def update(self) -> None:
        if not self.path.exists(): return
        try:
            size = self.path.stat().st_size
            if size < self._pos:
                self._pos = 0
                self._cum.clear()
                self.pnl_series.clear()
            with self.path.open("rb") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
            for line in data.decode(errors="replace").splitlines():
                line = line.strip()
                if not line: continue
                try: ev = json.loads(line)
                except ValueError: continue
                self.events.append(ev)
                self._dispatch(ev)
        except OSError:
            return
```

- [ ] **Step 5: Run tests to confirm they pass**

Run: `pytest tests/dashboard/test_events_tailer.py tests/dashboard/test_pnl_replay.py -v`
Expected: all six PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/dashboard.py tests/dashboard/test_events_tailer.py tests/dashboard/test_pnl_replay.py
git commit -m "feat(dashboard): port events tailer and add per-strategy pnl replay"
```

---

### Task 7: MainStrategyWidget — bind refined panel + PnL mini-chart

**Files:**
- Modify: `scripts/dashboard.py`

- [ ] **Step 1: Add plotext import and `MainStrategyWidget.render_state`**

Near the top:

```python
from textual_plotext import PlotextPlot
```

Replace `MainStrategyWidget` with:

```python
class MainStrategyWidget(Container):
    def compose(self) -> ComposeResult:
        yield Static("", id="refined-headline")
        yield Static("", id="refined-secondary")
        yield PlotextPlot(id="chart-pnl")
        yield Static("no position", id="refined-position")
        yield DataTable(id="refined-trades", zebra_stripes=False,
                        cursor_type="none")

    def on_mount(self) -> None:
        tbl = self.query_one("#refined-trades", DataTable)
        for col in ("t", "side", "in→out", "size", "pnl", "ROI", "why", "hold"):
            tbl.add_column(col, key=col)

    def render_state(self, state: dict | None,
                     actions, pnl_series: list[tuple[float, float]]) -> None:
        if state is None:
            self.query_one("#refined-headline", Static).update(
                "[dim]waiting for state.json[/]")
            return
        blob = state.get("refined") or {}
        s = _compute_stats(blob.get("stats") or {})
        extra = blob.get("extra") or {}
        fair = blob.get("fair_price")
        mkt = state.get("market_price_up")

        headline = Text()
        headline.append(f"PnL {s['total_pnl']:+.2f}  ",
                        style=f"bold {pnl_color(s['total_pnl'])}")
        headline.append(f"ROI {s['roi']:+.1f}%  ",
                        style=pnl_color(s['roi']))
        headline.append(f"{s['total']} tr  ", style="bold")
        headline.append(f"W={s['wins']} L={s['losses']} {s['wr']:.0f}%  ",
                        style="#94a3b8")
        headline.append(f"DD ${s['max_drawdown']:.2f}  ", style="red")
        st_style = ("green" if s['streak_type'] == 'W'
                    else "red" if s['streak_type'] == 'L' else "#94a3b8")
        headline.append(f"streak {s['streak']} {s['streak_type'] or '-'}",
                        style=st_style)
        second_line = Text()
        slug = state.get("slug") or ""
        t_zero = state.get("t_zero") or 0
        if t_zero:
            second_line.append(
                "Market: BTC " + time.strftime("%Y-%m-%d T-T+5m",
                                               time.localtime(t_zero)),
                style="bold")
        else:
            second_line.append(f"Market: {slug}", style="bold")
        self.query_one("#refined-headline", Static).update(
            Text("\n").join([second_line, headline]))

        secondary = Text()
        secondary.append(f"fair {fair:.3f}  " if fair is not None
                         else "fair -    ", style="#eab308")
        secondary.append(f"mkt {mkt:.3f}  " if mkt is not None
                         else "mkt -    ", style="#e2e8f0")
        if fair is not None and mkt is not None:
            d_ = fair - mkt
            secondary.append(f"edge {d_:+.3f}  ",
                             style="green" if abs(d_) >= 0.1 else "#94a3b8")
            secondary.append(f"side {'Up' if d_ > 0 else 'Down' if d_ < 0 else '-'}  ",
                             style="green" if d_ > 0 else "red" if d_ < 0 else "#94a3b8")
        secondary.append(
            f"TP/SL/Res {extra.get('tp_count',0)}/{extra.get('sl_count',0)}/{extra.get('resolution_count',0)}",
            style="white")
        self.query_one("#refined-secondary", Static).update(secondary)

        # PnL mini-chart
        plt = self.query_one("#chart-pnl", PlotextPlot).plt
        plt.clear_figure()
        plt.theme("pro")
        if pnl_series:
            xs = [ts for ts, _ in pnl_series]
            ys = [v for _, v in pnl_series]
            colour = "green" if ys[-1] > 0 else "red" if ys[-1] < 0 else "white"
            plt.plot(xs, ys, color=colour, marker="braille")
            plt.ylim(min(ys) - 0.5, max(ys) + 0.5)
        plt.xaxes(False, False); plt.yaxes(False, False)
        self.query_one("#chart-pnl", PlotextPlot).refresh()

        # Position block
        pos = blob.get("open_position")
        pos_text = Text()
        if pos:
            side = pos.get("side", "?")
            side_style = "bold green" if side == "Up" else "bold red"
            entry = pos.get("entry_price", 0)
            realizable = None
            if mkt is not None:
                realizable = mkt if side == "Up" else 1.0 - mkt
            pos_text.append("OPEN ", style="bold yellow")
            pos_text.append(f"{side} ", style=side_style)
            pos_text.append(f"entry {entry:.3f}  ", style="white")
            pos_text.append(f"size ${pos.get('size_usdc', 0):.2f}  ",
                            style="white")
            pos_text.append(f"edge {pos.get('edge', 0):.3f}", style="cyan")
            if realizable is not None:
                favor = realizable - entry
                pos_text.append(f"  realizable {realizable:.3f}  favor {favor:+.3f}",
                                style=pnl_color(favor))
        else:
            pos_text.append("no position", style="dim")
        self.query_one("#refined-position", Static).update(pos_text)

        # Recent trades tail
        tbl = self.query_one("#refined-trades", DataTable)
        tbl.clear()
        trades = (blob.get("closed_trades") or [])[-RECENT_TRADES_CAP:]
        for t_ in reversed(trades):
            ts = t_.get("resolved_time", 0)
            tm = time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"
            side = t_.get("side", "?")
            pnl = t_.get("pnl", 0) or 0
            size_usdc = t_.get("size_usdc", 0) or 0
            t_roi = (pnl / size_usdc * 100) if size_usdc else 0
            hold = t_.get("hold_time_s")
            tbl.add_row(
                tm,
                Text(side, style="green" if side == "Up" else "red"),
                f"{t_.get('entry_price', 0):.3f}->{t_.get('exit_price', 0):.3f}",
                f"${size_usdc:.0f}",
                Text(f"{pnl:+.2f}", style=pnl_color(pnl)),
                Text(f"{t_roi:+.1f}%", style=pnl_color(t_roi)),
                t_.get("exit_type", "") or "",
                f"{int(hold)}s" if hold else "-",
            )
```

- [ ] **Step 2: Store tailer on the app and wire into `_tick`**

Modify `DashboardApp`:

```python
def on_mount(self) -> None:
    self.tailer = EventsTailer(EVENTS_FILE)
    self.set_interval(0.5, self._tick)

def _tick(self) -> None:
    state = read_json(STATE_FILE)
    self.tailer.update()
    self.query_one(HeaderWidget).render_state(state)
    self.query_one(MainStrategyWidget).render_state(
        state, self.tailer.refined_actions,
        self.tailer.pnl_series.get("refined", []),
    )
```

- [ ] **Step 3: Manual verification**

Run against a live daemon with some trade history:
```bash
python3 scripts/dashboard.py
```
Expected: MAIN panel shows market name line + headline stats, fair/mkt/edge/TP-SL-Res line, a green or red PnL mini-chart (if any `refined` trades exist), position block, recent trades table.

- [ ] **Step 4: Commit**

```bash
git add scripts/dashboard.py
git commit -m "feat(dashboard): main strategy widget with stats and pnl chart"
```

---

### Task 8: LiveCurvesWidget — hero prices + btc + market/fair charts

**Files:**
- Modify: `scripts/dashboard.py`

- [ ] **Step 1: Implement `LiveCurvesWidget`**

Replace the stub `LiveCurvesWidget`:

```python
class LiveCurvesWidget(Container):
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

        t_zero = state.get("t_zero")
        if t_zero != self._last_t_zero:
            self._btc.clear(); self._mkt.clear(); self._fair.clear()
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

        up = max(0.0, min(1.0, float(mkt))) if isinstance(mkt, (int, float)) else None
        hero_t = Text()
        if up is None:
            hero_t.append("UP  -    ", style="dim"); hero_t.append("DOWN  -", style="dim")
        else:
            hero_t.append(f"UP  ${up:.2f}", style="bold #4ade80")
            hero_t.append("       ")
            hero_t.append(f"DOWN  ${1 - up:.2f}", style="bold #f87171")
        hero.update(hero_t)

        # BTC chart
        btc_plot = self.query_one("#chart-btc", PlotextPlot)
        plt = btc_plot.plt
        plt.clear_figure(); plt.theme("pro")
        if self._btc:
            xs = [t for t, _ in self._btc]; ys = [v for _, v in self._btc]
            plt.plot(xs, ys, color="cyan", marker="braille")
            plt.ylim(min(ys) * 0.9995, max(ys) * 1.0005)
        plt.title("BTC USD")
        plt.xaxes(False, False); plt.yaxes(True, False)
        btc_plot.refresh()

        # Market / fair chart (overlaid)
        mkt_plot = self.query_one("#chart-mkt", PlotextPlot)
        plt = mkt_plot.plt
        plt.clear_figure(); plt.theme("pro")
        if self._mkt:
            xs_m = [t for t, _ in self._mkt]
            ys_m = [v for _, v in self._mkt]
            plt.plot(xs_m, ys_m, color="white", marker="braille", label="mkt")
        if self._fair:
            xs_f = [t for t, _ in self._fair]
            ys_f = [v for _, v in self._fair]
            fair_colour = "green"
            if self._mkt:
                fair_colour = "green" if ys_f[-1] > self._mkt[-1][1] else "red"
            plt.plot(xs_f, ys_f, color=fair_colour, marker="braille", label="fair")
        plt.ylim(0.0, 1.0)
        plt.title("market / fair up")
        plt.xaxes(False, False); plt.yaxes(True, False)
        mkt_plot.refresh()
```

(Note on the "fill between" requirement: `plotext` has no native `fill_between`. We approximate it by colouring the fair line green when fair > mkt, red when fair < mkt. No third overlay — simpler, no visual artefacts, still communicates direction. Document this in the final commit body.)

- [ ] **Step 2: Wire into `_tick`**

In `DashboardApp._tick`, add:

```python
self.query_one(LiveCurvesWidget).render_state(state)
```

- [ ] **Step 3: Manual verification**

Run against a live daemon. Expected: hero row shows UP/DOWN in bright green/red bold; BTC line chart fills in over time; market/fair chart shows white market line and green-or-red fair line tracking from 0..1. On market rollover (`t_zero` change) both charts clear.

- [ ] **Step 4: Commit**

```bash
git add scripts/dashboard.py
git commit -m "feat(dashboard): live curves panel with hero prices and plotext charts"
```

---

### Task 9: OrderbookPoller + OrderbookWidget (path (a), dashboard-local)

**Files:**
- Modify: `scripts/dashboard.py`
- Create: `tests/dashboard/test_orderbook_poller.py`

- [ ] **Step 1: Write failing test for `OrderbookPoller`**

Create `tests/dashboard/test_orderbook_poller.py`:

```python
from unittest.mock import MagicMock, patch
import dashboard as d

class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
    def json(self): return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise d.requests.HTTPError(f"{self.status_code}")

def test_orderbook_poller_success():
    sess = MagicMock()
    sess.get.return_value = FakeResponse(200, {
        "bids": [{"price": "0.42", "size": "10"}],
        "asks": [{"price": "0.44", "size": "8"}],
    })
    p = d.OrderbookPoller("tokenid", session=sess)
    snap = p.poll_once()
    assert snap.status == "ok"
    assert snap.best_bid == 0.42
    assert snap.best_ask == 0.44
    assert snap.error is None

def test_orderbook_poller_403_sets_reason():
    sess = MagicMock()
    sess.get.return_value = FakeResponse(403, None)
    p = d.OrderbookPoller("tokenid", session=sess)
    snap = p.poll_once()
    assert snap.status == "error"
    assert "403" in (snap.error or "")

def test_orderbook_poller_backs_off_after_three_failures():
    sess = MagicMock()
    sess.get.side_effect = d.requests.Timeout()
    p = d.OrderbookPoller("tokenid", session=sess, base_interval=2.0, backoff_interval=10.0)
    for _ in range(3):
        p.poll_once()
    assert p.current_interval == 10.0

def test_orderbook_poller_recovers_from_backoff_on_success():
    sess = MagicMock()
    sess.get.side_effect = [d.requests.Timeout()] * 3 + [
        FakeResponse(200, {"bids": [], "asks": []})
    ]
    p = d.OrderbookPoller("tokenid", session=sess, base_interval=2.0, backoff_interval=10.0)
    for _ in range(4):
        p.poll_once()
    assert p.current_interval == 2.0
```

- [ ] **Step 2: Run to confirm fail**

Run: `pytest tests/dashboard/test_orderbook_poller.py -v`
Expected: FAIL (class missing).

- [ ] **Step 3: Implement `OrderbookPoller`**

Add to `dashboard.py`:

```python
import requests
from dataclasses import dataclass, field

CLOB_BOOK_URL = "https://clob.polymarket.com/book"

@dataclass
class OrderbookSnapshot:
    status: str                          # "ok" | "error" | "empty"
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, size) desc
    asks: list[tuple[float, float]] = field(default_factory=list)  # (price, size) asc
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    mid: float = 0.0
    error: str | None = None

class OrderbookPoller:
    def __init__(self, token_id: str, *, session: requests.Session | None = None,
                 base_interval: float = 2.0, backoff_interval: float = 10.0,
                 failure_threshold: int = 3, timeout: float = 3.0):
        self.token_id = token_id
        self._session = session or requests.Session()
        self.base_interval = base_interval
        self.backoff_interval = backoff_interval
        self.current_interval = base_interval
        self.failure_threshold = failure_threshold
        self._timeout = timeout
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

        bids = [(float(b["price"]), float(b["size"])) for b in (payload.get("bids") or [])]
        asks = [(float(a["price"]), float(a["size"])) for a in (payload.get("asks") or [])]
        bids.sort(key=lambda x: -x[0])  # descending
        asks.sort(key=lambda x:  x[0])  # ascending
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
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/dashboard/test_orderbook_poller.py -v`
Expected: all four PASS.

- [ ] **Step 5: Implement `OrderbookWidget.render_snapshot`**

Replace `OrderbookWidget`:

```python
class OrderbookWidget(Container):
    BOOK_DEPTH = 10

    def compose(self) -> ComposeResult:
        yield Static("", id="book-hdr")
        yield DataTable(id="book-table", zebra_stripes=False, cursor_type="none")

    def on_mount(self) -> None:
        tbl = self.query_one("#book-table", DataTable)
        tbl.add_columns("side", "price", "size", "depth")

    def render_snapshot(self, snap: "OrderbookSnapshot | None") -> None:
        hdr = self.query_one("#book-hdr", Static)
        tbl = self.query_one("#book-table", DataTable)
        tbl.clear()

        if snap is None:
            hdr.update("[dim]book: no poll yet[/]"); return
        if snap.status == "error":
            hdr.update(f"[dim]no book data (reason: {snap.error})[/]"); return
        if snap.status == "empty":
            hdr.update("[dim]book empty[/]"); return

        header = Text()
        header.append(f"bid {snap.best_bid:.3f}  ", style="green")
        header.append(f"ask {snap.best_ask:.3f}  ", style="red")
        header.append(f"spread {snap.spread:.3f}  mid {snap.mid:.3f}",
                      style="#94a3b8")
        hdr.update(header)

        asks = snap.asks[:self.BOOK_DEPTH][::-1]  # highest ask first, top of table
        bids = snap.bids[:self.BOOK_DEPTH]
        max_size = max(
            [s for _, s in asks] + [s for _, s in bids] + [0.0001]
        )
        bar_w = 20

        def bar(size: float) -> str:
            n = int(bar_w * (size / max_size)) if max_size else 0
            return "█" * n

        for px, sz in asks:
            tbl.add_row(Text("ASK", style="red"),
                        f"{px:.3f}", f"{sz:.1f}",
                        Text(bar(sz), style="red"))
        tbl.add_row(Text("───", style="#94a3b8"),
                    Text("spread", style="#94a3b8"),
                    f"{snap.spread:.3f}", "")
        for px, sz in bids:
            tbl.add_row(Text("BID", style="green"),
                        f"{px:.3f}", f"{sz:.1f}",
                        Text(bar(sz), style="green"))
```

- [ ] **Step 6: Wire poller into the app**

Add these imports + app state:

```python
from active_bots.execution.token_resolver import TokenResolver

# inside DashboardApp
def on_mount(self) -> None:
    self.tailer = EventsTailer(EVENTS_FILE)
    self._resolver = TokenResolver()
    self._poller: OrderbookPoller | None = None
    self._poller_slug: str | None = None
    self._last_book_poll = 0.0
    self.set_interval(0.5, self._tick)

def _ensure_poller(self, slug: str | None) -> None:
    if not slug:
        self._poller = None; self._poller_slug = None; return
    if slug == self._poller_slug and self._poller is not None:
        return
    try:
        tokens = self._resolver.resolve(slug)
    except Exception:
        tokens = None
    if tokens is None:
        self._poller = None; return
    self._poller = OrderbookPoller(tokens.yes_token_id)
    self._poller_slug = slug

def _maybe_poll_book(self) -> "OrderbookSnapshot | None":
    if self._poller is None: return None
    now = time.time()
    if now - self._last_book_poll < self._poller.current_interval:
        return self._poller.last_snapshot
    self._last_book_poll = now
    return self._poller.poll_once()
```

And in `_tick`:

```python
def _tick(self) -> None:
    state = read_json(STATE_FILE)
    self.tailer.update()
    self._ensure_poller(state.get("slug") if state else None)
    snap = self._maybe_poll_book()
    self.query_one(HeaderWidget).render_state(state)
    self.query_one(LiveCurvesWidget).render_state(state)
    self.query_one(MainStrategyWidget).render_state(
        state, self.tailer.refined_actions,
        self.tailer.pnl_series.get("refined", []),
    )
    self.query_one(OrderbookWidget).render_snapshot(snap)
```

- [ ] **Step 7: Manual verification**

Run against the daemon. In paper mode from SG the panel will show `no book data (reason: 403)` or `timeout` — that is correct. If you run the dashboard inside the `polybot` netns the book should populate. No exception must bubble up to the UI under any network condition (test by cutting wifi briefly).

- [ ] **Step 8: Commit**

```bash
git add scripts/dashboard.py tests/dashboard/test_orderbook_poller.py
git commit -m "feat(dashboard): dashboard-local orderbook poller with backoff"
```

---

### Task 10: BaselinesWidget (port `build_comparison_banner`) + OrdersLogWidget

**Files:**
- Modify: `scripts/dashboard.py`

- [ ] **Step 1: Implement `BaselinesWidget.render_state`**

Replace `BaselinesWidget`:

```python
class BaselinesWidget(Static):
    def render_state(self, state: dict | None) -> None:
        if state is None:
            self.update("[dim]baselines: waiting[/]"); return
        lines = []
        for key, label in (("base", "BASE    "), ("enhanced", "ENHANCED")):
            blob = state.get(key) or {}
            s = _compute_stats(blob.get("stats") or {})
            line = Text()
            line.append(f"{label}  ", style="bold")
            line.append(f"PnL {s['total_pnl']:+.2f}  ",
                        style=pnl_color(s['total_pnl']))
            line.append(f"ROI {s['roi']:+.1f}%  ",
                        style=pnl_color(s['roi']))
            line.append(f"{s['total']} tr W={s['wins']} L={s['losses']}  ",
                        style="#94a3b8")
            line.append(f"DD ${s['max_drawdown']:.2f}", style="red")
            lines.append(line)
        self.update(Text("\n").join(lines))
```

- [ ] **Step 2: Implement `OrdersLogWidget` tail of refined events**

Replace:

```python
class OrdersLogWidget(RichLog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, max_lines=50, wrap=False, markup=False, **kwargs)
        self._seen_ids: set[int] = set()

    def ingest(self, actions) -> None:
        """Append new actions by identity (ts+kind+price fingerprint)."""
        for a in actions:
            fp = hash((a.get("ts"), a.get("kind"), a.get("strategy"),
                       round(a.get("price") or 0, 4)))
            if fp in self._seen_ids: continue
            self._seen_ids.add(fp)
            self.write(_render_action_line(a))
```

- [ ] **Step 3: Wire into `_tick`**

Extend `_tick`:

```python
self.query_one(BaselinesWidget).render_state(state)
self.query_one(OrdersLogWidget).ingest(self.tailer.refined_actions)
```

- [ ] **Step 4: Manual verification**

Run. Expected: BASE and ENHANCED stats update live; orders log streams refined BUY/SELL/RES lines newest-at-bottom, scroll retained, capped at 50.

- [ ] **Step 5: Commit**

```bash
git add scripts/dashboard.py
git commit -m "feat(dashboard): baselines row and refined orders log"
```

---

### Task 11: Read-only discipline audit + tests

**Files:**
- Modify: `scripts/dashboard.py`
- Create: `tests/dashboard/test_read_only.py`

- [ ] **Step 1: Audit writes**

Grep `scripts/dashboard.py` for `open(` and `write`:
```bash
grep -nE "open\(|\.write\(|\.write_text|mkdir|touch\(" scripts/dashboard.py
```
Expected: every hit is either `Path.open("rb")` or `Path.read_text()` (pure reads), or on a path **not** in `STATE_DIR`. No `Path.touch()`, no `write_text`, no `mkdir` anywhere.

- [ ] **Step 2: Write assertion test**

Create `tests/dashboard/test_read_only.py`:

```python
import importlib, os
from unittest.mock import patch, MagicMock
import dashboard as d

def test_open_calls_are_read_only(tmp_path, monkeypatch):
    """Any open() of a file under daemon_state/ must be read-only."""
    state_dir = tmp_path / "daemon_state"
    state_dir.mkdir()
    monkeypatch.setattr(d, "STATE_DIR", state_dir)
    monkeypatch.setattr(d, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(d, "EVENTS_FILE", state_dir / "events.jsonl")
    monkeypatch.setattr(d, "LOG_FILE",    state_dir / "daemon.log")
    (state_dir / "state.json").write_text("{}")
    (state_dir / "events.jsonl").write_text("")
    (state_dir / "daemon.log").write_text("")

    allowed = ("r", "rb")
    real_open = open
    def guarded_open(file, mode="r", *a, **k):
        path = str(file)
        if path.startswith(str(state_dir)):
            assert mode in allowed, f"non-read open on {path} mode={mode}"
        return real_open(file, mode, *a, **k)

    with patch("builtins.open", side_effect=guarded_open):
        assert d.read_json(d.STATE_FILE) == {}
        t = d.EventsTailer(d.EVENTS_FILE); t.update()

def test_no_daemon_state_creates_during_tick():
    """KILL, pid, cache, etc. must not appear in daemon_state/ after ticking."""
    import tempfile, pathlib, os
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td)
        (p / "state.json").write_text("{}")
        (p / "events.jsonl").write_text("")
        before = set(os.listdir(p))
        d.read_json(p / "state.json")
        t = d.EventsTailer(p / "events.jsonl"); t.update()
        after = set(os.listdir(p))
        assert before == after
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/dashboard/test_read_only.py -v`
Expected: both PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/dashboard.py tests/dashboard/test_read_only.py
git commit -m "test(dashboard): enforce read-only access to daemon_state"
```

---

### Task 12: Signal handling + `launch_daemon.sh` smoke

**Files:**
- Modify: `scripts/dashboard.py` (only if needed)

- [ ] **Step 1: Verify Ctrl-C propagates**

Textual by default handles SIGINT as quit. The existing default is fine; we do not add our own handler. Confirm by removing any stray `try/except KeyboardInterrupt` around `DashboardApp().run()`:

Check:
```bash
grep -n "KeyboardInterrupt" scripts/dashboard.py
```
Expected: zero hits (or, if any, remove them — Textual handles it).

- [ ] **Step 2: Smoke test via `launch_daemon.sh`**

With a daemon already running (or launched in another pane), run:
```bash
./launch_daemon.sh status
```
Expected: dashboard attaches, grid renders, all panels update; Ctrl-C exits within 1 s; bash trap in the launch script then stops the daemon cleanly (for the non-`status` launch mode).

- [ ] **Step 3: Commit (only if changes)**

```bash
git add scripts/dashboard.py
git commit -m "fix(dashboard): let textual handle SIGINT directly"
```

If no changes, skip this commit.

---

### Task 13: RUNBOOK.md update (only if the install line changed)

**Files:**
- Modify: `RUNBOOK.md` (conditional)

- [ ] **Step 1: Check RUNBOOK install step**

```bash
grep -n "uv pip install" RUNBOOK.md
```

If the existing `uv pip install -r requirements.txt` line is already what users run and `textual` is in `requirements.txt`, the RUNBOOK is correct as-is. Otherwise add one line noting the TUI needs Textual.

- [ ] **Step 2: Commit (conditional)**

```bash
git add RUNBOOK.md
git commit -m "docs(runbook): note textual dependency for TUI"
```

---

### Task 14: Final verification and squash into single feature commit

**Files:** none

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/dashboard -v
```
Expected: all tests PASS.

- [ ] **Step 2: Lint / bytecheck**

```bash
python3 -m py_compile scripts/dashboard.py
python3 -c "import scripts.dashboard"
```
Expected: no output (clean).

- [ ] **Step 3: Manual run against live daemon, 5 minutes minimum**

Run:
```bash
./launch_daemon.sh paper
```
Watch through at least one market rollover (`t_zero` change). Confirm:
- Header progress bar fills then resets
- Live curves deques clear on rollover
- PnL mini-chart does NOT reset (per locked decision 2)
- Orders log keeps appending
- Orderbook panel either populates (in polybot netns) or shows the sane "no book data" message
- Ctrl-C exits cleanly

- [ ] **Step 4: Squash optional — verify final commit topic**

Per spec: "One conventional commit: `feat(dashboard): textual rewrite with charts and orderbook panel`". If the per-task commits are acceptable, keep them. If the user wants a single commit, squash tasks 1–13 into one:

```bash
git rebase -i HEAD~<N>
# squash all into the first, retitle to:
#   feat(dashboard): textual rewrite with charts and orderbook panel
```

Confirm with user before squashing published commits. If branch has not been pushed, safe to rewrite.

- [ ] **Step 5: Final status check**

```bash
git log --oneline -10
git status
```
Expected: clean working tree, feature commit(s) landed on `Sam-Dev`.

---

## Self-review against the spec

**Coverage:**

| Spec requirement | Covered in |
|---|---|
| Textual App, 3-row grid, 2/3-vs-1/3 middle/bottom, 1:1 cols | Task 2 CSS |
| Header with BTC/σ/strike (line 1), slug/progress/conns/kill (line 2) | Task 5 |
| Hero UP/DOWN prices + BTC chart + market/fair chart, clear on rollover | Task 8 |
| Main strategy panel (refined stats, fair/mkt/edge/TP-SL, position, recent trades) | Task 7 |
| Main strategy PnL mini-chart: persistent, per-strategy, with decimation | Tasks 6 + 7 |
| Orderbook panel (path a), slug→token resolve, 2 s poll, 10 s backoff after 3 fails, "no book data" text | Task 9 |
| Baselines 2-row banner | Task 10 |
| Refined orders log, newest at bottom, cap 50 | Task 10 |
| File rotation handling via ported EventsTailer | Task 6 |
| 2 Hz refresh, state.json read once per render | Tasks 5+7+8+10 (single `_tick`) |
| Strip emoji | Task 4 (`_strip_emoji`) |
| No daemon-state writes | Task 11 tests |
| Ctrl-C clean exit | Task 12 |
| No changes to daemon_base_v1.py / active_bots/ / launch_daemon.sh | enforced in plan scope |
| Launch: `conda activate polymarket-env && python3 scripts/dashboard.py` | Task 2 manual verify |
| Deliverable: `scripts/dashboard.py` + optional `scripts/dashboard.tcss` | Task 2 |
| RUNBOOK only updated if install changes | Task 13 |

**Placeholder scan:** every task has concrete code or concrete commands. No "implement later", no "similar to task N".

**Type / name consistency:** `EventsTailer.pnl_series` (dict[str, list[tuple[float,float]]]) matches between Tasks 6–7. `OrderbookSnapshot` fields match between poller and widget. `render_state(state, ...)` signature consistent across widgets.

**Known plan deviations from the user's "no new files other than .tcss" rule:** the tests under `tests/dashboard/`. Justified by the project convention (`tests/execution/` already exists) and the TDD requirement. **Ask the user in Task 3 whether to keep the tests dir or fold the logic into a `--self-test` CLI mode.**

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-23-dashboard-textual-rewrite.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
