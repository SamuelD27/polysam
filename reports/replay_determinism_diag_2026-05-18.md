# Replay "non-determinism" diagnostic — 2026-05-12T08-54-33Z

Read-only-with-prints investigation. No production code edited, no commits.
Session: `daemon_state/scrapes/2026-05-12T08-54-33Z` (capture-verifier: PASS).
Code state: HEAD `7e4e357`, Sam-Dev. No commit since 2026-05-12 touches the
replay / strategy / execution / pricing path (verified via `git log --since`).

## Plain-language summary

This investigates why a walked_vwap replay of the 2026-05-12 session appeared to
flip between profit and loss depending on configuration. The apparent
inconsistency was not real: the replay is fully deterministic (6/6 identical
runs), and the "profitable" number was a stale on-disk summary from an earlier
run under different conditions. The genuine bug is that Sam-Dev's replay engine
has no market-price source for modern captures — it only reads price from
`events.jsonl` rows that post-H2 daemons no longer write, so it silently feeds
every strategy a constant 0.5 and every trade decays to a loss. The complete
fix already exists, tested, on an unmerged branch (`feat/daemon-market-price-tape`);
the recommendation is to merge that branch, not write new code. Until then no
Sam-Dev replay ROI number can be trusted, so the Phase-5 sweep must wait.

## TL;DR — which hypothesis fired

**The premise is false.** walked_vwap solo vs walked_vwap+{refined,enhanced,base}
do **not** differ. Both produce `2×SL / -0.91` deterministically (6/6 runs). The
"+0.87 / 2×TP" figure was a **stale `replay_summary.json` artefact** produced under
a *different market-price source*, not an observer-interaction outcome.

**Root cause (fires): `ReplayDataProvider` on Sam-Dev has no market-price
source for post-H2 captures.** `_index_markets_and_rtds` indexes RTDS price
**only** from `events.jsonl` rows of type `rtds_market_price` /
`market_price_update`. This session's window contains **0** such rows (the
daemon writes price to the per-session `market_price.jsonl` tape instead — an
H2 feature that lives on the unmerged `feat/daemon-market-price-tape` branch,
which is the branch this capture was made on per its manifest). So
`rtds_by_slug` indexes empty → `_market_price_at` returns `(None, None)` →
`orchestrator.py:252` substitutes the literal `0.5` for **every tick**. Every
strategy's realizable-price exit gate sees a flat 0.5 for the whole session;
TP can never trigger; positions ride to SL / RESOLUTION. This independently
reproduces and confirms the prior `reports/r4_market_price_diagnostic.md`
(written 2026-05-11 against the R4 session) for the 2026-05-12 session.

This required a code change to fix, so per the constraint it is **proposed, not
applied** (see §Fix).

---

## Phase A — cheap hypotheses, all FALSIFIED

### A1. LatencyModel / RNG — FALSIFIED (static)
`polyhustle/cli.py:_build_trader` constructs `PaperTrader()` with no args in
every mode (paper/replay → `PaperTrader()`, lines 118-122). `PaperTrader.__init__`
defaults `latency_model = ZeroLatency()` (paper_trader.py:114). `ZeroLatency`
returns a constant `0.0`, no RNG (latency.py:84-92). Only `GaussianLatency` /
`EmpiricalLatency` hold a `random.Random`, and neither is constructed by the CLI.
No `LATENCY_MULTIPLIER` in the configs. For walked_vwap specifically the trader
is a **pass-through** anyway (`_is_pre_walked` true — the gate emits
`effective_VWAP`), so `latency_ms` is only stamped on `fill_details`, never
influences the fill. Each assignment gets its own `PaperTrader`/`PaperExecutor`
— no shared executor. **No RNG anywhere on the path** (`grep` for
`random.|np.random|.seed(` across strategies/pricing/execution → 0 hits).

### A2. Book mutation — FALSIFIED (static)
`walk_for_vwap` (live_book_state.py:29-60) is pure: it iterates `levels`
read-only into local accumulators; it never mutates the list. `apply_snapshot`
/ `apply_delta` / `_apply_one` **reassign** `self.bids/asks` to new lists and
run only at precompute time inside `_fold_into_market_books`, not at tick
dispatch. `ReplayDataProvider._books_at` returns the precomputed
`records[i].books` (a frozen per-record `LiveBookState`). No strategy mutates
the book in place (`grep` for `.pop/.sort/.remove/.append/del` on `asks|bids`
in all four strategies → 0 hits). walked_vwap's book access is read-only
(`walk_for_vwap`, `book.asks[0][0]`, `top_size`, `has_baseline`). Also moot:
walked_vwap is assignment index 0 — it runs *before* every observer each tick.

### A3. Sigma — FALSIFIED (static)
`_compute_sigma_tape` (replay.py:384-407) precomputes one `_sigma_tape` from
the BTC tape via `EWMAVariance` once at `load()`. `_sigma_at(now)` is a pure
bisect lookup (replay.py:409-422). It is assignment-independent; no strategy's
`on_tick` can change what sigma another assignment receives.

### A4. Position divergence — N/A (premise falsified before reaching it)
Made moot by the determinism battery below: there is no solo-vs-observers
divergence to localise.

---

## Phase B — what is actually true

### B5. Stability / determinism battery (DECISIVE)
Each config run 3× back-to-back, `replay_summary.json` captured per run,
`PORTFOLIO_SIZE_USDC=10`:

```
cfg=sd_state_replay.json (solo, benchmarks [])              run 1/2/3  -> 2  -0.91  win 0.00  {'SL': 2}
cfg=h2_smoke_replay.json (walked_vwap + refined/enh/base)   run 1/2/3  -> 2  -0.91  win 0.00  {'SL': 2}
```

Both configs are **internally deterministic and mutually identical**. The
"observer interaction" / "non-determinism" framing is refuted: it was an
artefact of comparing a fresh Sam-Dev run against the on-disk
`replay_summary.json` left over from 2026-05-13 16:01.

### Size sweep (rules out trade-size as the flip cause)
Same session, solo walked_vwap, varying effective trade size via
`PORTFOLIO_SIZE_USDC` / `MAX_TRADE_SIZE_USDC` (`compute_effective_max_risk` =
`PORTFOLIO_SIZE_USDC*MAX_BET_PCT(0.20)` min abs cap):

```
sz_A  no portfolio, cap 5.0      -> 2  -2.26  {'SL': 2}
sz_B  portfolio 10  -> eff 2.0   -> 2  -0.91  {'SL': 2}
sz_C  portfolio 100 -> eff 5.0   -> 2  -2.26  {'SL': 2}
sz_D  portfolio 100 cap25 ->eff20-> 1  -1.69  {'SL': 1}
```

SL at **every** size. Size scales magnitude, never restores TP.

### B6. Live-dryrun (ground truth) vs Sam-Dev replay — the real discrepancy

Captured `events.jsonl` walked_vwap (session window) — real RTDS feed live:

```
[ENTRY] 1778577149.704 btc-updown-5m-1778577000 Down  mid=0.3125 walkedVWAP=0.3239 sh=2.06 usdc=0.67
[EXIT ] 1778577165.719  exit_type=TP entry=0.3239 exit=0.49   pnl=+0.32 pnl_mid=+0.35  hold=16s
[ENTRY] 1778577480.572 btc-updown-5m-1778577300 Down  mid=0.27   walkedVWAP=0.2836 sh=8.46 usdc=2.40
[EXIT ] 1778577484.575  exit_type=TP entry=0.2836 exit=0.4288 pnl=+1.14 pnl_mid=+1.26  hold=4s
                                                            TOTAL  pnl=+1.46  (2×TP)
```

Sam-Dev replay of the same two entries: `2×SL / -0.91`.

Why: events.jsonl in `[launch_ts_ns, stop_ts_ns]` contains
**0** `rtds_market_price`/`market_price_update` rows (type histogram:
`entry_signal 23, shadow_entry 17, shadow_exit 10, market_rollover 9,
shadow_resolve 7, entry_rejected 4, ... rtds_market_price 0`). The real price
series lives in the **ignored** `market_price.jsonl` tape:

```
market_price.jsonl btc-updown-5m-1778577000: 2672 recs  0.51 -> ... -> 0.01
market_price.jsonl btc-updown-5m-1778577300: 3411 recs  0.52 -> ... -> 0.0298
```

Simulating Sam-Dev's loader (events.jsonl-only) for the two entry ticks:

```
btc-updown-5m-1778577000 @1778577149.704 : rtds_by_slug empty -> _market_price_at None -> orchestrator market_price_up = 0.5
btc-updown-5m-1778577300 @1778577480.572 : rtds_by_slug empty -> _market_price_at None -> orchestrator market_price_up = 0.5
```

For these Down/NO positions the real tape collapses toward 0.01 (the winning
direction → real TP at 0.49 / 0.4288). Pinned at a flat 0.5, the
realizable-price TP gate never sees the favourable move, so both ride to SL.
The drift is **100% market-price starvation**, not fill-realism tax: the
captured fills (`fill_vwap` 0.3239 / 0.2836, `class=full`) match what the
strategy gate computed; the entry side is faithful. Only the exit signal is
broken in replay.

### Whole-replay corroboration (every strategy degenerates)

```
                NOW (Sam-Dev, market_price_up=0.5)     PRIOR replay_summary.json (2026-05-13 16:01)
refined      n= 8 pnl= -0.25 {'TP': 8}              refined     n=6 pnl= +6.85 {'SL':3,'TP':3}
enhanced     n= 8 pnl= -0.12 {'TP':7,'RES':1}       enhanced    n=6 pnl=+23.41 {'TP':3,'RES':2,'SL':1}
base         n= 7 pnl= -1.22 {'RES':7}              base        n=5 pnl=+14.33 {'RES':5}
walked_vwap  n= 2 pnl= -0.91 {'SL': 2}              walked_vwap n=2 pnl= +0.87 {'TP':2}
```

Different trade *counts* and exit-type *distributions* across **all four**
strategies — the signature of a global input (the realizable-price series)
collapsing, not a per-strategy or observer-coupling bug. The PRIOR artefact's
TP-heavy, profitable shape is only reachable when a real market-price series is
fed (i.e., it was generated on / with the H2 market-price tape consumed). The
RES-heavy `base` row (8 RES vs 5 RES) further shows position lifetimes change
because exits never fire early.

---

## Fix — PROPOSED, not applied (constraint: code change → stop)

No new patch is needed. The complete fix already exists, fully tested, on the
**unmerged** branch `feat/daemon-market-price-tape` (tip `711aa8a`, the branch
this very capture was recorded on — which is why `market_price.jsonl` exists in
the session dir but Sam-Dev cannot read it):

```
78f98ba feat(daemon): write market_price.jsonl per session at RTDS handler
e9f9603 feat(replay): load market_price.jsonl as priority-1 RTDS source
dc9df9d test(daemon): market_price.jsonl emission + rollover-grace filter
83e339c test(replay): market_price.jsonl priority + per-slug lookup
711aa8a docs(scraper): H2 market_price tape; capture verification updated
```

`git diff --stat Sam-Dev feat/daemon-market-price-tape`:
`daemon_base_v1.py +263/-96`, `polyhustle/data/replay.py +89`. On that branch
`replay.py` gains `_load_market_price()` (called from `load()`), which seeds
`rtds_by_slug` from `<session_dir>/market_price.jsonl` as priority-1 RTDS
source, falling back to `_index_markets_and_rtds` (the current events.jsonl
path) for legacy captures.

**Recommended next step:** merge `feat/daemon-market-price-tape` into Sam-Dev
(it is 3 commits ahead / 61 behind — needs a rebase onto Sam-Dev first), then
re-run the determinism battery and the live-dryrun↔replay attribution above.
Expected post-merge: walked_vwap replay of 2026-05-12 reproduces `2×TP` and
tracks the captured `+1.46` modulo the genuine fill-realism tax (which §B6
shows is *not* the cause here). Do **not** sweep Phase-5 until this lands —
every current Sam-Dev replay ROI number is measuring the 0.5-fallback artefact.

## Falsified vs fired — summary

| Hypothesis | Verdict | Basis |
|---|---|---|
| A1 LatencyModel / shared RNG | FALSIFIED | ZeroLatency default, no RNG on path, walked_vwap is pass-through |
| A2 Book mutation (cross-assignment) | FALSIFIED | walk_for_vwap pure; no in-place book ops; precomputed snapshots; walked_vwap runs first |
| A3 Sigma order-sensitivity | FALSIFIED | precomputed assignment-independent tape |
| A4 Position divergence | N/A | premise falsified by B5 |
| B5 Harness non-determinism | FALSIFIED | 6/6 runs identical; configs mutually identical |
| Trade size flips TP/SL | FALSIFIED | SL at every size in sweep |
| **Replay has no market-price source post-H2** | **FIRED** | 0 RTDS rows in events.jsonl; rtds_by_slug empty; orchestrator 0.5 fallback; all 4 strategies degenerate; matches reports/r4_market_price_diagnostic.md |
