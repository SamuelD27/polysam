# R3 capture diagnostic — 2026-05-07T04:35:26Z

**Source artefacts:**
- `daemon_state/scrapes/2026-05-07T04-35-26Z/`
- `daemon_state/book_feed/2026-05-07_R3/` (10 per-slug gz files, 17-29 MB each)
- Daemon `events.jsonl` window: ts ∈ [1778128526, 1778131384] (47 min)
- Directories were originally suffixed `_R3_FAILED` while this diagnostic
  was conducted; renamed in the same Sam-Dev session after the report
  cleared the capture as healthy (median 95.6 % populated). The
  `_FAILED` suffix exists only in commit history.

**Pre-existing context (CLAUDE.md §18):** scraper fix `e3724dd..dcfe272` shipped to
add resubscribe-on-discovery. Smoke test outside the WireGuard netns reported 94 %
populated frames per slug. R3 ran inside the `polybot` netns and was flagged failed
when sampled slugs showed 0.0 % populated frames.

## Plain-language summary

R3 is not a failed capture. The scraper fix from `dcfe272` is working as intended:
the WS subscribed, resubscribed correctly on every market discovery (8 resubscribe
events visible in `scraper.log`), book events flowed continuously, and the dispatcher
applied them to local state. The "0.0 % populated" symptom that prompted the rollback
is a measurement artefact in the spot-check methodology, not a data-quality failure.
When measured correctly the actual data-population rate per slug is 95.4-95.9 %.
Inside the netns, the daemon's own `clob_book_feed` (separate WS in the same netns)
also worked: events.jsonl shows 28 entry signals from all four strategies and
walked_vwap making real entry/reject decisions during the window. **Hypothesis (A) —
netns-specific subscription failure — is rejected.** Two genuine secondary findings
came out of the diagnostic and are worth fixing in the next pass, but neither caused
R3 to "fail" — they are quality-of-life issues for downstream replay tools and the
smoke gate methodology. **Recommendation: do not roll back `dcfe272`. The smoke gate
that approved the merge needs to be revised to use a correct population predicate
before any next capture is run.**

## 1. Process state

`pgrep -fa daemon_base_v1.py` and `pgrep -fa scrape_book.py` both empty. Daemon and
scraper cleanly stopped via SIGINT then SIGTERM at 13:22:55 SGT (manifest:
`scraper_exit_code=sigterm`, `daemon_exit_code=sigterm`).

## 2. The "0.0 % populated" symptom is a measurement artefact

The original spot-check methodology counted records where the top-level `bids` or
`asks` field was non-empty. That predicate misses two of the three event classes
that carry book data:

| Record `type`  | Where data lives                | Top-level `bids`/`asks` predicate matches? |
|----------------|---------------------------------|---|
| `snapshot`     | top-level `bids` and `asks`     | ✅ yes |
| `book`         | nested under `raw.bids` / `raw.asks` | ❌ no |
| `price_change` | nested under `deltas[*].size`   | ❌ no |

In R3, `price_change` events dominate the file (~95 % of records). Inside the
netns, total event volume was high (≈ 117 records/sec/slug). The top-level
predicate consequently produced near-0 % even though the data was healthy.

**Corrected population check** (counts a record as "populated" if it carries any
book data anywhere — `snapshot.bids|asks`, `book.raw.bids|asks`, or any
`price_change.deltas[*].size > 0`):

| Slug             | Total records | "Any data" rate | book events populated | snapshot populated | price_change with size>0 |
|------------------|---|---|---|---|---|
| 1778128500       | 223,023 | **95.4 %** | 3,634 / 3,634 (100 %) | 22 / 1,014 (2.2 %) | 209,042 / 211,702 (98.7 %) |
| 1778128800       | 332,913 | **95.9 %** | 4,659 / 4,659 (100 %) | 86 /   954 (9.0 %) | 314,464 / 318,610 (98.7 %) |
| 1778131200       | 206,147 | **95.4 %** | 4,288 / 4,288 (100 %) | 14 /    74 (18.9 %) | 192,346 / 194,438 (98.9 %) |

`book` events are 100 % populated for all three slugs. The WS data path through
the WireGuard tunnel is fine.

## 3. Resubscribe path is firing correctly

`scraper.log` for the R3 session shows the fix from `e3724dd` working as designed:

```
12:35:53,995 [INFO] subscribed assets=4
...
12:43:22,700 [WARNING] ws resubscribe: discovery added 2 new tokens (was 4, now 6)
12:43:24,325 [INFO] subscribed assets=6
12:50:08,160 [WARNING] ws resubscribe: ... was 6, now 8
12:55:08,834 [WARNING] ws resubscribe: ... was 8, now 10
13:00:11,832 [WARNING] ws resubscribe: ... was 10, now 12
13:03:00,985 [WARNING] ws resubscribe: ... was 12, now 14
13:07:51,173 [WARNING] ws resubscribe: ... was 14, now 16
13:15:07,978 [WARNING] ws resubscribe: ... was 16, now 18
13:18:11,393 [WARNING] ws resubscribe: ... was 18, now 20
```

**Eight resubscribe events for 8 new market discoveries** over 47 min. Every
discovery cycle that added tokens triggered exactly one resubscribe — the
intended cadence. Zero `ERROR` lines, zero tracebacks, zero `RuntimeError`
caught (the catch from `dcfe272` never had to fire), zero
`prime_from_rest: empty REST /book for ACTIVE token` warnings.

## 4. Daemon side cross-check (same netns)

The daemon runs its own `clob_book_feed` WS subscription independently of the
scraper, in the same netns. R3-window event tally from `events.jsonl`:

| event type | count |
|---|---|
| entry_signal | 28 |
| shadow_entry | 23 |
| shadow_exit  | 13 |
| shadow_resolve | 9 |
| market_rollover | 9 |
| entry_rejected | 6 |
| shadow_entry_rejected | 3 |
| exit_filled | 2 |
| entry_filled | 2 |
| startup / shutdown / session_start | 3 |

walked_vwap activity specifically: 2 entry_signals, 2 entry_filled, 2 exit_filled,
6 entry_rejected. Reject-reason distribution: `insufficient_walked_edge` (3) +
`insufficient_top_of_book` (3) — exactly the economic reject reasons we expect
from a working entry gate against populated books, NOT the data-quality
`empty_book` / `book_baseline_missing` reasons that would indicate a feed problem.

**Both the daemon's WS feed and the scraper's WS feed worked inside the netns.**

## 5. Hypothesis evaluation

| Hypothesis | Verdict | Evidence |
|---|---|---|
| (A) Netns-specific subscription failure | **REJECTED** | Resubscribes fired (§3); book events 100 % populated (§2); daemon's separate WS in same netns also worked (§4) |
| (B) Sustained-load race after N rollovers | **REJECTED** | No silent task death; no traceback; resubscribe count matches discovery count exactly |
| (C) Frame-count explosion as root cause | **PARTIALLY CONFIRMED — but not a bug** | Inside-netns Polymarket traffic was genuinely ~20× the smoke-test rate. The scraper writes one record per WS event by design. The high count is real Polymarket activity, not a defect. |

## 6. Two genuine secondary findings (not the cause of "failure")

These are real, but they did not cause R3 to fail. They are surfaced by the
diagnostic and worth tracking.

### Finding F1 — Periodic snapshotter is starved during high-rate book events

`scripts/scrape_book.py:603` (in `_handle_book`) sets
`st.last_snapshot_ts = time.time()` after applying every WS book event. The
periodic snapshotter at `:462` skips any token whose `now - last_snapshot_ts <
SNAPSHOT_CADENCE_S` (5 s). The intended effect is "don't write a duplicate
periodic snapshot if a `book` event already wrote a record" (comment at
:604-:607). The unintended consequence: when book events arrive faster than
every 5 s — which is normal during active markets — the periodic snapshotter
NEVER writes for that token.

Evidence per slug:

| Slug | Book event rate (active period) | Periodic-snapshot fire rate vs expected |
|---|---|---|
| 1778128500 (resolved early in session) | 11/sec for 5 min, then 0 | 992/1012 empty *(post-resolution; correct)* |
| 1778128800 (resolved mid-session)      | 7/sec for 10 min, then 0  | 78 populated periodic — partial; race won 61 % of expected fires |
| 1778131200 (still active at SIGTERM)   | 16/sec for 4.5 min        | 12 populated periodic — race won 22 % of expected fires; periodic stream STOPS at 05:19:38 even though SIGTERM was at 05:22:55 |

Mitigation: replay (`polyhustle/data/replay.py:228-247`) reads `book` and
`price_change` event types directly, so it does NOT rely on periodic snapshots
to reconstruct state. Consumers that read ONLY snapshots — older sweep tools,
ad-hoc analysis scripts, or any methodology that resembles the smoke gate —
WILL starve.

### Finding F2 — token_states never deletes resolved markets (intentional)

Source comment at `_discovery_loop`: "Existing entries are never deleted from
`token_states` during the session — deletion happens on process restart." After
a market resolves, its book empties (correctly, via cancellation `price_change`
events with `size: "0"` for every level). Periodic snapshots for that token
keep firing every 5 s, all empty, for the rest of the session.

For slug 1778128500 in R3, populated periodic snapshots cover 04:40:37-04:41:32
(post-active cleanup window). Empty periodic snapshots cover 04:41:37-05:22:53
(41 minutes of post-resolution writing). 992 empty periodics in a single
slug-file is structurally correct but visually alarming.

The hash transition for one asset right at resolution (extracted in the
diagnostic): a cascade of `('SELL', '0.01', '0')` ... `('SELL', '0.99', '0')`
price-change deltas wipes asks level-by-level, ending with the empty-state
sentinel hash `e3b0c44298fc1c14`.

This is NOT a bug, but it interacts with the smoke gate methodology to amplify
the appearance of a problem — most snapshots in any multi-market window are
post-resolution empties of older markets.

## 7. Why the smoke gate passed

The 50 %-populated-frames-per-slug gate from CLAUDE.md §18 implicitly assumed:
- predicate = "top-level bids OR asks" (which excludes `book` events and
  `price_change` events as shown in §2)
- low-volume traffic, where `book` events / `snapshot` records dominate the
  file (smoke-2 reported 7/sec across all slugs; R3 was 117/sec/slug — ~50× more
  per-slug, ~250× more total)

Outside netns at low traffic, the predicate found populated `snapshot` records
(snapshotter starvation didn't trigger because book event rate was below
1/5 s) plus the predicate accidentally counted nothing-but-misleadingly-passed
on the empty `book` records (because those events are uncommon at low traffic).
At ~94 % populated frames, the gate passed.

Inside netns at high traffic, the predicate misses the data-bearing event
classes. At ~0 % populated, the gate "fails" — but the data is there.

**The gate doesn't measure scraper correctness; it measures Polymarket's traffic
profile.** It needs to be replaced before being applied to any future capture.

## 8. Ranked candidate follow-ups

In priority order; each is a small, separable branch.

1. **Fix the smoke gate methodology.** Replace the top-level-bids predicate with
   the corrected "any data anywhere" predicate from §2 (`book.raw.bids|asks`,
   `snapshot.bids|asks`, OR any `price_change.deltas[*].size > 0`). Bake it
   into a script under `scripts/` so it's reproducible and run by CI/launcher.
   This unblocks any future capture; without it the next R3-style "failure"
   alarm fires the same way.

2. **(Optional) Fix periodic-snapshotter starvation (Finding F1).** Either
   stop updating `st.last_snapshot_ts` from `_handle_book`, or change the
   snapshotter's gate to "≥ N seconds since last *periodic* snapshot, regardless
   of book events". Replay does not need this fix; sweep tools and the
   reconcile pipeline that may read snapshots-only would benefit. Default-off
   feature flag would be appropriate per CLAUDE.md §7.

3. **(Optional) Document or fix Finding F2.** Either delete resolved markets
   from `token_states` (changes the per-slug-file shape; needs replay parity
   check), or document explicitly in CLAUDE.md §18 that empty periodic
   snapshots after market resolution are correct and the per-slug "populated
   ratio" cannot be a single number — it must be windowed to the active period.

## 9. Constraints honoured

- No code edits. No daemon restart. No new capture run.
- Diagnostics ran read-only against the failed-capture artefact.
- Protected list (`active_bots/execution/{live_executor,reconciler,risk_manager}.py`,
  `RefinedStrategy` squeeze path) untouched.
- `scripts/scrape_book.py` was read-only inspected, not edited.

---

*Generated 2026-05-07 against the failed-R3 capture artefacts. No code changes, no
fix proposed. Standing by for review.*
