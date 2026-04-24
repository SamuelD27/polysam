# Session capture — design notes for `reconcile.py`

Load-bearing assumptions about how `launch_daemon.sh` routes scraper
output. A future reconcile.py that globs a session directory expecting
book files will find nothing; read this first.

## 1. Scraper output stays at canonical path, NOT per-session

`scripts/scrape_book.py` hard-codes `FEED_DIR = daemon_state/book_feed`
at module scope. It has no CLI flag or env var to redirect. Changing
that would touch the scraper (forbidden by the R2.2 brief), so the
launcher takes a different route:

- Raw book data is always written to
  `daemon_state/book_feed/{YYYY-MM-DD}/{slug}.jsonl.gz`.
- The session dir at `daemon_state/scrapes/<session_id>/` holds:
  - `manifest.json` — session metadata (see §4)
  - `scraper.log` — stdout/stderr of the scraper process
  - `scraper.pid` — while the scraper is alive
  - `book_feed/` — symlink to `../../book_feed`
    (relative, so the session dir stays portable across repo moves)

Consequence: `reconcile.py` cannot `glob('daemon_state/scrapes/<sid>/*.jsonl.gz')`
and expect book records. It must either
(a) follow the `book_feed` symlink, or
(b) read `manifest.json` → `launch_ts_ns`/`stop_ts_ns` → glob
    `daemon_state/book_feed/**/*.jsonl.gz` → filter records where
    `ts_ns ∈ [launch_ts_ns, stop_ts_ns]`.

Prefer (b). It survives the symlink being missing on a reconstituted
session dir and handles the edge case where the scraper overlapped two
sessions on disk (same-day files are append-only per slug).

## 2. Output format is `.jsonl.gz`, NOT `.parquet`

The original spec wording mentioned "parquet" as a hint; the scraper
actually emits per-slug gzipped JSONL — one JSON record per line,
written through `gzip.open(..., "at")`. Record shapes:

- `type=book` — full snapshot (asset_id, bids[], asks[], hash, ts_ns)
- `type=snapshot` — reconstructed snapshot at 5 s cadence (same shape, plus `reason`)
- `type=price_change` — level delta list (asset_id, deltas[], remote_hashes, local_hash_after)
- `type=best_bid_ask` — top-of-book update (in `raw`)
- `type=last_trade_price` — trade tick (in `raw`)
- `type=tick_size_change` — rare, emits a new canonical tick
- `type=reconnect` — WS dropped, capture interrupted
- `type=tick_size_disagreement` — observed precision finer than canonical

`manifest.json` records this as
`"scrape_output_format": "per-slug gzipped JSONL: {YYYY-MM-DD}/{slug}.jsonl.gz"`.
Parquet conversion, if needed downstream, is a reconcile.py step, not a
scraper step.

## 3. Token list is auto-discovered, NOT pre-resolved

The manifest stores
`"tokens_scraped": "auto:gamma-5m-active-window"` rather than an
explicit token list. Rationale:

- `scrape_book.py` runs a discovery loop at 60 s cadence
  (`discover_markets` → Gamma `/events?tag_slug=5M&active=true`),
  filtering to markets with `t_zero ≤ now < t_zero + 300` plus the
  next 5-minute window.
- Pre-resolving a list in the launch script would duplicate that
  discovery logic and race the 5-minute rollover: a list written at
  t=290 s is stale by t=305 s when a new window opens.
- The scraper reports the actual resolved set in its log:
  `initial subscription set: N tokens (from M markets)`.

Consequence: `reconcile.py` cannot trust the manifest to enumerate
which tokens have data. It must:
(a) parse `scraper.log` for the `subscribed assets=` line(s) and
    subsequent `new market <slug> <side> tick=...` lines, or
(b) walk the actual files in the session's date dirs under
    `daemon_state/book_feed/` and group by slug.

Prefer (b); (a) breaks if the log is truncated.

## 4. `manifest.json` schema

Populated at launch, updated at stop. Future keys may be added — read
with tolerance for unknown fields.

```json
{
  "session_id": "2026-04-24T04-53-07Z",
  "launch_ts_utc": "2026-04-24T04:53:07Z",
  "launch_ts_ns": 1777006387576908991,
  "stop_ts_utc": "2026-04-24T04:55:19Z",
  "stop_ts_ns": 1777006519611509586,
  "daemon_exit_code": "sigterm|sigkill|already_dead|none",
  "scraper_exit_code": "sigterm|sigkill|already_dead|none",
  "mode": "paper|live_dryrun|live",
  "max_trade_size_usdc": "$N" | null,
  "daemon_pid": 12345,
  "scraper_pid": 12346,
  "tokens_scraped": "auto:gamma-5m-active-window",
  "netns": "polybot" | null,
  "scrape_output_dir": ".../scrapes/<session_id>",
  "scrape_canonical_dir": ".../book_feed",
  "scrape_output_format": "per-slug gzipped JSONL: {YYYY-MM-DD}/{slug}.jsonl.gz",
  "events_jsonl_path": ".../events.jsonl",
  "daemon_log_path": ".../daemon.log",
  "git_sha": "abcd123",
  "git_branch": "Sam-Dev"
}
```

### Known-issue: `daemon_exit_code` may be `"sigkill"` even on a clean paper session

The launcher's stop_daemon wait window is 6 s; the daemon's
`SHUTDOWN_TIMEOUT_S` is 5 s plus ≤2 s finally-block overhead. When
tasks use the full asyncio.wait timeout, total daemon shutdown can
land at 5-7 s — occasionally past the launcher's 6 s threshold, at
which point SIGKILL fires and the manifest records `"sigkill"`.
Inspect `daemon.log` for a `daemon_base_v1 stopped` line near
`stop_ts_utc`: its presence means the daemon finished finally before
SIGKILL landed (log line survives the kill), absent means the race
was lost. Fix is either `SHUTDOWN_TIMEOUT_S` tightening in the daemon
or a wider launcher wait — pending decision.
