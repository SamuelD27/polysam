# BTC 5m Up/Down — Strategy Lineage and Current Mechanics

This doc traces the strategy classes that ship in `active_bots/`, how each
one extends the previous, what the experimental fork tree taught us, and
exactly how the current production strategy (`WalkedVWAPStrategy` with
the walked-bid VWAP exit gate) decides whether to enter and exit a trade.

The instrument is Polymarket's 5-minute BTC up/down binary, settled on
whether the BTC spot is above or below the strike at T+300s. Each market
re-strikes every 5 minutes. All math is on the YES leg (`market_price_up`);
DOWN positions get reflected via the standard binary identity
`P(NO) = 1 - P(YES)`.

---

## 1. Strategy class lineage

The strategies form a single inheritance chain. Each class adds one
orthogonal piece of behaviour to the parent.

```
BaseStrategy
   └── TimeBasedStrategy            (compose, not inherit)
EnhancedStrategy
   = TimeBasedStrategy + ProfitGrabber + SqueezeDetector
   └── RefinedStrategy              (champion-tuned EnhancedStrategy)
        └── WalkedVWAPStrategy      (RefinedStrategy + book-aware gates)
              └── WalkedExitProfitGrabber
                  (the new walked-bid exit gate)
```

The `WalkedVWAPStrategy` wrapper swaps the parent's `ProfitGrabber`
attribute for a `WalkedExitProfitGrabber` subclass, so the wrapper itself
inherits all RefinedStrategy logic AND replaces the exit-pricing brain.

### 1.1 `BaseStrategy` — `active_bots/base_strategy.py`

Pure fair-price edge model.

- Compute fair price using GBM:
  `compute_fair_price(spot, strike, sigma, time_remaining)` returns
  `Phi(d2)` where `d2 = ln(S/K) / (sigma * sqrt(tau))` (no drift assumption).
- Compute edge: `edge = abs(fair - market_price)`.
- Side: `Up` if `fair > market`, else `Down`.
- Position size: linear interpolation between `EDGE_MIN=0.10` and
  `EDGE_MAX=0.25`, scaled to `MAX_RISK=100.0` USDC.
- Single fixed entry window: T+ENTRY_OFFSET to T+ENTRY_CUTOFF (defaults
  120s/150s).
- Hold to resolution; PnL = `(1 if won else 0) - entry_price` per share.

The base strategy never closes early. There is no TP/SL. Slippage is
flat-modelled via `SPREAD_COST = 0.01` USDC/share.

### 1.2 `TimeBasedStrategy` — composed inside `EnhancedStrategy`

Replaces the single hard entry window with four time-zoned windows, each
with its own minimum edge:

| Zone        | T-window  | edge_min | Rationale                                |
|-------------|-----------|----------|------------------------------------------|
| `early`     | 60–120s   | 0.20     | Vol still high, demand bigger edge       |
| `mid`       | 120–210s  | 0.12     | Working zone                             |
| `sweet_spot`| 210–260s  | 0.08     | Highest fair-price confidence            |
| `late_gamma`| 260–285s  | 0.15     | Resolution sensitivity spikes; back off  |

Plus two hard floors (`enhanced_strategy.py:42-43`):
- `MIN_ENTRY_PRICE = 0.05` — fills below 5c are unrealistically thin
- `MAX_ENTRY_PRICE = 0.95` — symmetric ceiling

### 1.3 `EnhancedStrategy` — `active_bots/enhanced_strategy.py`

`TimeBasedStrategy` + two new orthogonal pieces:

**Enhancement 2 — `ProfitGrabber`** (active TP/SL instead of hold-to-expiry):
- Per tick, on an open position, compute `realizable = market_price_up if
  side=="Up" else 1 - market_price_up` (`enhanced_strategy.py:290`).
- Compare `delta_favor = realizable - entry_price` against `tp_delta`,
  `delta_against = entry_price - realizable` against `sl_delta`.
- TP fires if `delta_favor >= tp_absolute_favor` (a hard floor — default
  0.10) OR `delta_favor >= adaptive_tp(edge, time_remaining)`.
- SL fires if `delta_against >= adaptive_sl(edge)`.
- Force-exit window: in the last `FORCE_EXIT_BEFORE_S=30s`, take whatever
  the last quoted price gives — TP if `delta_favor >= 0` else SL.
- Staleness gate: outside the force-window, refuse to exit unless the
  last quote is fresher than 10 seconds (`enhanced_strategy.py:274-288`).

Adaptive thresholds:
```
adaptive_tp(edge, time_remaining):
  floor = TP_DELTA_MIN  (default 0.05)
  target = max(edge, floor)
  frac = clamp(time_remaining / MARKET_DURATION, 0, 1)
  return max(floor, floor + (target - floor) * frac)

adaptive_sl(edge):
  return clamp(0.5 * edge + 0.05, SL_DELTA_MIN=0.05, SL_DELTA_MAX=0.20)
```

The TP threshold starts at the entry edge and decays linearly toward
`TP_DELTA_MIN` over the cycle. The SL grows with conviction so a high-edge
trade isn't shaken out by noise.

**Enhancement 3 — `SqueezeDetector`** (early-spike mean reversion):
- Detection window T+0 to T+60s: track the max `|ln(S/K)|` and direction.
- If the spike score exceeds `SPIKE_THRESHOLD=2.0`, mark as candidate.
- Entry window T+60s to T+150s: wait for a reversion of at least
  `SQUEEZE_REVERSION_FRAC=0.30` of the spike, then enter counter-trend.
- This is an *independent* signal — squeeze entries can fire even when
  the time-zoned edge entry would not.

### 1.4 `RefinedStrategy` — `active_bots/refined_strategy.py`

`EnhancedStrategy` pre-configured with the session-champion parameters:
- `enable_squeeze=False` — squeeze detector disabled.
- `DEFAULT_TP_DELTA_MIN = 0.08` (vs default 0.05) — wait for a bigger
  edge to give up a high-conviction hold.
- `DEFAULT_TP_ABSOLUTE_FAVOR = 0.15` (vs default 0.10) — fire absolute-TP
  later (= hold longer for bigger gains).
- All other knobs inherit the parent defaults.

This is the post-tuning configuration. Why these specific numbers landed
is in §2.

### 1.5 `WalkedVWAPStrategy` — `active_bots/walked_vwap_strategy.py`

`RefinedStrategy` + book-aware **entry** gate (the original commit) +
book-aware **exit** gate (the e1959c4 commit added in this codebase
review). Two independent additions, both layered on top of Refined's
unchanged TP/SL math.

Entry gate (`walked_vwap_strategy.py:_gate`, called from `on_tick`):
1. Stale-quote reject: refuse if `now - market_price_ts > 30s`.
2. No-book reject: refuse if no WS book subscription.
3. Empty-book reject: refuse if the relevant ask side is empty.
4. Top-of-book reject: refuse if top size < requested shares (i.e., we'd
   walk past level 0). Default `MIN_TOP_OF_BOOK_SHARES_RATIO=1.0`.
5. Walked-VWAP economics:
   - Walk the asks for `requested_shares`, get `walked_VWAP`.
   - Add per-share bell-curve taker fee → `effective_VWAP`.
   - Compute `walked_edge = fair - effective_VWAP`. Reject if
     `walked_edge < WALKED_VWAP_EDGE_MIN` (default 0.02).
6. On pass: rewrite `action["entry_price"]` from the parent's mid quote
   to the `effective_VWAP`. Preserve the original mid in
   `action["entry_price_mid"]` for diagnostics. Sync the parent's
   `_open_position` so subsequent TP/SL math operates on the realistic
   per-share cost, not the mid.

Exit gate (`WalkedExitProfitGrabber`, the new piece):
- See §4 for the full mechanics. In short: `WalkedVWAPStrategy.__init__`
  swaps `self.profit_grabber` with the subclass; the subclass's
  `check_exit` re-prices the realizable exit on the post-fee walked-bid
  VWAP for the held side, falling back to mid (or holding) when the book
  is bad. Default-off via `WALKED_VWAP_EXIT_ENABLE=0` so the exit path is
  bit-for-bit identical to RefinedStrategy until enabled.

---

## 2. How we got to the current configuration

The parameter space is: time-zone edge thresholds, TP delta floor, TP
absolute favor, SL band, squeeze on/off, plus the entry-window gates.
We swept this space by checking out parameter variants under
`experiments/` and replaying historical book/feed data.

### 2.1 Experiment fork tree

The `experiments/` directory has two namespaces:

`r*_*` — **R**efined-tuning forks. Each tweaks one knob from the
RefinedStrategy baseline:

| Fork                          | Change                                    |
|-------------------------------|-------------------------------------------|
| `r2_no_squeeze`               | Squeeze detector disabled                 |
| `r2_tp_tight`                 | TP_DELTA_MIN raised to 0.08              |
| `r2_tp_loose`                 | TP_DELTA_MIN lowered                     |
| `r2_sl_wide`                  | SL_DELTA_MAX widened                     |
| `r2_mid_aggressive`           | Mid-zone edge_min lowered                |
| `r2_sweet_only`               | Only `sweet_spot` time zone armed        |
| `r3_combined_plus`            | r2 winners stacked                       |
| `r3_sl_wide_no_squeeze`       | SL wide + squeeze off                    |
| `r3_tp_tighter`               | Tighter TP than r2_tp_tight              |
| `r4_all_winners`              | Final stack candidate                    |
| `r4_tp_tight_no_squeeze`      | The eventual champion config             |
| `r5_aggressive_size`          | Position-size scaling experiment         |
| `r5_squeeze_low_thresh`       | SqueezeDetector with lower spike floor   |

`n*_*` — **N**ew-direction experiments (not necessarily descending from
Refined):

| Fork                  | Idea                                                |
|-----------------------|-----------------------------------------------------|
| `n1_mean_revert`      | Pure mean-reversion entry (no fair price)           |
| `n2_late_gamma`       | Add explicit gamma-aware entry near resolution      |
| `n3_momentum`         | Trend-following (ignore fair price)                 |
| `n4_market_maker`     | Bid-ask-posting maker (failed — see §6)            |
| `n5_no_squeeze`       | Same as r2_no_squeeze, in n-namespace               |
| `n6_vol_regime`       | Per-volatility-regime parameter switching           |

Each fork has its own `active_bots/` copy with the strategy class
modified, plus a `runs/` directory of replayed trades. The
`experiments/champion/` and `experiments/_baseline/` folders pin the
canonical reference for comparison.

### 2.2 Two findings that produced RefinedStrategy

The session captured in `docs/superpowers/plans/2026-04-22-strategy-fine-tuning.md`
and `memory/project_strategy_fine_tuning_session.md` settled the
following:

**Finding 1 — squeeze-off is the robust contributor.** Across all
configurations, disabling the squeeze detector produced a higher and
more stable Sharpe ratio than enabling it. The squeeze added a few big
wins but a comparable number of toxic-flow losses (entering on a
"reversion" that turned out to be a continuation). The signal is real
but too noisy at the available 5-minute granularity.

**Finding 2 — TP-tight beats TP-loose.** Raising `TP_DELTA_MIN` from
0.05 to 0.08 and `TP_ABSOLUTE_FAVOR` from 0.10 to 0.15 made the strategy
less greedy at the start of a cycle and more decisive at the end — fewer
trades giving up at break-even, more trades capturing real edge. Combined
with squeeze-off, this is the champion (`r4_tp_tight_no_squeeze`), which
is encoded directly into `RefinedStrategy`.

ROI improvements were within the noise band of a single replay session,
so the choice was robustness-driven (low variance on the parameter
sweep) more than mean-improvement-driven.

### 2.3 Why we then needed walked-VWAP

RefinedStrategy showed daemon-reported PnL that was systematically
rosier than the realized on-chain PnL. The diagnosis (validated by
reconciling daemon `exit_filled` events against the Polymarket history
CSV) was that the *mid quote* used as the realizable price isn't
realizable: the bid-ask spread + bell-curve fee + book thinness mean
the actual fill price walks several ticks worse than mid.

Two trades from a real live session demonstrated the gap:
- Trade R1: daemon reported -$0.64; on-chain realized -$1.78 (gap $1.14)
- Trade R2: daemon reported -$6.62; on-chain realized -$7.43 (gap $0.81)

The walked-VWAP entry gate (the original commit) closed half this gap
by replacing the parent's mid-quoted entry price with the actual taker
cost computed by walking the asks. That made entries truthful but exits
were still mid-quoted. The walked-bid VWAP exit gate added in this
codebase review closes the other half.

### 2.4 The role-gating fix

A separate fix that landed alongside (commit 25d50f0) prevents multiple
strategies from posting orders on the same wallet. Before this commit,
`enhanced`, `refined`, and `walked_vwap` all emitted real `exit_filled`
events to the live executor when running in observer mode. After the
commit, only the strategy with `role="trader"` posts orders;
observer-only strategies emit `shadow_*` events for benchmarking. This
is what made the previous session's $10.20 loss visible (refined was
posting alongside walked_vwap; refined's $8.07 loss dominated). The
current dry-run and live sessions confirm only `walked_vwap` posts now.

---

## 3. The current strategy in one tick

What happens when `daemon_base_v1.py` calls `walked.on_tick(...)` with
a fresh BTC price, mid quote, sigma, and book snapshot:

```
daemon_base_v1.py:1635
  walked.on_tick(btc_price, market_price_up, sigma, t_zero,
                 market_price_ts, books=books)
       │
       ▼
WalkedVWAPStrategy.on_tick (walked_vwap_strategy.py:62)
  1. Stash books on profit_grabber._latest_books (so the exit-gate
     subclass can read them later this same tick).
  2. Delegate to parent's on_tick via _run_parent_on_tick (test seam).
       │
       ▼
EnhancedStrategy.on_tick (enhanced_strategy.py:565)
  Priority order:

  STEP 1 — exit check on existing position
    if has_position:
      action = self.profit_grabber.check_exit(...)
        ↓ This is now WalkedExitProfitGrabber.check_exit (§4)
      if action: return action

  STEP 2 — resolution check
    if has_position and elapsed >= MARKET_DURATION:
      return resolve_trade(position, btc_price)

  STEP 3 — squeeze entry  (DISABLED in RefinedStrategy)
    skipped because enable_squeeze=False

  STEP 4 — time-based edge entry
    entry = time_strategy.on_tick(...)
    if entry["action"] == "ENTER":
      stash _open_position
      return entry              ← only path that exits this method
                                  with an ENTER action
       │ (only when STEP 4 fired)
       ▼
WalkedVWAPStrategy.on_tick (continued)
  3. If action.get("action") == "ENTER", run the entry gate (§3.1).
     Otherwise return the action unchanged (exit / resolve / None).
```

### 3.1 Entry gate in detail

The parent's `entry` action carries: side, edge, mid-quoted entry_price,
size_shares, size_usdc, fair, market. The gate either:

- Returns the action augmented with walked-VWAP diagnostics and an
  `effective_VWAP` price replacement, OR
- Returns a `WALKED_VWAP_REJECT` action with a reject_reason and clears
  the parent's phantom `_open_position`.

Reject branches in order:

| # | Reject reason             | Trigger                                                          |
|---|---------------------------|------------------------------------------------------------------|
| 1 | `stale_market_price`      | `now - market_price_ts > WALKED_VWAP_MARKET_STALENESS_S` (30s)  |
| 2 | `no_book_subscription`    | `books is None` or `book.has_baseline == False`                  |
| 3 | `empty_book`              | `book.asks` empty, OR walk produced NaN/no fill                  |
| 4 | `insufficient_top_of_book`| Top ask level smaller than requested shares                      |
| 5 | `partial_fill_disallowed` | Walk classification == "partial" and `WALKED_VWAP_PARTIAL_OK=0`  |
| 6 | `insufficient_walked_edge`| `fair - effective_VWAP < WALKED_VWAP_EDGE_MIN` (default 0.02)    |

On pass, the gate writes:
```
action["entry_price"]      = effective_VWAP   (was: mid quote)
action["entry_price_mid"]  = original mid quote
action["size_usdc"]        = size_shares * effective_VWAP
self._open_position["entry_price"] = effective_VWAP   (parent sync)
```

Subsequent TP/SL math then operates on the realistic per-share cost.

### 3.2 Why the gate matters at exit time too

Even though the entry gate already rewrote `entry_price` to the
walked-ask VWAP, the *exit* side of the PnL equation
`(exit_price - entry_price) * size_shares` was still using the parent's
mid-quoted `realizable`. So exits over-stated wins (reading the mid
when the bid-walk would actually deliver less) and under-stated SL
losses (claiming a tighter exit than the real fill). The new exit gate
closes that side.

---

## 4. The walked-bid exit gate (the new piece)

`WalkedExitProfitGrabber` lives in `active_bots/walked_vwap_strategy.py`
alongside `WalkedVWAPStrategy`. It subclasses `ProfitGrabber` and
overrides only `check_exit`.

### 4.1 Why a subclass and not an override on the wrapper

The exit decision is made by `EnhancedStrategy.on_tick:592` calling
`self.profit_grabber.check_exit(...)`. That call lands on the
`profit_grabber` *attribute*, not on the strategy class. Overriding
`check_exit` on `WalkedVWAPStrategy` would be dead code — nothing
dispatches there. Two options:

1. Edit `daemon_base_v1.py` to route exits through the wrapper. Crosses
   a protected file boundary near the orchestration loop.
2. Subclass `ProfitGrabber`, swap `self.profit_grabber` in
   `WalkedVWAPStrategy.__init__`, leave the daemon and enhanced_strategy
   untouched.

We chose option 2. The wrapper's `__init__` reads the parent's freshly
built `ProfitGrabber` config and reconstructs a `WalkedExitProfitGrabber`
with identical TP/SL/force-window knobs.

### 4.2 The wrapper-tick stash

`EnhancedStrategy.on_tick` calls `check_exit` without a `books` kwarg,
so the subclass cannot receive book data through the call signature.
The wrapper's existing `on_tick(..., *, books=None)` already runs every
tick. We added a one-liner: BEFORE delegating to the parent, stash
`self.profit_grabber._latest_books = books` (gated by `isinstance` so
test seams using a vanilla parent are unaffected).

The stash is unconditional — even when `books is None` — so stale book
data from a prior tick can't leak into the next exit decision.

### 4.3 `check_exit` flow

```
def check_exit(self, position, ..., market_price_up, market_price_ts):
    if not WALKED_VWAP_EXIT_ENABLE:
        return super().check_exit(...)        # bit-for-bit fallback

    in_force_window = time_remaining <= self.force_exit_before_s

    translated = self._compute_translated_market_price_up(
        position, market_price_up, in_force_window,
    )
    if translated is None:
        return None                            # hold, re-check next tick

    return super().check_exit(
        position, ..., market_price_up=translated, ...,
    )
```

The trick: instead of re-implementing TP/SL math, the override computes
the post-fee walked-bid VWAP, translates it back into the YES-frame the
parent expects, and lets the parent's `check_exit` do the rest. The
translation is provably correct because the parent's own math is

```
realizable = market_price_up if side == "Up" else 1 - market_price_up
```

so feeding `eff` for Up positions and `1 - eff` for Down positions makes
`realizable = eff` for both sides — the holder's true post-fee per-share
exit proceeds.

### 4.4 The bad-book matrix

`_compute_translated_market_price_up` decides what to feed the parent:

| Condition                                         | EXIT_ENABLE=0    | FALLBACK_MID=1 | FALLBACK_MID=0 |
|---------------------------------------------------|------------------|----------------|----------------|
| Healthy book, full fill                           | parent (mid)     | walked-bid VWAP| walked-bid VWAP|
| Stale book                                        | parent (mid)     | parent (mid)   | hold (None)    |
| Empty / NaN book                                  | parent (mid)     | parent (mid)   | hold (None)    |
| Partial fill, EXIT_PARTIAL_OK=0                   | parent (mid)     | parent (mid)   | hold (None)    |
| Partial fill, EXIT_PARTIAL_OK=1                   | parent (mid)     | walked-bid VWAP| walked-bid VWAP|
| Force-window + healthy book                       | parent (mid, fc) | walked-bid VWAP| walked-bid VWAP|
| Force-window + bad book                           | parent (mid, fc) | parent (mid, fc)| parent (mid, fc)|

"fc" = force-close: parent fires TP if `delta_favor >= 0` else SL,
unconditionally. Inside the force-window, `partial_ok` is forced True so
partial fills are accepted, and a genuinely empty book always falls back
to mid so the parent's force-close still fires (never orphans a residual
position pre-resolution).

Outside the force-window with `EXIT_FALLBACK_MID=0` and a bad book, the
override returns `None` — the parent's on_tick falls through to the
resolution check, returns None, and the daemon idles for one tick. Next
tick re-runs the gate against fresh book data.

### 4.5 Fee direction

The entry-side helper `_effective_vwap_after_fees` ADDS the per-share
bell-curve fee (taker pays more on a buy). The exit-side helper
`_effective_exit_after_fees` SUBTRACTS the per-share bell-curve fee
(taker receives less on a sell out of proceeds). Both reuse
`fee_usdc(p, shares, category)` from `execution/fees.py` — the bell-curve
formula `(180 bps / 10000) * p * (1-p) * shares * category_coefficient`,
peaks at 45 bps when p = 0.5, zero at p = 0 or 1.

### 4.6 Knobs

| Env var                              | Default | Meaning                                                |
|--------------------------------------|---------|--------------------------------------------------------|
| `WALKED_VWAP_EXIT_ENABLE`            | `0`     | Master switch. Off = bit-for-bit RefinedStrategy.     |
| `WALKED_VWAP_EXIT_STALENESS_S`       | `30`    | Book age threshold; >threshold triggers fallback path. |
| `WALKED_VWAP_EXIT_PARTIAL_OK`        | `0`     | Outside force-window, accept partial fills            |
| `WALKED_VWAP_EXIT_FALLBACK_MID`      | `1`     | On bad book, 1 = parent (mid), 0 = hold and re-check  |

Defaults preserve today's behaviour exactly when the master switch is
off. Rollout posture is paper-validate first, then flip per-deployment.

### 4.7 Liquidity-gap logging

Whenever the gate falls back or holds, it emits an INFO log line:

```
walked_vwap_exit_liquidity_gap {'reason': '<reason>', 'side': '<Up|Down>'}
```

reasons: `empty_book`, `stale_book`, `partial_fill_disallowed`. These
are the operational signal that the bid book was too thin for the
walked-VWAP exit to be reliable on that tick. The dry-run that validated
this gate fired 16 of these in 10.9 hours, all `empty_book`.

---

## 5. Cheat-sheet: which knobs live where

| Knob                            | File                                  | Default | Meaning                              |
|---------------------------------|---------------------------------------|---------|--------------------------------------|
| `EDGE_MIN`                      | base_strategy.py:14                   | 0.10    | Position-size lower fraction edge    |
| `EDGE_MAX`                      | base_strategy.py:15                   | 0.25    | Position-size upper fraction edge    |
| `MAX_RISK`                      | base_strategy.py:16                   | 100.0   | Max USDC notional at full size       |
| `MARKET_DURATION`               | base_strategy.py:18                   | 300     | Seconds per market cycle             |
| `MIN_ENTRY_PRICE`               | enhanced_strategy.py:42               | 0.05    | Refuse entries below this price      |
| `MAX_ENTRY_PRICE`               | enhanced_strategy.py:43               | 0.95    | Refuse entries above this price      |
| `TP_DELTA_MIN`                  | enhanced_strategy.py:174 / env        | 0.05    | TP threshold floor (Refined: 0.08)   |
| `TP_ABSOLUTE_FAVOR`             | enhanced_strategy.py:187 / env        | 0.10    | Absolute TP fire (Refined: 0.15)     |
| `SL_DELTA_MIN` / `SL_DELTA_MAX` | enhanced_strategy.py:177-178 / env    | 0.05/.20| Adaptive SL band                     |
| `FORCE_EXIT_BEFORE_S`           | enhanced_strategy.py:182 / env        | 30.0    | Force-window into resolution         |
| `WALKED_VWAP_MARKET_STALENESS_S`| walked_vwap_strategy.py:34 / env      | 30.0    | Entry-gate stale-quote threshold     |
| `WALKED_VWAP_MIN_TOP_RATIO`     | walked_vwap_strategy.py:37 / env      | 1.0     | Top size must >= requested * ratio   |
| `WALKED_VWAP_EDGE_MIN`          | walked_vwap_strategy.py:39 / env      | 0.02    | Min walked-edge after fee            |
| `WALKED_VWAP_PARTIAL_OK`        | walked_vwap_strategy.py:40 / env      | 0       | Accept partial entry fills           |
| `WALKED_VWAP_FEE_CATEGORY`      | walked_vwap_strategy.py:43 / env      | crypto  | Fee bucket (crypto/finance/geopol)   |
| `WALKED_VWAP_EXIT_ENABLE`       | walked_vwap_strategy.py:48 / env      | 0       | Exit gate master switch              |
| `WALKED_VWAP_EXIT_STALENESS_S`  | walked_vwap_strategy.py:51 / env      | 30      | Exit-gate stale-book threshold       |
| `WALKED_VWAP_EXIT_PARTIAL_OK`   | walked_vwap_strategy.py:52 / env      | 0       | Accept partial exit fills            |
| `WALKED_VWAP_EXIT_FALLBACK_MID` | walked_vwap_strategy.py:55 / env      | 1       | Bad-book fallback policy             |
| `SL_ABSOLUTE_AGAINST`           | enhanced_strategy.py:198 / env        | unset   | Hard SL cap, mirror of `TP_ABSOLUTE_FAVOR`. Default unset = off. |
| `SL_DECAY_ENABLE`               | enhanced_strategy.py:204 / env        | 0       | Time-decay on adaptive SL, mirror of `adaptive_tp` decay. Default off. |
| `SL_DELTA_DECAY_FLOOR`          | enhanced_strategy.py:205 / env        | 0.05    | Floor of the SL decay curve (only consulted when `SL_DECAY_ENABLE=1`). |
| `WALKED_VWAP_SIZE_ON_WALKED_EDGE` | walked_vwap_strategy.py:60 / env    | 0       | Recompute `size_shares` from the walked edge instead of mid edge. |

---

## 6. What the current strategy does NOT do

Acknowledged gaps, not bugs. Each is a deliberate non-goal of the
current shape.

- **Limit (GTC) orders.** All entries and exits use FAK (fill-and-kill,
  market-style). The bot crosses the spread on every fill. Maker rebates
  / spread savings are unrealized. The `n4_market_maker` experiment
  attempted limit-order posting; abandoned because 5-minute markets
  don't give limit orders enough queue time to fill reliably.

- **Symmetric TP/SL.** TP has both an adaptive *and* an absolute floor
  (`tp_absolute_favor=0.15`); SL historically had only the adaptive
  curve. The May 2026 cleanup pass added two flag-gated knobs that
  remove the asymmetry without changing default behaviour:
  - `SL_ABSOLUTE_AGAINST` (default unset = off) — hard SL cap mirror.
  - `SL_DECAY_ENABLE` (default 0 = off) — time-decay on adaptive SL,
    mirror of `adaptive_tp` decay; floor controlled by
    `SL_DELTA_DECAY_FLOOR`.
  Both are paper-validate-then-flip per deployment.

- **Position-size scaling on walked edge.** Sizing still defaults to
  the pre-walk mid edge. The May 2026 cleanup pass added
  `WALKED_VWAP_SIZE_ON_WALKED_EDGE` (default 0 = off) — when set, the
  gate recomputes `size_shares` against `walked_edge` after the gate
  passes, so a thin-book market sizes down to match the actual
  realizable edge per share.

- **Multi-position / pyramiding.** Single position per market; no
  scale-in or scale-out. The `_open_position` field is a single dict.

- **Cross-market hedging.** No correlation logic between markets.

- **Limit-order escalation on FAK reject.** When the entry-side FAK
  no-matches (book moved between gate snapshot and order arrival), the
  strategy gives up on that market. No retry, no limit fallback.

- **Snapshot-vs-fill execution drag.** Even with the walked-bid exit
  gate, the daemon-reported PnL is the *gate-time* walked VWAP, not the
  actual fill price. Book churn between the gate decision and the FAK
  arrival on the CLOB can move the fill 1-2c. The first live session
  with the gate enabled showed an average ~$0.25/trade residual gap
  between daemon PnL and on-chain PnL. Not fixable inside this strategy
  file; would require either pre-cross pricing or multi-snapshot
  smoothing.

---

## 7. Tags

Repository tags pin the relevant snapshots:

- `pre-walked-bid-exit-gate` → 25d50f0 — RefinedStrategy + walked-VWAP
  entry gate + role-gating fix; exit side still mid-quoted. Use for
  rollback if the new exit gate misbehaves.
- `walked-bid-exit-gate-v1` → e1959c4 — current. Walked-bid VWAP exit
  gate added, default-off. Flip `WALKED_VWAP_EXIT_ENABLE=1` per-env to
  activate.

Rollback to legacy: `git checkout pre-walked-bid-exit-gate`.
