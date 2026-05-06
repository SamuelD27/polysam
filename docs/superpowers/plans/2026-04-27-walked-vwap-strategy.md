# Walked-VWAP Strategy + Daemon Book Subscription + Dashboard Restructure

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Two-stage review per task** — see "Review protocol" below.

**Goal:** Stop entering paper/dryrun trades against unknowable order books. Add a parallel strategy variant that gates entries on book state, computes the walked-VWAP fill against the live CLOB book, applies the Polymarket bell-curve fee, and rejects entries with negative post-fee edge. Surface the new variant in both dashboards and emit reject events so the gate is tunable from observation.

**Architecture:**

- New strategy class `WalkedVWAPStrategy(RefinedStrategy)` in `active_bots/walked_vwap_strategy.py`. Reuses Refined's TP/SL knobs; adds a book-aware `on_tick(..., *, books=...)` override that runs the gate AFTER Refined would have signalled ENTER, but BEFORE returning it.
- New per-token book state machine in `active_bots/execution/live_book_state.py` (float-based, mirrors `experiments/backtest/harness.py:_RollingBookState` but lives in the runtime layer — see Open decisions §2). Daemon owns instances per (slug, side).
- Daemon adds a CLOB book WebSocket coroutine (`clob_book_feed`) — parallel to `binance_feed` and `rtds_feed` — that subscribes to YES + NO tokens of the current+next markets and feeds the per-token state machines. Subscription pattern mirrors `scripts/scrape_book.py`. Independent failure domain; does not depend on `scrape_book.py` running.
- `daemon_base_v1.py` adds a 4th strategy block (`walked_vwap`) parallel to base/enhanced/refined, threading the per-market book pair into `WalkedVWAPStrategy.on_tick`. Existing strategies' signatures are unchanged.
- Reject events (`entry_rejected` with `strategy="walked_vwap"` + `reason=...`) flow through the existing `EventLogger` so the gate is tunable from `events.jsonl`. Action dict carries the gate's diagnostic fields (`walked_VWAP`, `walked_edge`, etc.) which ride on `entry_signal`/`entry_rejected` events; `fill_details` keeps its existing shape.
- Dashboards: TUI `tui/python/dashboard.py` and web `scripts/live_dashboard.py` both reorder so `walked_vwap` is the primary panel (neutral label `walked_vwap (candidate)`) and `refined` joins `enhanced` + `base` in the baselines comparison row. Both dashboards add reject-count + reject-reason breakdown for `walked_vwap` specifically.

**Tech Stack:** Python 3.11, `websockets` (already in daemon), `requests` (already), `decimal.Decimal` only inside `fees.py` boundaries (gate uses floats; see Open decisions §2). No new runtime dependencies. Tests use `pytest` (existing convention).

---

## Review protocol (two-stage per task)

For each task:

1. **Stage 1 — implement.** Dispatch fresh subagent with the task's full context (this plan + the user's original prompt context). Subagent writes code, runs tests, commits.
2. **Stage 2 — review.** Main thread reads the diff, runs the acceptance commands, checks for: (a) constraint violations (untouched files actually untouched), (b) test coverage of the listed cases, (c) deviations from the plan, (d) commit message style. If clean → proceed. If issues → either fix-in-place via Edit (small) or dispatch a fix subagent (large) with the specific issues itemised, then re-review.

Do not proceed past a task with unresolved review findings. Surface to user when in doubt.

---

## Open decisions (defaults chosen — flag if you want to override)

1. **Both YES and NO books, not a single book.** The user's spec said `book: OrderBook | None`. For BTC binary options, "Up" buys YES (walks YES asks) and "Down" buys NO (walks NO asks). Passing a single book forces the strategy to do bid/ask conversion via 1−p arbitrage, which has rounding cost. **Default:** strategy receives a `MarketBooks(yes, no)` wrapper. Daemon subscribes to both per current+next slug. `MarketBooks` lives in `active_bots/execution/live_book_state.py` next to the state machine.
2. **Float-based book state machine, not Decimal `_RollingBookState`.** `experiments/backtest/harness.py:_RollingBookState` is Decimal — designed for canonical reconciliation. The walked-VWAP gate is a decision-time approximation, not a fill simulator. Importing experiments code into the runtime layer reverses the intended directionality (runtime is upstream of experiments). **Default:** new float-based class `LiveBookState` in `active_bots/execution/live_book_state.py`, ~80 LOC, with a comment pointing at `_RollingBookState` for the Decimal canonical version. Mark with a `# FUTURE-REFACTOR:` comment so a later pass can extract a shared protocol.
3. **fill_details unchanged.** User constraint forbids modifying executors. The walked-VWAP gate produces extra diagnostic fields (`walked_VWAP`, `walked_edge`, `book_top_size_take`, `spread_at_entry`, `book_staleness_at_entry_ms`) which live on the action dict and ride into `entry_signal`/`entry_filled` events as separate kwargs. PaperExecutor still synthesises `fill_details` the way it does today.
4. **Reject events as `entry_rejected`, not a new event type.** The daemon already emits `entry_rejected` when an executor returns None. Walked-VWAP reuses this event with `strategy="walked_vwap"` + `reason="..."` + the gate's diagnostic fields. New reason values are additive; downstream consumers (reconcile, dashboards) can branch on them. No schema migration.
5. **Gate runs AFTER Refined's edge check.** Sub-classing `RefinedStrategy` means the parent decides whether to enter; the gate only filters. If Refined returns no action, walked_vwap returns no action — no wasted gate computation.
6. **Down-size on partial fill is OFF by default.** `WALKED_VWAP_PARTIAL_OK=False`. Reject the whole entry if the book can't deliver the requested size. Conservative default; flip to `True` once we have observed reject patterns.
7. **Dashboard label is `walked_vwap (candidate)`** until we have data. Neither "primary" nor "champion" in user-facing strings.
8. **Entries rejected BEFORE risk manager.** The gate is part of the strategy; risk manager runs inside `LiveExecutor.enter` (paper/dryrun bypass it). For walked_vwap, gate-rejected actions never reach the executor — they're logged as `entry_rejected` directly from the strategy_loop.

If any of these are wrong, stop and flag before Task 1.

---

## File structure

### Created files

| Path | Responsibility |
|---|---|
| `active_bots/walked_vwap_strategy.py` | `WalkedVWAPStrategy(RefinedStrategy)` + gate constants. ~250 LOC. |
| `active_bots/execution/live_book_state.py` | `LiveBookState` float-based per-token state machine + `MarketBooks` wrapper + `walk_for_vwap` helper. ~150 LOC. |
| `tests/strategy/__init__.py` | Empty. |
| `tests/strategy/test_walked_vwap.py` | Unit tests for VWAP, fees, all 5 gate reasons, partial-fill flag. ~350 LOC. |
| `tests/execution/test_live_book_state.py` | Snapshot/delta/sequence-gap/tick-change tests for `LiveBookState`. ~200 LOC. |
| `tests/daemon/__init__.py` | Empty. |
| `tests/daemon/test_book_subscription.py` | Fake-WS unit tests for the daemon's `clob_book_feed` coroutine — message dispatch into per-token states. ~180 LOC. |

### Modified files

| Path | What changes |
|---|---|
| `daemon_base_v1.py` | New `clob_book_feed` coroutine; `DaemonState` gets `walked_*` fields + per-slug `MarketBooks` registry; `strategy_loop` gets a 4th block dispatching to `WalkedVWAPStrategy` with `books=...`; reject events plumbed; tests imports updated. |
| `tui/python/dashboard.py` | `MainStrategyWidget` retargets to `walked_vwap` (was `refined`). `BaselinesWidget` adds `refined` row alongside `base`/`enhanced`. New "Reject summary" line in `MainStrategyWidget`. `EventsTailer` accumulates `walked_vwap_actions` + per-strategy reject counters. PnL chart key flips to `walked_vwap`. |
| `scripts/live_dashboard.py` | New `<div id="strat-walked-vwap">` panel as the primary; new `<div id="strat-refined">` slot in baselines section; `renderStrategyPanel('strat-walked-vwap', 'walked_vwap (candidate)', ...)`; reject summary panel; events stream colour-codes walked_vwap differently. Backend tailer aggregates `walked_vwap` reject counts. |
| `tui/tests/conftest.py`, `tui/tests/test_*` | Update tests that hard-code "refined" panel routing — at minimum `test_main_strategy.py`, `test_pnl_replay.py`, `test_baselines.py`, `test_events_tailer.py`. |

### Untouched (constraint)

- `active_bots/base_strategy.py`, `active_bots/enhanced_strategy.py`, `active_bots/refined_strategy.py`
- `active_bots/execution/paper_executor.py`, `live_executor.py`, `dry_run_executor.py`, `executor.py`, `book.py` (the Decimal Book primitives stay replay-only), `fees.py`, `risk_manager.py`, `reconciler.py`, `token_resolver.py`
- `scripts/scrape_book.py`
- `experiments/backtest/*`
- `launch_daemon.sh`

---

## Risk register

| # | Risk | Mitigation | Stop-and-flag trigger |
|---|---|---|---|
| 1 | Adding `book` kwarg to base/enhanced/refined breaks them. | Don't. Daemon dispatches `walked.on_tick(..., books=mb)` and `refined.on_tick(...)` separately. Existing on_tick signatures are unchanged. | Any diff to `base_strategy.py`, `enhanced_strategy.py`, `refined_strategy.py` — STOP. |
| 2 | CLOB book WS subscription fails (geo-restriction, auth, rate-limit). | `scripts/scrape_book.py` confirms public unauth subscription works from outside the netns. Same subscription pattern (`{assets_ids, type:"market", custom_feature_enabled:true}`). | If first paper smoke shows zero `book` events arriving in 60 s — STOP, triage: subscribe-from-daemon vs pipe-from-scrape_book.py via UNIX socket. |
| 3 | Decimal vs float arithmetic divergence between gate (float) and reconcile (Decimal). | Gate is decision-time approximation, not a fill simulator. Document in walked_vwap docstring: "for canonical fill arithmetic see `experiments/backtest/replay_executor.py`". Test the gate against float hand-computed VWAPs only. | Magnitude > $0.005 USDC per trade in golden-trace cross-check (deferred; not in this plan). |
| 4 | Existing dashboard tests break when "refined" routing flips to walked_vwap. | Update tests in same commit as dashboard changes. Don't skip tests. | Any `pytest tui/tests` regression unrelated to the new feature → STOP. |
| 5 | events.jsonl schema bloat. | New fields are additive on `entry_signal` / `entry_rejected` only, scoped to `strategy="walked_vwap"` rows. Existing event consumers (reconcile, dashboards) ignore unknown kwargs. | Reconcile.py raises on unknown columns → STOP, gate the diagnostic fields behind a per-strategy schema check. |
| 6 | Daemon book WS subscription brings in another asyncio failure mode (silent stalls, reconnect storms). | Mirror the RTDS Layer 2 stale-frame watchdog (`_rtds_stale_watchdog`) for the book WS. Drop and rebuild state on reconnect — it's a cheap REST reprime. | Reconnect storms (>3 in 5 min) in paper smoke → STOP. |
| 7 | TokenResolver lookups multiply by 4 (current YES + current NO + next YES + next NO). | Already cached in `TokenResolver`; resolves once per slug-rollover. The discovery mirrors `scrape_book.py` which has been running fine in production. | Lookup latency > 5 s on rollover → STOP, isolate. |
| 8 | Race between "first book frame arrives" and "first walked_vwap entry would fire". | The gate explicitly rejects with `no_book_subscription` or `empty_book` until books land. Dashboard shows reject reasons. By design, walked_vwap is silent for the first ~5 s of every market until the snapshot arrives. | If reject rate is >95% with `empty_book` for >2 min after first market — STOP, the WS path is broken. |
| 9 | Side convention error (Up/Down → asks/bids on wrong token). | Hand-computed unit test asserts: action `side="Up"` consumes `books.yes.asks`, `side="Down"` consumes `books.no.asks`. Fails loudly if the convention drifts. | Test red → STOP, do not paper away the bug. |
| 10 | Total scope balloons past two weeks. | Each task is sized 30–90 minutes; total estimate ~12–18 hours including review. Break out the dashboard work to a follow-up plan if the strategy + daemon land cleanly and observation is non-blocking. | Phase A+B+C+D > 3 sessions of focused work — STOP, descope dashboards. |

---

## Estimated total

- **Commits:** 12 (one per task that produces a logical diff; Task 0 prep + Task 11 final smoke don't commit)
- **LOC:** ~1500 lines added (mostly tests), ~200 lines modified across daemon + dashboards
- **Time:** 12–18 hours of focused work assuming subagent-driven execution; main-thread review adds ~30 % overhead
- **Scope boundary:** stop at end of Phase E. Reconciling walked_vwap entries against captured book feeds (matching reconcile.py's flow for refined) is OUT OF SCOPE — separate plan once we have data.

---

## Phase A — Book state machine (foundation, no daemon yet)

### Task A1: `LiveBookState` + `MarketBooks` + `walk_for_vwap`

**Files:**
- Create: `active_bots/execution/live_book_state.py`
- Create: `tests/execution/test_live_book_state.py`

**Subagent:** `general-purpose`. Brief it with: this task's full text, the user prompt's spec excerpt §2.4, and the path `active_bots/execution/book.py` for reference (Decimal version that we are NOT importing).

**Strategic context:** The daemon will hold one `LiveBookState` per (slug, side). Book WS messages drive `apply_snapshot` and `apply_delta`. The strategy reads `MarketBooks` snapshots via a synchronous getter the daemon exposes. Floats are fine: the gate is decision-time approximation, not the canonical fill simulator. A `# FUTURE-REFACTOR:` comment at the top of the file points at `experiments/backtest/harness.py:_RollingBookState` for the Decimal canonical class.

- [ ] **Step 1: Write `tests/execution/test_live_book_state.py` with failing tests**

```python
"""Unit tests for the daemon-side per-token book state machine."""
from __future__ import annotations

import pytest

from active_bots.execution.live_book_state import (
    LiveBookState,
    MarketBooks,
    walk_for_vwap,
)


def test_apply_snapshot_replaces_both_sides():
    s = LiveBookState(tick_size=0.01)
    s.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}, {"price": "0.39", "size": "5"}],
        asks=[{"price": "0.42", "size": "8"}, {"price": "0.43", "size": "20"}],
        ts_ms=1000,
    )
    assert s.bids == [(0.40, 10.0), (0.39, 5.0)]
    assert s.asks == [(0.42, 8.0), (0.43, 20.0)]
    assert s.ts_ms == 1000


def test_apply_delta_inserts_and_removes_levels():
    s = LiveBookState(tick_size=0.01)
    s.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}],
        ts_ms=1000,
    )
    s.apply_delta({"side": "BUY", "price": "0.41", "size": "5"}, ts_ms=1100)
    s.apply_delta({"side": "SELL", "price": "0.42", "size": "0"}, ts_ms=1100)
    assert s.bids == [(0.41, 5.0), (0.40, 10.0)]
    assert s.asks == []  # 0.42 removed, no other asks
    assert s.ts_ms == 1100


def test_apply_delta_before_snapshot_is_dropped():
    s = LiveBookState(tick_size=0.01)
    # No snapshot yet — apply_delta is a no-op + records the dropped count
    s.apply_delta({"side": "BUY", "price": "0.40", "size": "5"}, ts_ms=900)
    assert s.bids == []
    assert s.dropped_deltas_no_baseline == 1
    assert not s.has_baseline


def test_walk_for_vwap_full_fill():
    asks = [(0.42, 8.0), (0.43, 20.0), (0.45, 100.0)]
    res = walk_for_vwap(asks, requested_shares=10.0)
    # 8 @ 0.42 + 2 @ 0.43 = 10 shares; VWAP = (8*0.42 + 2*0.43) / 10 = 0.422
    assert res.classification == "full"
    assert res.filled_shares == pytest.approx(10.0)
    assert res.vwap == pytest.approx(0.422)
    assert res.levels_consumed == 2


def test_walk_for_vwap_partial_fill():
    asks = [(0.42, 8.0)]
    res = walk_for_vwap(asks, requested_shares=10.0)
    assert res.classification == "partial"
    assert res.filled_shares == pytest.approx(8.0)
    assert res.vwap == pytest.approx(0.42)
    assert res.residual_shares == pytest.approx(2.0)
    assert res.levels_consumed == 1


def test_walk_for_vwap_empty_book():
    res = walk_for_vwap([], requested_shares=10.0)
    assert res.classification == "unfilled"
    assert res.filled_shares == 0.0
    assert res.vwap is None
    assert res.residual_shares == pytest.approx(10.0)


def test_top_of_book_size_helper():
    s = LiveBookState(tick_size=0.01)
    s.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}],
        ts_ms=1000,
    )
    assert s.top_size("asks") == 8.0
    assert s.top_size("bids") == 10.0
    s.apply_snapshot(bids=[], asks=[], ts_ms=1100)
    assert s.top_size("asks") == 0.0
    assert s.top_size("bids") == 0.0


def test_market_books_wrapper_staleness():
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}],
        ts_ms=1000,
    )
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    assert mb.yes_staleness_ms(now_ms=1500) == 500
    assert mb.no_staleness_ms(now_ms=1500) is None  # never seen a snapshot
```

- [ ] **Step 2: Run tests to confirm fail**

```bash
cd /home/samsam/polymarket-hustle && conda activate polymarket-env && pytest tests/execution/test_live_book_state.py -v
```
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `active_bots/execution/live_book_state.py`**

```python
"""Daemon-side per-token book state machine for the walked-VWAP gate.

Floats throughout — decision-time approximation, not canonical fill
arithmetic. For the canonical Decimal version used by the offline
backtester see ``experiments/backtest/harness.py:_RollingBookState``
and ``active_bots/execution/book.py:walk_book``.

# FUTURE-REFACTOR: extract a shared Protocol once a third caller
# (e.g. an in-daemon shadow reconciler) needs the same state machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class WalkResult:
    classification: Literal["full", "partial", "unfilled"]
    filled_shares: float
    residual_shares: float
    vwap: float | None
    levels_consumed: int


def walk_for_vwap(
    levels: list[tuple[float, float]],
    requested_shares: float,
) -> WalkResult:
    """Walk levels (price, size) in given order, accumulating up to requested_shares."""
    if requested_shares <= 0 or not levels:
        return WalkResult(
            classification="unfilled",
            filled_shares=0.0,
            residual_shares=requested_shares,
            vwap=None,
            levels_consumed=0,
        )
    remaining = requested_shares
    notional = 0.0
    filled = 0.0
    consumed = 0
    for price, size in levels:
        if remaining <= 0:
            break
        take = size if size < remaining else remaining
        if take <= 0:
            break
        notional += price * take
        filled += take
        remaining -= take
        consumed += 1
    if filled <= 0:
        return WalkResult("unfilled", 0.0, requested_shares, None, 0)
    if abs(filled - requested_shares) < 1e-9:
        return WalkResult("full", filled, 0.0, notional / filled, consumed)
    return WalkResult("partial", filled, requested_shares - filled, notional / filled, consumed)


@dataclass
class LiveBookState:
    """Per-token book. apply_snapshot replaces both sides; apply_delta mutates one level.

    apply_delta before any snapshot is dropped (recorded in dropped_deltas_no_baseline).
    """
    tick_size: float
    bids: list[tuple[float, float]] = field(default_factory=list)  # descending
    asks: list[tuple[float, float]] = field(default_factory=list)  # ascending
    ts_ms: int = 0
    has_baseline: bool = False
    dropped_deltas_no_baseline: int = 0

    def apply_snapshot(
        self,
        bids: list[dict],
        asks: list[dict],
        ts_ms: int,
        tick_size: float | None = None,
    ) -> None:
        if tick_size is not None:
            self.tick_size = tick_size
        self.bids = sorted(
            ((float(lvl["price"]), float(lvl["size"]))
             for lvl in (bids or []) if float(lvl["size"]) > 0),
            key=lambda x: -x[0],
        )
        self.asks = sorted(
            ((float(lvl["price"]), float(lvl["size"]))
             for lvl in (asks or []) if float(lvl["size"]) > 0),
            key=lambda x: x[0],
        )
        self.ts_ms = ts_ms
        self.has_baseline = True

    def apply_delta(self, delta: dict, ts_ms: int) -> None:
        if not self.has_baseline:
            self.dropped_deltas_no_baseline += 1
            return
        side = (delta.get("side") or "").upper()
        try:
            price = float(delta["price"])
            size = float(delta["size"])
        except (KeyError, TypeError, ValueError):
            return
        if side == "BUY":
            self.bids = _apply_one(self.bids, price, size, descending=True)
        elif side == "SELL":
            self.asks = _apply_one(self.asks, price, size, descending=False)
        self.ts_ms = ts_ms

    def top_size(self, which: Literal["bids", "asks"]) -> float:
        levels = self.bids if which == "bids" else self.asks
        return levels[0][1] if levels else 0.0


def _apply_one(
    levels: list[tuple[float, float]],
    price: float,
    size: float,
    *,
    descending: bool,
) -> list[tuple[float, float]]:
    out = [lvl for lvl in levels if lvl[0] != price]
    if size > 0:
        out.append((price, size))
        out.sort(key=(lambda x: -x[0]) if descending else (lambda x: x[0]))
    return out


@dataclass
class MarketBooks:
    """Pair of YES + NO LiveBookState for a single market slug."""
    yes: LiveBookState | None = None
    no: LiveBookState | None = None

    def yes_staleness_ms(self, now_ms: int) -> int | None:
        if self.yes is None or not self.yes.has_baseline:
            return None
        return now_ms - self.yes.ts_ms

    def no_staleness_ms(self, now_ms: int) -> int | None:
        if self.no is None or not self.no.has_baseline:
            return None
        return now_ms - self.no.ts_ms
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
pytest tests/execution/test_live_book_state.py -v
```
Expected: 7/7 PASS.

- [ ] **Step 5: Commit**

```bash
git add active_bots/execution/live_book_state.py tests/execution/test_live_book_state.py
git commit -m "$(cat <<'EOF'
feat(execution): float-based per-token live book state machine

Decision-time approximation for the upcoming walked-VWAP gate. Decimal
canonical version stays in experiments/backtest. Adds LiveBookState,
MarketBooks wrapper, and a walk_for_vwap helper.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Acceptance:** `pytest tests/execution/test_live_book_state.py -v` → 7 passed; `git log -1 --oneline` shows the new commit.

---

## Phase B — WalkedVWAPStrategy (no daemon yet)

### Task B1: Scaffold `WalkedVWAPStrategy` + smoke test

**Files:**
- Create: `active_bots/walked_vwap_strategy.py`
- Create: `tests/strategy/__init__.py` (empty)
- Create: `tests/strategy/test_walked_vwap.py`

**Subagent:** `general-purpose`. Brief it with: this task + Task A1 (the LiveBookState contract).

**Strategic context:** Subclass `RefinedStrategy`. Expose `on_tick` with `*, books: MarketBooks | None = None` keyword-only param. When `books is None`, behave identically to `RefinedStrategy`. The gate logic comes in B4; this task is just the scaffold + identity behavior.

- [ ] **Step 1: Write `tests/strategy/__init__.py`**

(empty file)

- [ ] **Step 2: Write failing scaffold tests**

`tests/strategy/test_walked_vwap.py`:

```python
"""Unit tests for WalkedVWAPStrategy.

Tests are organized by phase:
- Scaffold: import, instantiation, identity behaviour without books.
- Computation (B2/B3): walked VWAP, fee curve.
- Gate (B4): each reject reason fires under the right condition.
- Partial-fill (B5): WALKED_VWAP_PARTIAL_OK flag behaviour.
"""
from __future__ import annotations

import time

import pytest

from active_bots.walked_vwap_strategy import WalkedVWAPStrategy
from active_bots.refined_strategy import RefinedStrategy
from active_bots.execution.live_book_state import LiveBookState, MarketBooks


def test_imports_cleanly():
    s = WalkedVWAPStrategy()
    assert isinstance(s, RefinedStrategy)


def test_no_books_falls_through_to_refined():
    """Without books kwarg, on_tick should behave exactly like RefinedStrategy."""
    s = WalkedVWAPStrategy()
    t0 = time.time() - 130  # entry window for refined is T+120..T+150
    # No books → expect None (refined would fire only with edge; we pass benign args)
    out = s.on_tick(
        btc_price=110_000.0,
        market_price_up=0.50,
        sigma=0.5,
        t_zero=t0,
        market_price_ts=time.time(),
    )
    # Without books, at p=mkt=0.5 fair=0.5 → no edge → None. Sanity: also reachable
    # via parent path. The point is: no exception, no books-required error.
    assert out is None or out.get("action") in ("ENTER", "EXIT_TP", "EXIT_SL", "RESOLVE")


def test_books_kwarg_accepted():
    """Calling with books=MarketBooks(...) does not raise."""
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    out = s.on_tick(
        btc_price=110_000.0,
        market_price_up=0.50,
        sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    # Empty books at p=0.50 with no fair-price edge: returns None (no entry signal upstream).
    assert out is None
```

- [ ] **Step 3: Run; expect fail**

```bash
pytest tests/strategy/test_walked_vwap.py -v
```
Expected: FAIL — module not found.

- [ ] **Step 4: Implement scaffold**

`active_bots/walked_vwap_strategy.py`:

```python
"""WalkedVWAPStrategy — RefinedStrategy with a book-aware entry gate.

Inherits all of Refined's TP/SL / squeeze-off / time-zone behaviour. The
override on on_tick adds a 5-step gate that runs AFTER Refined would have
returned an ENTER action but BEFORE the action propagates to the executor.
The gate rejects entries that would consume an empty / thin / fee-eaten
book — see docs/superpowers/plans/2026-04-27-walked-vwap-strategy.md.

Per-side convention: 'Up' buys YES (consumes books.yes.asks); 'Down' buys
NO (consumes books.no.asks). Both are LONG positions in different tokens.

Floats throughout. For canonical Decimal arithmetic see
experiments/backtest/replay_executor.py.
"""
from __future__ import annotations

import os
from typing import Any

from .refined_strategy import RefinedStrategy
from .execution.live_book_state import MarketBooks

# Defaults — env overridable per project convention.
MARKET_PRICE_MAX_STALENESS_S = float(
    os.environ.get("WALKED_VWAP_MARKET_STALENESS_S", 30.0)
)
MIN_TOP_OF_BOOK_SHARES_RATIO = float(
    os.environ.get("WALKED_VWAP_MIN_TOP_RATIO", 1.0)
)
WALKED_EDGE_MIN = float(os.environ.get("WALKED_VWAP_EDGE_MIN", 0.02))
WALKED_VWAP_PARTIAL_OK = (
    os.environ.get("WALKED_VWAP_PARTIAL_OK", "0").strip() in ("1", "true", "True")
)
FEE_CATEGORY = os.environ.get("WALKED_VWAP_FEE_CATEGORY", "crypto")


class WalkedVWAPStrategy(RefinedStrategy):
    """RefinedStrategy + walked-VWAP entry gate."""

    def on_tick(
        self,
        btc_price: float,
        market_price_up: float,
        sigma: float,
        t_zero: float,
        market_price_ts: float = 0.0,
        *,
        books: MarketBooks | None = None,
    ) -> dict[str, Any] | None:
        # Run the parent first.
        action = super().on_tick(
            btc_price=btc_price,
            market_price_up=market_price_up,
            sigma=sigma,
            t_zero=t_zero,
            market_price_ts=market_price_ts,
        )
        if action is None:
            return None
        # Only gate ENTER (and squeeze enter) actions; pass exits through unchanged.
        if action.get("action") not in ("ENTER",):
            return action
        # B4 implements the actual gate; for now, no-op pass-through.
        return action
```

- [ ] **Step 5: Run; expect pass**

```bash
pytest tests/strategy/test_walked_vwap.py -v
```
Expected: 3/3 PASS.

- [ ] **Step 6: Commit**

```bash
git add active_bots/walked_vwap_strategy.py tests/strategy/__init__.py tests/strategy/test_walked_vwap.py
git commit -m "$(cat <<'EOF'
feat(strategy): scaffold WalkedVWAPStrategy as RefinedStrategy sibling

Pass-through override on on_tick that accepts an optional books kwarg.
Gate logic lands in subsequent commits; this is the empty harness so
the daemon can wire it up in parallel with strategy work.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Acceptance:** `pytest tests/strategy/test_walked_vwap.py -v` → 3 passed; new commit landed.

---

### Task B2: Walked-VWAP computation per spec §2.4

**Files:**
- Modify: `active_bots/walked_vwap_strategy.py`
- Modify: `tests/strategy/test_walked_vwap.py`

**Subagent:** `general-purpose`.

**Strategic context:** The strategy already has `walk_for_vwap` from Task A1. We just wire it in and test with hand-computed cases. No gate firing yet — this task only adds the helper that the gate will call.

- [ ] **Step 1: Add failing tests for computation**

Append to `tests/strategy/test_walked_vwap.py`:

```python
def test_walked_vwap_for_action_up():
    """side=Up → consumes books.yes.asks."""
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}, {"price": "0.43", "size": "20"}],
        ts_ms=1000,
    )
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    res = s._walked_vwap_for_entry(
        side="Up", requested_shares=10.0, books=mb,
    )
    # 8 @ 0.42 + 2 @ 0.43 = 10 shares; VWAP = 0.422
    assert res.classification == "full"
    assert res.vwap == pytest.approx(0.422)


def test_walked_vwap_for_action_down():
    """side=Down → consumes books.no.asks."""
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    no = LiveBookState(tick_size=0.01)
    no.apply_snapshot(
        bids=[{"price": "0.55", "size": "10"}],
        asks=[{"price": "0.58", "size": "5"}, {"price": "0.59", "size": "20"}],
        ts_ms=1000,
    )
    mb = MarketBooks(yes=yes, no=no)
    res = s._walked_vwap_for_entry(
        side="Down", requested_shares=10.0, books=mb,
    )
    # 5 @ 0.58 + 5 @ 0.59 = 10 shares; VWAP = (5*0.58 + 5*0.59)/10 = 0.585
    assert res.classification == "full"
    assert res.vwap == pytest.approx(0.585)


def test_walked_vwap_partial():
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(
        bids=[],
        asks=[{"price": "0.42", "size": "5"}],
        ts_ms=1000,
    )
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    res = s._walked_vwap_for_entry(side="Up", requested_shares=10.0, books=mb)
    assert res.classification == "partial"
    assert res.filled_shares == pytest.approx(5.0)
    assert res.residual_shares == pytest.approx(5.0)
```

- [ ] **Step 2: Run; expect fail**

```bash
pytest tests/strategy/test_walked_vwap.py -k walked_vwap_for -v
```
Expected: FAIL — `_walked_vwap_for_entry` undefined.

- [ ] **Step 3: Implement helper**

In `active_bots/walked_vwap_strategy.py`, add (above the class):

```python
from .execution.live_book_state import (
    LiveBookState,
    MarketBooks,
    WalkResult,
    walk_for_vwap,
)
```

(Adjust the existing import if it duplicates.) Then add to the class:

```python
    def _walked_vwap_for_entry(
        self,
        side: str,
        requested_shares: float,
        books: MarketBooks,
    ) -> WalkResult:
        """Walk the appropriate token's asks for an entry of side ('Up'|'Down')."""
        if side == "Up":
            book = books.yes
        elif side == "Down":
            book = books.no
        else:
            return WalkResult("unfilled", 0.0, requested_shares, None, 0)
        if book is None or not book.has_baseline:
            return WalkResult("unfilled", 0.0, requested_shares, None, 0)
        return walk_for_vwap(book.asks, requested_shares)
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/strategy/test_walked_vwap.py -v
```
Expected: 6/6 PASS.

- [ ] **Step 5: Commit**

```bash
git add active_bots/walked_vwap_strategy.py tests/strategy/test_walked_vwap.py
git commit -m "feat(strategy): walked-VWAP computation per spec §2.4 ($(printf 'asks for Up, no_asks for Down'))

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

(If your shell doesn't like the printf, use a HEREDOC commit message instead. The point: side convention captured.)

**Acceptance:** `pytest tests/strategy/test_walked_vwap.py -v` → 6 passed.

---

### Task B3: Fee computation in strategy per spec §2.3

**Files:**
- Modify: `active_bots/walked_vwap_strategy.py`
- Modify: `tests/strategy/test_walked_vwap.py`

**Subagent:** `general-purpose`.

**Strategic context:** `active_bots/execution/fees.py` already has `fee_usdc(p, shares, category)` returning Decimal. Our gate needs a float wrapper that converts at the boundary. Test against §2.3 numbers at p=0.50 (peak fee) and p=0.10 (low-fee tail).

- [ ] **Step 1: Add failing tests**

Append to `tests/strategy/test_walked_vwap.py`:

```python
def test_post_fee_effective_vwap_at_peak():
    """At p=0.50, fee = (180/10000) * 0.5 * 0.5 * shares * 1.0 = 45 bps * shares."""
    s = WalkedVWAPStrategy()
    # 100 shares at fill_VWAP=0.50: fee = 0.0045 * 100 = $0.45
    eff = s._effective_vwap_after_fees(fill_vwap=0.50, filled_shares=100.0)
    # effective per-share = 0.50 + 0.45/100 = 0.5045
    assert eff == pytest.approx(0.5045)


def test_post_fee_effective_vwap_at_tail():
    """At p=0.10, fee = (180/10000) * 0.1 * 0.9 * shares * 1.0 = 16.2 bps * shares."""
    s = WalkedVWAPStrategy()
    # 100 shares at fill_VWAP=0.10: fee = 0.00162 * 100 = $0.162
    eff = s._effective_vwap_after_fees(fill_vwap=0.10, filled_shares=100.0)
    # effective per-share = 0.10 + 0.162/100 = 0.10162
    assert eff == pytest.approx(0.10162)


def test_post_fee_effective_vwap_zero_shares():
    s = WalkedVWAPStrategy()
    eff = s._effective_vwap_after_fees(fill_vwap=0.5, filled_shares=0.0)
    assert eff == 0.5  # no fee on zero shares; preserve fill_vwap


def test_post_fee_effective_vwap_at_extremes():
    """p=0 or p=1 → no fee per fees.py rules."""
    s = WalkedVWAPStrategy()
    assert s._effective_vwap_after_fees(0.0, 100.0) == 0.0
    assert s._effective_vwap_after_fees(1.0, 100.0) == 1.0
```

- [ ] **Step 2: Run; expect fail**

```bash
pytest tests/strategy/test_walked_vwap.py -k post_fee -v
```
Expected: FAIL — undefined.

- [ ] **Step 3: Implement helper**

In `active_bots/walked_vwap_strategy.py`, add imports:

```python
from decimal import Decimal

from .execution.fees import CATEGORIES, fee_usdc
```

Then on the class:

```python
    def _effective_vwap_after_fees(
        self,
        fill_vwap: float,
        filled_shares: float,
    ) -> float:
        """Per-share entry cost including the Polymarket bell-curve taker fee.

        Effective price = fill_vwap + fee_usdc / filled_shares (signed for the
        BUY side: paying more per share). Symmetric around p=0.50; zero at the
        extremes per fees.py.
        """
        if filled_shares <= 0:
            return fill_vwap
        category = CATEGORIES.get(FEE_CATEGORY, CATEGORIES["crypto"])
        fee = fee_usdc(
            p=Decimal(str(fill_vwap)),
            shares=Decimal(str(filled_shares)),
            category=category,
        )
        return fill_vwap + float(fee) / filled_shares
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/strategy/test_walked_vwap.py -v
```
Expected: 10/10 PASS.

- [ ] **Step 5: Commit**

```bash
git add active_bots/walked_vwap_strategy.py tests/strategy/test_walked_vwap.py
git commit -m "$(cat <<'EOF'
feat(strategy): integrate Polymarket bell-curve fee per spec §2.3

Wraps fees.fee_usdc(p, shares, CRYPTO) with a float boundary. Effective
post-fee VWAP feeds the gate's edge check in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Acceptance:** `pytest tests/strategy/test_walked_vwap.py -v` → 10 passed.

---

### Task B4: Gate orchestration with all 5 reject reasons

**Files:**
- Modify: `active_bots/walked_vwap_strategy.py`
- Modify: `tests/strategy/test_walked_vwap.py`

**Subagent:** `general-purpose`. Brief it with the user prompt's `Gate logic (in order)` section verbatim.

**Strategic context:** This is the load-bearing task. Replace the pass-through in `on_tick` with full gate logic. Reject paths return a sentinel action with `action="WALKED_VWAP_REJECT"` so the daemon can branch cleanly. Accepted paths pass the original action through with extra diagnostic fields merged in.

- [ ] **Step 1: Add failing tests for each gate reason**

Append to `tests/strategy/test_walked_vwap.py`:

```python
def _make_strategy_with_books(
    *,
    yes_asks=None, yes_bids=None,
    no_asks=None, no_bids=None,
    yes_ts_ms=1000, no_ts_ms=1000,
):
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(
        bids=yes_bids or [], asks=yes_asks or [], ts_ms=yes_ts_ms,
    )
    no = LiveBookState(tick_size=0.01)
    no.apply_snapshot(
        bids=no_bids or [], asks=no_asks or [], ts_ms=no_ts_ms,
    )
    return WalkedVWAPStrategy(), MarketBooks(yes=yes, no=no)


def _refined_would_enter_action():
    """Synthetic ENTER action shape RefinedStrategy returns."""
    return {
        "action": "ENTER",
        "side": "Up",
        "entry_price": 0.30,
        "edge": 0.20,
        "size_usdc": 5.0,
        "size_shares": 16.6667,  # $5 at $0.30
        "fair": 0.50,
        "market": 0.30,
        "time_zone": "sweet_spot",
    }


def test_gate_rejects_stale_market_price(monkeypatch):
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "100"}],
    )
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time() - 60,  # stale by 60s, threshold 30s
        books=mb,
    )
    assert out is not None
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "stale_market_price"


def test_gate_rejects_no_book_subscription(monkeypatch):
    s = WalkedVWAPStrategy()
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=None,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "no_book_subscription"


def test_gate_rejects_empty_book(monkeypatch):
    s, mb = _make_strategy_with_books(yes_asks=[])  # YES asks empty
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "empty_book"


def test_gate_rejects_insufficient_top_of_book(monkeypatch):
    # Top of book has 5 shares; we want 16.67 → reject because
    # MIN_TOP_OF_BOOK_SHARES_RATIO=1.0 means top must >= requested.
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "5"}],
    )
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "insufficient_top_of_book"


def test_gate_rejects_insufficient_walked_edge(monkeypatch):
    # Big book at 0.49 (just below fair=0.50). Walked VWAP ≈ 0.49,
    # post-fee ≈ 0.4945, walked_edge = 0.50 - 0.4945 = 0.0055 < 0.02 → reject.
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.49", "size": "100"}],
    )
    action = _refined_would_enter_action()
    action.update({"entry_price": 0.49, "fair": 0.50, "size_shares": 50.0})
    monkeypatch.setattr(s, "_run_parent_on_tick", lambda *a, **k: action)
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.49, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "insufficient_walked_edge"


def test_gate_passes_when_all_checks_satisfy(monkeypatch):
    # Big book at 0.30, fair=0.50, walked edge ≈ 0.20 minus tiny fees → passes.
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "1000"}],
    )
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "ENTER"
    assert "walked_VWAP" in out
    assert out["walked_VWAP"] == pytest.approx(0.30)
    assert "walked_edge" in out
    assert out["walked_edge"] > 0.02
    assert "book_top_size_take" in out
    assert out["book_top_size_take"] == pytest.approx(1000.0)
    assert "spread_at_entry" in out
    assert "book_staleness_at_entry_ms" in out
```

- [ ] **Step 2: Run; expect fail**

```bash
pytest tests/strategy/test_walked_vwap.py -v
```
Expected: FAIL — `_run_parent_on_tick` not defined; gate logic missing.

- [ ] **Step 3: Implement gate**

Update `active_bots/walked_vwap_strategy.py`. Replace `on_tick` and add helpers:

```python
import time as _time

# ...

class WalkedVWAPStrategy(RefinedStrategy):

    def _run_parent_on_tick(
        self, btc_price, market_price_up, sigma, t_zero, market_price_ts,
    ):
        # Indirection so tests can monkeypatch this seam.
        return RefinedStrategy.on_tick(
            self,
            btc_price=btc_price,
            market_price_up=market_price_up,
            sigma=sigma,
            t_zero=t_zero,
            market_price_ts=market_price_ts,
        )

    def on_tick(
        self,
        btc_price: float,
        market_price_up: float,
        sigma: float,
        t_zero: float,
        market_price_ts: float = 0.0,
        *,
        books: MarketBooks | None = None,
    ) -> dict[str, Any] | None:
        action = self._run_parent_on_tick(
            btc_price, market_price_up, sigma, t_zero, market_price_ts,
        )
        if action is None:
            return None
        if action.get("action") != "ENTER":
            return action
        return self._gate(action, market_price_ts=market_price_ts, books=books)

    def _gate(
        self,
        action: dict[str, Any],
        *,
        market_price_ts: float,
        books: MarketBooks | None,
    ) -> dict[str, Any]:
        now = _time.time()

        # 1. Staleness
        if (
            market_price_ts is None
            or market_price_ts <= 0
            or (now - market_price_ts) > MARKET_PRICE_MAX_STALENESS_S
        ):
            return self._reject(action, "stale_market_price",
                                staleness_s=(now - market_price_ts) if market_price_ts else None)

        # 2. Book availability
        if books is None:
            return self._reject(action, "no_book_subscription")
        side = action["side"]
        book = books.yes if side == "Up" else books.no
        if book is None or not book.has_baseline:
            return self._reject(action, "no_book_subscription")
        if not book.asks:
            return self._reject(action, "empty_book")

        requested_shares = float(action["size_shares"])
        top_size = book.top_size("asks")

        # 3. Top-of-book liquidity
        if top_size < requested_shares * MIN_TOP_OF_BOOK_SHARES_RATIO:
            return self._reject(
                action, "insufficient_top_of_book",
                book_top_size_take=top_size,
                requested_shares=requested_shares,
            )

        # 4. Walked VWAP + fees
        walk = walk_for_vwap(book.asks, requested_shares)
        if walk.classification == "unfilled":
            return self._reject(action, "empty_book")
        if walk.classification == "partial" and not WALKED_VWAP_PARTIAL_OK:
            return self._reject(
                action, "partial_fill_disallowed",
                walked_VWAP=walk.vwap, filled_shares=walk.filled_shares,
            )
        eff_vwap = self._effective_vwap_after_fees(walk.vwap, walk.filled_shares)
        fair = float(action.get("fair") or action.get("fair_price") or 0.0)
        # walked_edge for a BUY (long YES or long NO): fair - effective per-share
        walked_edge = fair - eff_vwap

        if walked_edge < WALKED_EDGE_MIN:
            return self._reject(
                action, "insufficient_walked_edge",
                walked_VWAP=walk.vwap,
                effective_VWAP=eff_vwap,
                walked_edge=walked_edge,
            )

        # 5. Pass — augment action with diagnostics
        opposite = book.bids
        spread = (book.asks[0][0] - opposite[0][0]) if (book.asks and opposite) else 0.0
        mid = (book.asks[0][0] + opposite[0][0]) / 2 if (book.asks and opposite) else book.asks[0][0]
        staleness_ms = int((now - book.ts_ms / 1000.0) * 1000) if book.ts_ms else None

        # Down-size if partial allowed
        if walk.classification == "partial" and WALKED_VWAP_PARTIAL_OK:
            action["size_shares"] = walk.filled_shares
            action["size_usdc"] = walk.filled_shares * walk.vwap

        action.update({
            "walked_VWAP": walk.vwap,
            "effective_VWAP": eff_vwap,
            "walked_edge": walked_edge,
            "book_top_size_take": top_size,
            "book_top_size_make": book.top_size("bids"),
            "spread_at_entry": (spread / mid) if mid > 0 else 0.0,
            "book_staleness_at_entry_ms": staleness_ms,
            "gate_passed": True,
        })
        return action

    def _reject(
        self,
        original_action: dict[str, Any],
        reason: str,
        **diag: Any,
    ) -> dict[str, Any]:
        return {
            "action": "WALKED_VWAP_REJECT",
            "reject_reason": reason,
            "side": original_action.get("side"),
            "intended_size_usdc": original_action.get("size_usdc"),
            "intended_size_shares": original_action.get("size_shares"),
            "fair_at_decision": original_action.get("fair"),
            "market_at_decision": original_action.get("market"),
            **diag,
        }
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/strategy/test_walked_vwap.py -v
```
Expected: 16/16 PASS (10 prior + 6 new).

- [ ] **Step 5: Commit**

```bash
git add active_bots/walked_vwap_strategy.py tests/strategy/test_walked_vwap.py
git commit -m "$(cat <<'EOF'
feat(strategy): walked-VWAP gate with staleness, book, edge checks

Five gate reasons fire in order: stale market price, missing book
subscription, empty book, insufficient top-of-book size, insufficient
post-fee walked edge. Rejects emit a WALKED_VWAP_REJECT action with
the diagnostic fields the daemon will log to events.jsonl.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Acceptance:** `pytest tests/strategy/test_walked_vwap.py -v` → 16 passed.

---

### Task B5: `WALKED_VWAP_PARTIAL_OK` flag tests

**Files:**
- Modify: `tests/strategy/test_walked_vwap.py` only (no production change — flag is already wired in B4)

**Subagent:** `general-purpose` (small).

- [ ] **Step 1: Add tests covering both flag values**

Append to `tests/strategy/test_walked_vwap.py`:

```python
def test_partial_fill_off_rejects(monkeypatch):
    """PARTIAL_OK=False: book can't fill full size → reject."""
    monkeypatch.setattr("active_bots.walked_vwap_strategy.WALKED_VWAP_PARTIAL_OK", False)
    monkeypatch.setattr("active_bots.walked_vwap_strategy.MIN_TOP_OF_BOOK_SHARES_RATIO", 0.0)
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "5"}],  # 5 shares avail, want 16.67
    )
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "partial_fill_disallowed"


def test_partial_fill_on_downsizes(monkeypatch):
    """PARTIAL_OK=True: fill what we can, downsize entry."""
    monkeypatch.setattr("active_bots.walked_vwap_strategy.WALKED_VWAP_PARTIAL_OK", True)
    monkeypatch.setattr("active_bots.walked_vwap_strategy.MIN_TOP_OF_BOOK_SHARES_RATIO", 0.0)
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "5"}],
    )
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "ENTER"
    assert out["size_shares"] == pytest.approx(5.0)
    assert out["walked_VWAP"] == pytest.approx(0.30)
```

- [ ] **Step 2: Run**

```bash
pytest tests/strategy/test_walked_vwap.py -v
```
Expected: 18/18 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/strategy/test_walked_vwap.py
git commit -m "test(strategy): cover WALKED_VWAP_PARTIAL_OK both branches

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Acceptance:** `pytest tests/strategy/test_walked_vwap.py -v` → 18 passed.

---

## Phase C — Daemon book WS subscription (no strategy hook yet)

### Task C1: `clob_book_feed` coroutine + DaemonState plumbing

**Files:**
- Modify: `daemon_base_v1.py`
- Create: `tests/daemon/__init__.py` (empty)
- Create: `tests/daemon/test_book_subscription.py`

**Subagent:** `general-purpose`. Brief it: this task + `scripts/scrape_book.py` lines 421–530 (the WS subscribe + dispatch shape) + Task A1 (LiveBookState contract).

**Strategic context:** Add an asyncio coroutine that opens a single WS to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribes to YES + NO of current+next markets (re-subscribes on rollover), maintains `MarketBooks` per slug in `DaemonState.books_by_slug`, and includes a stale-frame watchdog. Strategy hookup comes in Task D1.

- [ ] **Step 1: Write failing test**

`tests/daemon/test_book_subscription.py`:

```python
"""Tests for daemon_base_v1.clob_book_feed message dispatch.

Uses a fake async generator standing in for the WS connection. Exercises
the dispatcher (snapshot, price_change, last_trade_price, tick_size_change)
into the per-token state machine.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from active_bots.execution.live_book_state import LiveBookState, MarketBooks
from daemon_base_v1 import _dispatch_book_message  # to be added


def _state_with_token(asset_id: str, slug: str, side: str):
    state = {asset_id: {"slug": slug, "side": side, "book": LiveBookState(tick_size=0.01)}}
    return state


def test_book_event_replaces_state():
    state = _state_with_token("AID1", "btc-updown-5m-100", "yes")
    msg = {
        "event_type": "book",
        "asset_id": "AID1",
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.42", "size": "8"}],
    }
    _dispatch_book_message(msg, state, ts_ms=1000)
    assert state["AID1"]["book"].asks == [(0.42, 8.0)]


def test_price_change_after_baseline():
    state = _state_with_token("AID1", "btc-updown-5m-100", "yes")
    state["AID1"]["book"].apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}],
        ts_ms=900,
    )
    msg = {
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "AID1", "side": "SELL", "price": "0.42", "size": "0"},
        ],
    }
    _dispatch_book_message(msg, state, ts_ms=1100)
    assert state["AID1"]["book"].asks == []


def test_unknown_asset_id_dropped():
    state = _state_with_token("AID1", "btc-updown-5m-100", "yes")
    msg = {
        "event_type": "book",
        "asset_id": "AID_UNKNOWN",
        "bids": [], "asks": [],
    }
    _dispatch_book_message(msg, state, ts_ms=1000)
    assert state["AID1"]["book"].has_baseline is False


def test_tick_size_change_updates_state():
    state = _state_with_token("AID1", "btc-updown-5m-100", "yes")
    state["AID1"]["book"].apply_snapshot(bids=[], asks=[], ts_ms=900)
    msg = {
        "event_type": "tick_size_change",
        "asset_id": "AID1",
        "new_tick_size": "0.001",
    }
    _dispatch_book_message(msg, state, ts_ms=1100)
    assert state["AID1"]["book"].tick_size == 0.001
```

- [ ] **Step 2: Run; expect fail**

```bash
pytest tests/daemon/test_book_subscription.py -v
```
Expected: FAIL — `_dispatch_book_message` undefined.

- [ ] **Step 3: Implement in `daemon_base_v1.py`**

Add near the top:

```python
from active_bots.execution.live_book_state import LiveBookState, MarketBooks
```

Add constants:

```python
WS_CLOB_BOOK = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BOOK_WS_PING_INTERVAL_S = 15.0
BOOK_WS_PING_TIMEOUT_S = 10.0
BOOK_WS_STALE_S = 30.0
```

Add to `DaemonState.__init__`:

```python
        # Per-slug book registry — populated by clob_book_feed.
        # books_by_slug[slug] = MarketBooks(yes=..., no=...)
        self.books_by_slug: dict[str, "MarketBooks"] = {}
        # Per-asset_id metadata for the WS dispatcher
        # token_index[asset_id] = {"slug": str, "side": "yes"|"no", "book": LiveBookState}
        self.token_index: dict[str, dict] = {}
        self.connections["clob_book"] = False
```

Add the dispatcher (module-level so it's testable):

```python
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
```

Add the coroutine:

```python
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
```

Wire it into `run()` alongside the other tasks:

```python
    tasks = [
        asyncio.create_task(binance_feed(state, ewma)),
        asyncio.create_task(rtds_feed(state)),
        asyncio.create_task(clob_book_feed(state)),
        asyncio.create_task(
            strategy_loop(state, executor, resolver, risk, events, max_risk=effective_max)
        ),
        asyncio.create_task(state_persister(state)),
    ]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/daemon/test_book_subscription.py -v
```
Expected: 4/4 PASS.

- [ ] **Step 5: Commit**

```bash
git add daemon_base_v1.py tests/daemon/__init__.py tests/daemon/test_book_subscription.py
git commit -m "$(cat <<'EOF'
feat(daemon): subscribe to CLOB book WS for current+next markets

Adds clob_book_feed coroutine running in parallel with binance_feed and
rtds_feed. Maintains LiveBookState per (slug, yes|no) for the strategy
loop to read. Reconnect drops baselines so the next snapshot re-anchors;
strategy gate sees empty_book / no_book_subscription during the gap.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Acceptance:** `pytest tests/daemon/test_book_subscription.py -v` → 4 passed; daemon starts (paper mode) and `daemon.log` contains `CLOB book WS connected` within 30 s.

---

## Phase D — Daemon walked_vwap strategy block

### Task D1: DaemonState fields + strategy_loop block

**Files:**
- Modify: `daemon_base_v1.py`

**Subagent:** `general-purpose`. **Heightened Stage 2 review** — this task is load-bearing. Have the reviewer specifically check that the existing refined/enhanced/base blocks are byte-for-byte identical except for the new state fields wherever shared utilities touch.

**Strategic context:** Add `walked_*` fields to DaemonState mirroring `refined_*`. Add a 4th block in `strategy_loop` that mirrors the refined block, but: (a) calls `WalkedVWAPStrategy.on_tick(..., books=...)`, (b) handles the new `WALKED_VWAP_REJECT` action by emitting `entry_rejected` events with the diagnostic fields, (c) uses the same executor as refined (live or paper depending on env). Reset on rollover. Resolve on T+300.

- [ ] **Step 1: Add state fields**

In `DaemonState.__init__`:

```python
        # Walked-VWAP strategy state (new candidate; gates entries on book)
        self.walked_fair_price = None
        self.walked_position = None
        self.walked_trades = []
        self.walked_stats = {
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
        self.walked_extra = {
            "last_exit_type": None,
            "last_time_zone": None,
            "last_source": None,
            "tp_count": 0,
            "sl_count": 0,
            "resolution_count": 0,
            "edge_trades": 0,
            "reject_count": 0,
            "reject_reasons": {},  # reason -> count
            "last_walked_edge": None,
        }
```

In `DaemonState.detect_market`, add `self.walked_fair_price = None` reset.
In `DaemonState.recompute_fair`, add `self.walked_fair_price = fair`.
In `DaemonState.to_dict`, add the `"walked_vwap": {...}` block mirroring `"refined"`.

- [ ] **Step 2: Import the strategy and instantiate**

At top of `daemon_base_v1.py`:

```python
from active_bots.walked_vwap_strategy import WalkedVWAPStrategy
```

In `strategy_loop`, after the existing `refined = RefinedStrategy(...)`:

```python
    walked = WalkedVWAPStrategy(max_risk=max_risk)
```

In the rollover block (right after `refined.reset(...)`):

```python
            walked.reset(t_zero=state.t_zero, strike=state.strike)
```

In rollover resolution (after the refined-position resolution):

```python
                if state.walked_position is not None:
                    if state.btc_price > 0 and prev_ctx is not None:
                        result = executor.resolve(
                            state.walked_position, prev_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.walked_stats, trade)
                            risk.record_trade(trade["pnl"], trade.get("resolved_time"))
                            state.walked_trades.append(trade)
                            if len(state.walked_trades) > MAX_CLOSED_TRADES:
                                state.walked_trades = state.walked_trades[-MAX_CLOSED_TRADES:]
                            state.walked_extra["resolution_count"] += 1
                            state.walked_extra["last_exit_type"] = "RESOLUTION"
                            outcome = "WIN" if trade["won"] else "LOSS"
                            events.log("resolve", strategy="walked_vwap", trade=trade, trigger="rollover")
                            logger.info(
                                "WALKED_VWAP RESOLVED [%s] %s %s PnL=%+.2f",
                                outcome, trade["side"], trade["slug"], trade["pnl"],
                            )
                    state.walked_position = None
```

- [ ] **Step 3: Add the per-tick walked_vwap entry block**

After the refined entry block in `strategy_loop`:

```python
            # ── Walked-VWAP strategy tick ──
            if (
                state.sigma and state.btc_price > 0
                and state.walked_position is None
                and market_ctx is not None
            ):
                market_up = state.market_price_up
                if market_up is not None:
                    books = state.books_by_slug.get(state.slug)
                    walked_action = walked.on_tick(
                        state.btc_price, market_up, state.sigma, state.t_zero,
                        market_price_ts=state.market_price_ts,
                        books=books,
                    )
                    if walked_action is not None:
                        action_type = walked_action.get("action")

                        if action_type == "ENTER":
                            tz = walked_action.get("time_zone")
                            events.log(
                                "entry_signal",
                                strategy="walked_vwap", source="edge",
                                side=walked_action.get("side"),
                                entry_price=walked_action.get("entry_price"),
                                edge=walked_action.get("edge"),
                                size_usdc=walked_action.get("size_usdc"),
                                time_zone=tz, offset=offset, slug=state.slug,
                                walked_VWAP=walked_action.get("walked_VWAP"),
                                effective_VWAP=walked_action.get("effective_VWAP"),
                                walked_edge=walked_action.get("walked_edge"),
                                book_top_size_take=walked_action.get("book_top_size_take"),
                                book_top_size_make=walked_action.get("book_top_size_make"),
                                spread_at_entry=walked_action.get("spread_at_entry"),
                                book_staleness_at_entry_ms=walked_action.get(
                                    "book_staleness_at_entry_ms"
                                ),
                            )
                            state.walked_extra["last_walked_edge"] = walked_action.get("walked_edge")
                            result = executor.enter(
                                walked_action, market_ctx, now, source="edge",
                            )
                            if result is not None:
                                result.ack_ts = time.time()
                                state.walked_position = result.to_position_dict()
                                state.walked_extra["edge_trades"] += 1
                                state.walked_extra["last_source"] = "edge"
                                state.walked_extra["last_time_zone"] = tz
                                events.log(
                                    "entry_filled",
                                    strategy="walked_vwap", source="edge",
                                    position=result.to_position_dict(),
                                    order_id=result.order_id, token_id=result.token_id,
                                    fill_details=result.fill_details,
                                )
                                logger.info(
                                    "WALKED_VWAP ENTRY %s %s @%.3f edge=%.3f $%.2f tz=%s",
                                    result.side, result.slug, result.entry_price,
                                    result.edge, result.size_usdc, tz,
                                )
                            else:
                                events.log(
                                    "entry_rejected",
                                    strategy="walked_vwap", source="edge",
                                    side=walked_action.get("side"),
                                    slug=state.slug,
                                    reason="executor_rejected",
                                )

                        elif action_type == "WALKED_VWAP_REJECT":
                            reason = walked_action.get("reject_reason", "unknown")
                            state.walked_extra["reject_count"] += 1
                            counts = state.walked_extra["reject_reasons"]
                            counts[reason] = counts.get(reason, 0) + 1
                            state.walked_extra["last_walked_edge"] = walked_action.get("walked_edge")
                            events.log(
                                "entry_rejected",
                                strategy="walked_vwap", source="edge",
                                slug=state.slug,
                                reason=reason,
                                **{k: v for k, v in walked_action.items()
                                   if k not in ("action", "reject_reason")},
                            )
```

- [ ] **Step 4: Add the walked_vwap exit/resolve block**

After the refined exit block:

```python
            # ── Walked-VWAP: TP/SL/RESOLVE ──
            if (
                state.walked_position is not None and state.sigma
                and state.btc_price > 0 and market_ctx is not None
            ):
                books = state.books_by_slug.get(state.slug)
                walked_action = walked.on_tick(
                    state.btc_price, state.market_price_up or 0.5, state.sigma,
                    state.t_zero, market_price_ts=state.market_price_ts,
                    books=books,
                )
                if walked_action is not None:
                    action_type = walked_action.get("action")
                    if action_type in ("EXIT_TP", "EXIT_SL"):
                        result = executor.exit(
                            state.walked_position, walked_action, market_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.walked_stats, trade)
                            risk.record_trade(trade["pnl"], trade.get("resolved_time"))
                            state.walked_trades.append(trade)
                            if len(state.walked_trades) > MAX_CLOSED_TRADES:
                                state.walked_trades = state.walked_trades[-MAX_CLOSED_TRADES:]
                            if result.exit_type == "TP":
                                state.walked_extra["tp_count"] += 1
                            else:
                                state.walked_extra["sl_count"] += 1
                            state.walked_extra["last_exit_type"] = result.exit_type
                            state.walked_position = None
                            events.log(
                                "exit_filled", strategy="walked_vwap", trade=trade,
                                fill_details=result.fill_details,
                            )
                            logger.info(
                                "WALKED_VWAP %s %s %s PnL=%+.2f hold=%.0fs",
                                result.exit_type, result.side, result.slug,
                                result.pnl, result.hold_time_s or 0,
                            )
                    elif action_type == "RESOLVE":
                        result = executor.resolve(
                            state.walked_position, market_ctx, now,
                            btc_price=state.btc_price,
                        )
                        if result is not None:
                            trade = result.to_trade_dict()
                            update_stats(state.walked_stats, trade)
                            risk.record_trade(trade["pnl"], trade.get("resolved_time"))
                            state.walked_trades.append(trade)
                            if len(state.walked_trades) > MAX_CLOSED_TRADES:
                                state.walked_trades = state.walked_trades[-MAX_CLOSED_TRADES:]
                            state.walked_extra["resolution_count"] += 1
                            state.walked_extra["last_exit_type"] = "RESOLUTION"
                            outcome = "WIN" if trade["won"] else "LOSS"
                            events.log("resolve", strategy="walked_vwap", trade=trade)
                            logger.info(
                                "WALKED_VWAP RESOLVED [%s] %s %s PnL=%+.2f",
                                outcome, trade["side"], trade["slug"], trade["pnl"],
                            )
                        state.walked_position = None
```

- [ ] **Step 5: Manual paper smoke**

Run:

```bash
cd /home/samsam/polymarket-hustle && conda activate polymarket-env && \
POLYMARKET_MODE=paper python3 daemon_base_v1.py 2>&1 | tee /tmp/d.log &
sleep 600 && pkill -f daemon_base_v1.py
```

Then:

```bash
grep -c "CLOB book WS connected" daemon_state/daemon.log
grep -c '"strategy":"walked_vwap"' daemon_state/events.jsonl
grep -c '"reason":"empty_book"' daemon_state/events.jsonl
grep -c '"reason":"insufficient_walked_edge"' daemon_state/events.jsonl
```

Expected: ≥1 connect log line, ≥1 walked_vwap event, mix of reject reasons. NOT 100 % rejects, NOT 100 % entries.

- [ ] **Step 6: Commit**

```bash
git add daemon_base_v1.py
git commit -m "$(cat <<'EOF'
feat(daemon): wire WalkedVWAPStrategy as a 4th parallel strategy

Mirrors the refined block in DaemonState/strategy_loop. Reads
state.books_by_slug for the current market and dispatches with books=mb.
Reject events emit with reason + diagnostic fields so the gate is
tunable from events.jsonl.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Acceptance:** Paper smoke shows ≥1 walked_vwap event in events.jsonl, with both ENTER and reject events present; existing base/enhanced/refined strategies still emit at their normal cadence (regression check via `grep -c '"strategy":"refined"' daemon_state/events.jsonl` shows non-zero).

---

## Phase E — Dashboards

These two tasks are independent and can be dispatched to subagents in parallel. Both consume the `walked_vwap` blob in `state.json` and `walked_vwap` strategy events in `events.jsonl`. Each dashboard already has the wiring for one strategy as primary (refined); the change is structural, not algorithmic.

### Task E1: TUI dashboard — promote walked_vwap to primary

**Files:**
- Modify: `tui/python/dashboard.py`
- Modify: `tui/tests/test_main_strategy.py`, `tui/tests/test_pnl_replay.py`, `tui/tests/test_baselines.py`, `tui/tests/test_events_tailer.py`

**Subagent:** `general-purpose`. Brief it: this task + the existing `tui/python/dashboard.py` for the current refined-as-primary structure.

**Strategic context:** The TUI's `MainStrategyWidget` reads `state["refined"]` and uses `pnl_series.get("refined", [])`. Switch both to `walked_vwap`. `BaselinesWidget` currently shows `base` + `enhanced` — add a `refined` row. Add a "Reject summary" Static under the headline showing `reject_count` and the top 3 `reject_reasons`. Re-label headline to `walked_vwap (candidate)`. Update `EventsTailer` to also accumulate `walked_vwap_actions` and `_compute_stats` to handle the new strategy.

- [ ] **Step 1: Update `EventsTailer` to track walked_vwap**

```python
        self.walked_actions: deque[dict] = deque(maxlen=ACTION_CAP)
```

In `_dispatch`:

```python
            elif strat == "walked_vwap":
                self.walked_actions.append(action)
```

PnL series already keyed by `strategy` field — handles `walked_vwap` automatically.

- [ ] **Step 2: Update `MainStrategyWidget` to render walked_vwap blob**

Replace `state.get("refined")` → `state.get("walked_vwap")` in `render_state`. Replace `"refined"` ID prefixes in DOM (`#refined-headline` etc. → `#walked-headline`). Headline label: `"walked_vwap (candidate)"`. Add a third static under headline:

```python
        yield Static("", id="walked-rejects")
```

Render:

```python
    def _render_rejects(self, extra: dict) -> None:
        ct = int(extra.get("reject_count") or 0)
        reasons = extra.get("reject_reasons") or {}
        top3 = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
        line = Text()
        line.append(f"rejects: {ct}  ", style="dim" if ct == 0 else "yellow")
        for r, n in top3:
            line.append(f"{r}={n}  ", style="dim")
        last_we = extra.get("last_walked_edge")
        if last_we is not None:
            line.append(f"  last walked_edge {last_we:+.4f}",
                        style="green" if last_we > 0 else "red")
        self.query_one("#walked-rejects", Static).update(line)
```

Call `self._render_rejects(extra)` inside `render_state`.

- [ ] **Step 3: Update `BaselinesWidget` to show three baselines (base / enhanced / refined)**

```python
        for key, label in (
            ("base", "BASE     "),
            ("enhanced", "ENHANCED "),
            ("refined", "REFINED  "),
        ):
            ...
```

CSS row count for `BaselinesWidget` may need bumping from 4 to 5 in `tui/python/dashboard.tcss`.

- [ ] **Step 4: Update DashboardApp `_tick`**

```python
        self.query_one(MainStrategyWidget).render_state(
            state,
            self.tailer.pnl_series.get("walked_vwap", []),
        )
        self.query_one(OrdersLogWidget).ingest(self.tailer.walked_actions)
```

- [ ] **Step 5: Update tests**

Find every test that hard-codes `"refined"` for the primary panel. Replace with `"walked_vwap"`. Examples (paths from `tui/tests/`):

- `test_main_strategy.py`: `state.get("refined")` → `state.get("walked_vwap")`
- `test_pnl_replay.py`: PnL series tests under `"refined"` → `"walked_vwap"`
- `test_baselines.py`: assert `"REFINED"` row appears alongside `"BASE"` + `"ENHANCED"`
- `test_events_tailer.py`: walked_actions deque populated when event has `"strategy": "walked_vwap"`

- [ ] **Step 6: Run TUI tests**

```bash
pytest tui/tests -v
```
Expected: all pass.

- [ ] **Step 7: Manual TUI verification (TTY required)**

Launch the daemon then in another pane:

```bash
python3 tui/python/dashboard.py
```

Verify: header unchanged; main panel labelled `walked_vwap (candidate)`; reject row visible; baselines row shows BASE / ENHANCED / REFINED.

- [ ] **Step 8: Commit**

```bash
git add tui/python/dashboard.py tui/python/dashboard.tcss tui/tests/
git commit -m "$(cat <<'EOF'
feat(dashboard): promote walked_vwap to primary in TUI

Renames refined panel to walked_vwap (candidate); demotes refined to
the baselines row alongside base + enhanced. Adds reject summary
(count + top 3 reasons + last walked_edge) under the main headline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Acceptance:** TUI tests pass; running dashboard against the live daemon shows walked_vwap in the primary slot with reject counts ticking when entries are rejected.

---

### Task E2: Web dashboard — promote walked_vwap to primary

**Files:**
- Modify: `scripts/live_dashboard.py`

**Subagent:** `general-purpose`. Brief it: this task + the existing `scripts/live_dashboard.py` (especially the HTML template region around `strat-base`/`strat-enh` and the JS `renderStrategyPanel` calls).

**Strategic context:** Web dashboard's `INDEX_HTML` declares panels in order. Currently `strat-base` and `strat-enh` sit side by side. Add `strat-walked-vwap` as a full-width panel ABOVE the two-col baselines panels. Add `strat-refined` as a third small panel alongside base/enhanced. JS `renderStrategyPanel` is generic on the key — just add the three calls. Add a small `<div id="walked-rejects">` between the walked-vwap panel header and its content. Backend tailer aggregates `walked_vwap` events automatically (it spreads on `strategy`).

- [ ] **Step 1: Add HTML panels**

In `INDEX_HTML`, locate the existing block:

```html
<div class="two-col">
  <div class="panel" id="strat-base">...</div>
  <div class="panel" id="strat-enh">...</div>
</div>
```

Replace with:

```html
<div class="panel" id="strat-walked-vwap">
  <div class="panel-title">walked_vwap (candidate)</div>
  <div id="walked-rejects" class="hbar" style="margin-bottom:0.5rem;color:var(--dim)"></div>
</div>

<div class="two-col">
  <div class="panel" id="strat-base">...</div>
  <div class="panel" id="strat-enh">...</div>
  <div class="panel" id="strat-refined"><div class="panel-title">refined (baseline)</div></div>
</div>
```

(Adjust `.two-col` to a 3-column grid for the baselines row, OR use a separate `.three-col` class — minor CSS.)

- [ ] **Step 2: Wire renderStrategyPanel + reject summary**

In the JS, locate the existing renderStrategyPanel block. Add:

```javascript
renderStrategyPanel('strat-walked-vwap', 'walked_vwap', '(candidate)',
                    d.walked_vwap || {}, mktUp, 'walked_vwap');
renderStrategyPanel('strat-refined', 'refined', '(baseline)',
                    d.refined || {}, mktUp, 'refined');
renderRejectSummary((d.walked_vwap && d.walked_vwap.extra) || {});
```

Add `renderRejectSummary`:

```javascript
function renderRejectSummary(extra) {
  const el = document.getElementById('walked-rejects');
  if (!el) return;
  const ct = extra.reject_count || 0;
  const reasons = extra.reject_reasons || {};
  const top3 = Object.entries(reasons).sort((a,b) => b[1]-a[1]).slice(0, 3);
  const lastWe = extra.last_walked_edge;
  let html = `<span><span class="lbl">rejects</span><span class="val">${ct}</span></span>`;
  for (const [r, n] of top3) {
    html += `  <span><span class="lbl">${r}</span><span class="val">${n}</span></span>`;
  }
  if (typeof lastWe === 'number') {
    const c = lastWe > 0 ? 'var(--green)' : 'var(--red)';
    html += `  <span><span class="lbl">last walked_edge</span><span class="val" style="color:${c}">${lastWe.toFixed(4)}</span></span>`;
  }
  el.innerHTML = html;
}
```

- [ ] **Step 3: Update events stream coloring (optional)**

In the events-stream renderer, add a colour prefix for walked_vwap (cyan/yellow — pick something distinct from enhanced's cyan and refined's existing colour). Skip if it's not trivially gated on `strategy === 'walked_vwap'`.

- [ ] **Step 4: Update Python backend if state shape requires it**

`scripts/live_dashboard.py` reads `state.json` and serialises to the WS. The new `walked_vwap` blob (added in Task D1's `to_dict`) flows automatically. No backend change required unless the script does explicit per-strategy whitelisting — grep:

```bash
grep -n '"refined"\|"enhanced"\|"base"' scripts/live_dashboard.py
```

If any explicit whitelist appears, add `"walked_vwap"` to it.

- [ ] **Step 5: Manual smoke test**

```bash
python3 scripts/live_dashboard.py --port 3006 &
# open http://localhost:3006
```

Verify: walked_vwap panel renders with reject summary; refined demoted to baselines row; existing base/enhanced panels unchanged.

- [ ] **Step 6: Commit**

```bash
git add scripts/live_dashboard.py
git commit -m "$(cat <<'EOF'
feat(dashboard): promote walked_vwap to primary in web dashboard

Adds full-width strat-walked-vwap panel with reject-count summary;
demotes refined into the baselines row alongside base + enhanced.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Acceptance:** Browser load shows walked_vwap as primary panel above base/enhanced/refined baselines.

---

## Phase F — End-to-end smoke

### Task F1: Paper-mode 10-minute smoke (no commit)

- [ ] Launch `POLYMARKET_MODE=paper python3 daemon_base_v1.py` for 10 minutes.
- [ ] Verify `daemon_state/events.jsonl`:
  - `grep -c '"type":"entry_filled".*"strategy":"walked_vwap"'` ≥ 1
  - `grep -c '"type":"entry_rejected".*"strategy":"walked_vwap"'` ≥ 1
  - Existing strategies untouched: `grep -c '"strategy":"refined"' events.jsonl` shows usual cadence.
- [ ] TUI dashboard shows reject summary updating; mainpanel labelled `walked_vwap (candidate)`.
- [ ] Web dashboard at :3006 shows walked_vwap as primary.
- [ ] Stop the daemon cleanly. No commit.

### Task F2: Live-dryrun 30-minute smoke (manual, after user reviews paper smoke; no commit)

- [ ] User confirms paper smoke is healthy.
- [ ] Launch via `MAX_TRADE_SIZE_USDC=1 bash launch_daemon.sh live dryrun` for 30 minutes (operator runs this; the WireGuard netns is required).
- [ ] Verify same checks as F1 with the live book WS path active.

---

## Self-review against the spec

**Spec-coverage matrix:**

| User-prompt requirement | Covered in |
|---|---|
| New strategy variant as parallel sibling, not extension of refined | B1 — subclass but lives in own file; no edits to refined |
| Subclass RefinedStrategy | B1 |
| Override on_tick with `book` kwarg | B4 — single `books` arg as MarketBooks (see Open decisions §1) |
| Gate runs after edge check, before returning ENTER | B4 — `super().on_tick` first |
| Stale market price check (Layer 0) | B4, configurable via `WALKED_VWAP_MARKET_STALENESS_S` |
| Book-availability check (None / empty) | B4 |
| Top-of-book size ratio check | B4, configurable via `WALKED_VWAP_MIN_TOP_RATIO` |
| Walked VWAP per spec §2.4 | A1 + B2 |
| Polymarket bell-curve fee per spec §2.3 | B3 — wraps existing `fees.fee_usdc` |
| Effective VWAP including fees | B3 |
| Post-fee edge check (`WALKED_EDGE_MIN`) | B4 |
| Partial-fill flag (default False) | B4 + B5 |
| ENTER action carries diagnostic fields | B4 |
| Reject events emitted to events.jsonl | D1 — daemon side |
| New action type `WALKED_VWAP_REJECT` so daemon branches cleanly | B4 |
| Daemon adds book WS coroutine subscribing to YES + NO | C1 |
| Reuses scrape_book.py's subscription pattern | C1 |
| Independent of scrape_book.py process | C1 |
| Per-token in-memory state machine, snapshot+delta+tick_change | A1 + C1 |
| Float-based, decision-time approximation | A1 (Open decisions §2) |
| Daemon threads books into walked_vwap on_tick | D1 |
| Existing strategies unchanged | C1, D1 (no diff to base/enhanced/refined files) |
| Reject events feed events.jsonl | D1 |
| Book WS subscription independent of strategy instantiation | C1 — runs whenever daemon runs |
| TUI primary panel = walked_vwap | E1 |
| Refined demoted to baselines | E1 |
| Web dashboard primary = walked_vwap | E2 |
| Both dashboards show reject count | E1, E2 |
| Both dashboards show reject reason breakdown (top 3) | E1, E2 |
| Both dashboards show most recent walked_edge | E1, E2 |
| Neutral `walked_vwap (candidate)` label | E1, E2 |
| Conventional commits, scope feat(strategy) / feat(daemon) / feat(dashboard) | All tasks |
| One commit per logical chunk | 12 commits across A1–E2 |
| Do NOT modify base/enhanced/refined/executor/scrape_book/manifest | Risk #1 |
| No new runtime deps | None added |

**Placeholder scan:** every step has concrete code or a concrete command. No "implement later", no "see Task N".

**Type / name consistency:**
- `MarketBooks(yes, no)` — defined in A1, used in B1–B5 + D1 + E1 + E2 reading from state
- `WALKED_VWAP_REJECT` action type — emitted in B4, branched on in D1
- Reject reasons (`stale_market_price`, `no_book_subscription`, `empty_book`, `insufficient_top_of_book`, `partial_fill_disallowed`, `insufficient_walked_edge`) — strings agree across B4 and D1 dashboards (E1, E2)
- `walked_*` field names on DaemonState (D1) match the keys read by the dashboards (E1, E2)
- `walked_vwap` strategy string — matches across daemon events, EventsTailer, dashboard renders

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-27-walked-vwap-strategy.md`. Two execution options:

**1. Subagent-Driven (recommended)** — main thread dispatches a fresh subagent per task with the task's full text + cross-references. Two-stage review per task: subagent ships a candidate diff; main thread reviews against acceptance criteria and either commits or dispatches a fix subagent. Tasks A1 → E2 are mostly sequential; E1 and E2 can run in parallel after D1 lands.

**2. Inline Execution** — main thread executes tasks in this session using `superpowers:executing-plans` with checkpoints between phases (A, B, C, D, E).

Recommend Subagent-Driven given the surface area (12 commits across 5 phases) and the user's "use subagents for implementation tasks" directive. Wait for user approval of this plan first.
