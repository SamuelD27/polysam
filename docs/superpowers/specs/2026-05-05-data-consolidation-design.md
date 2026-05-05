# Polymarket Data Consolidation — Design Spec

**Date:** 2026-05-05
**Status:** Approved

## Goal

Gather all scraped data — both targeted-scraper output and daemon-run output — and standardize it into the smallest practical set of CSV files without losing any information.

## Sources to consolidate

### Targeted scraping (`data/`)

Five per-asset SQLite DBs (`btc5m.db`, `doge5m.db`, `eth5m.db`, `sol5m.db`, `xrp5m.db`), each containing the same 10 tables:

| Table             | Description                                      | Notes                                                |
|-------------------|--------------------------------------------------|------------------------------------------------------|
| `markets`         | Market metadata                                  | Same `condition_id` may appear in multiple DBs       |
| `trades`          | REST-fetched executed trades                     | ~52M rows total across the 5 DBs                     |
| `price_histories` | Yes/No price time series                         | ~4.2M rows total                                     |
| `orderbooks`      | REST orderbook snapshots                         | ~525K rows (mostly in `btc5m.db`)                    |
| `spot`            | 5-min OHLCV candles                              | Each DB tracks its own asset's spot                  |
| `ws_trades`       | Websocket-captured trades                        |                                                       |
| `ws_spot`         | Websocket-captured spot ticks                    | ~1.2M rows total                                     |
| `resolutions`     | Closed-market outcomes                           |                                                       |
| `traders`         | Wallet trading aggregates                        | Only `btc5m.db` is populated                         |
| `btc_spot`        | Legacy BTC OHLCV table                           | Duplicate of `spot` in `btc5m.db`; skip              |
| `scrape_progress` | Internal scraper bookkeeping                     | Skip                                                 |

### Daemon runs (`daemon_state/`)

| Source                              | Description                                              | Size                |
|-------------------------------------|----------------------------------------------------------|---------------------|
| `book_feed/**/*.jsonl.gz`           | Live websocket orderbook snapshots + updates             | 1.8 GB compressed   |
| `events.jsonl`                      | Bot trading events (session_start, entry, exit, etc.)    | 10K rows            |
| `dashboard_history.jsonl`           | Bot internal dashboard ticks                             | 5K rows             |
| `scrapes/<session>/manifest.json`   | Scrape session metadata stubs                            | Skip — metadata only|

### Explicitly skipped

- Legacy `data/0X_*.csv` (stale partial exports, kept in place untouched)
- `data/*errors.log`, `data/logs/`, `daemon_state/daemon.log`, `daemon_state/stdout.log` — error logs, not data
- `data/btc_spot` table (duplicate of `spot` table in same DB)
- `scrape_progress` tables (scraper bookkeeping)
- `daemon_state/scrapes/*/manifest.json` (metadata-only stubs)

## Output

8 plain `.csv` files in `data/consolidated/`. One-shot, idempotent rebuild — re-running the script truncates and rewrites all 8 files.

### Merge keys

- `asset` column on every per-asset table — values: `btc`, `doge`, `eth`, `sol`, `xrp`
- `source` column — values: `rest` or `ws` (or `ws_book_feed` for the live orderbook stream) — used wherever REST and WS variants of the same entity are unified into one CSV

### File 1 — `markets.csv`

Markets + resolutions merged on `condition_id`. Resolution columns are NULL for unresolved markets.

```
condition_id, slug, asset, title, start_date, end_date,
volume, liquidity, closed, active,
yes_token_id, no_token_id,
outcome_price_yes, outcome_price_no, resolved_outcome,
btc_price_at_start, btc_price_at_end
```

**Dedup strategy:** A given `condition_id` may exist in multiple DBs. Pick the row with the most complete data (prefer rows where `closed=1`, then highest `volume`). The `asset` column reflects which DB the chosen row came from.

### File 2 — `trades.csv`

REST `trades` + WS `ws_trades` unified across all 5 asset DBs.

```
asset, source, trade_id, wallet, condition_id, slug,
side, outcome, size, price, usdc_value, fee_rate_bps,
match_time, transaction_hash, timestamp
```

- `source=rest`: all columns populated except `timestamp` (NULL — REST uses `match_time`)
- `source=ws`: `trade_id`, `wallet`, `condition_id`, `usdc_value`, `fee_rate_bps`, `transaction_hash` are NULL; `timestamp` populated; `match_time` NULL

**Streaming:** This file dominates row count (~52M). Stream rows directly from each `sqlite3` cursor through `csv.writer` — never load into memory.

### File 3 — `price_histories.csv`

Direct concat across 5 DBs, prepending `asset`.

```
asset, condition_id, timestamp, yes_price, no_price
```

### File 4 — `orderbooks.csv`

REST `orderbooks` tables + flattened live `book_feed/*.jsonl.gz`. Bids/asks stored as JSON strings inline (round-trip lossless).

```
asset, source, condition_id, slug, side, snapshot_time, ts_ns,
event_type, best_bid, best_ask, spread, depth_10c,
bids_json, asks_json,
asset_id, canonical_tick, effective_tick,
remote_hash, local_hash, reason
```

**REST half (`source=rest`)**:
- From SQLite `orderbooks` tables. `event_type="rest_snapshot"`. `ts_ns`, `asset_id`, `canonical_tick`, `effective_tick`, `remote_hash`, `local_hash`, `reason` are NULL.
- The REST table's `bids` and `asks` columns are already JSON strings — copy directly into `bids_json` / `asks_json`.

**WS half (`source=ws_book_feed`)**:
- From `daemon_state/book_feed/<date>/<slug>.jsonl.gz`. Two record types: `snapshot` (initial) and `book` (update).
- `asset` is derived from the slug prefix (`btc-` → `btc`, etc.).
- `best_bid`, `best_ask`, `spread`, `depth_10c` are computed from the ladder so the column is populated for both halves.
  - `best_bid` = max(bid prices); `best_ask` = min(ask prices); `spread` = ask − bid.
  - `depth_10c` = sum of bid sizes within 10¢ of `best_bid` plus sum of ask sizes within 10¢ of `best_ask` (matches the REST table's semantics).
- For `book` (update) records, the ladder lives at `raw.bids` / `raw.asks` — extract from there.
- `snapshot_time` is `ts_ns` formatted as ISO8601 UTC.

**Recursion:** walk `book_feed/<YYYY-MM-DD>/*.jsonl.gz`. The `book_feed/logs/` subdir is skipped.

### File 5 — `spot.csv`

5-min candles (`spot` table) + WS spot ticks (`ws_spot` table) unified across 5 assets.

```
asset, source, granularity, timestamp, open, high, low, close, volume, price, size
```

- `source=rest, granularity=5m`: `open`/`high`/`low`/`close`/`volume` populated, `price`/`size` NULL.
- `source=ws, granularity=tick`: `price`/`size` populated, OHLCV NULL.
- `timestamp` is integer Unix seconds for REST candles. WS `ws_spot.timestamp` is TEXT — parse with `datetime.fromisoformat` (or numeric coerce if it's already a numeric string) and write as integer Unix seconds. On parse failure, write the raw string verbatim and let the consumer handle it. The column is otherwise dtype-flexible (string-typed in CSV, numeric where parseable).

### File 6 — `traders.csv`

Wallet aggregates. Only `btc5m.db` is populated, but the `asset` column is kept for forward compatibility.

```
asset, wallet, total_trades, total_volume_usdc, win_rate, avg_trade_size,
first_trade_time, last_trade_time, favorite_side, favorite_outcome
```

### File 7 — `daemon_events.csv`

Flatten `daemon_state/events.jsonl`. Event payloads vary by `type` (e.g., `session_start`, `startup`, `market_rollover`, `entry_filled`, `exit_filled`, ...).

**Approach:** Two-pass.
1. First pass: scan all events, collect the union of all top-level keys across every record.
2. Second pass: write CSV with one column per key. Rows leave columns blank where that event type doesn't supply the field.

Nested objects (if any) are JSON-serialized into a single column. Acceptable cost: only 10K rows, schema width likely 25-30 columns.

```
ts, type, <union of all event keys>
```

### File 8 — `dashboard_history.csv`

Direct flatten from `daemon_state/dashboard_history.jsonl`. Schema is stable.

```
ts, btc_price, market_price_up, market_price_down, fair_base, fair_enh
```

## Implementation

**Language:** Python 3.11+
**Dependencies:** stdlib only (`sqlite3`, `csv`, `json`, `gzip`, `pathlib`, `datetime`)
**Location:** `scripts/consolidate_data.py`

### Build sequence

1. Ensure `data/consolidated/` exists; truncate any pre-existing 8 CSVs.
2. Pre-compute the union of `daemon_events.jsonl` keys (first pass over events).
3. Open all 8 CSV writers; write headers.
4. Walk the 5 SQLite DBs, in order: `btc5m`, `doge5m`, `eth5m`, `sol5m`, `xrp5m`. For each DB:
   - Stream `markets` rows into a dedup buffer keyed by `condition_id`. Write at the end (after all DBs scanned).
   - Stream `trades` rows directly to `trades.csv` with `source=rest`.
   - Stream `price_histories` directly to `price_histories.csv`.
   - Stream `orderbooks` directly to `orderbooks.csv` with `source=rest`.
   - Stream `spot` directly to `spot.csv` with `source=rest, granularity=5m`.
   - Stream `ws_trades` to `trades.csv` with `source=ws`.
   - Stream `ws_spot` to `spot.csv` with `source=ws, granularity=tick`.
   - Stream `resolutions` into a dict keyed by `condition_id` (used in step 7).
   - Stream `traders` to `traders.csv` with `asset=btc` (only btc DB has data).
5. After scanning all DBs, walk `daemon_state/book_feed/<YYYY-MM-DD>/*.jsonl.gz`. For each line, parse JSON, derive `best_bid`/`best_ask`/`spread`/`depth_10c`, write to `orderbooks.csv` with `source=ws_book_feed`.
6. Stream `daemon_state/events.jsonl` to `daemon_events.csv` (using the pre-computed key union).
7. Now flush the markets dedup buffer: for each `condition_id`, attach matching `resolutions` row data; write to `markets.csv`.
8. Stream `daemon_state/dashboard_history.jsonl` to `dashboard_history.csv`.
9. Print summary: per-CSV row count and total file size.

### Memory profile

Most tables stream straight through. The two memory-resident structures are:
- `markets` dedup buffer — at most ~95K unique `condition_id`s × ~14 fields ≈ tens of MB. Fine.
- `resolutions` lookup dict — ~10K rows. Trivial.
- `daemon_events` key union — single hash set of strings. Trivial.

### Idempotency

Re-running the script truncates and rewrites all 8 CSVs. No incremental state is tracked. This is by design — full rebuild on a fast NVMe is acceptable for the data volumes involved.

### Error handling

- Missing source files (e.g., a DB that doesn't exist) → log warning, skip that source, continue.
- Malformed JSONL lines in `book_feed` → log warning with file path + line number, skip line.
- Malformed JSON in `events.jsonl` / `dashboard_history.jsonl` → same: warn, skip.
- SQLite errors → propagate (script exits, user investigates).

## Validation

After the run, the script prints:

```
=== Consolidation summary ===
markets.csv             : <rows>  <size>
trades.csv              : <rows>  <size>
price_histories.csv     : <rows>  <size>
orderbooks.csv          : <rows>  <size>
spot.csv                : <rows>  <size>
traders.csv             : <rows>  <size>
daemon_events.csv       : <rows>  <size>
dashboard_history.csv   : <rows>  <size>
Total                   : <rows>  <size>
```

A separate sanity check (run manually): for `trades.csv`, the row count should equal sum-across-DBs of `trades` + `ws_trades` row counts. Same kind of check for the other tables.

## Out of scope

- Compression (`.csv.gz`) — explicit user preference for plain `.csv`.
- Deletion of legacy `data/0X_*.csv` files — explicit user preference to leave alone.
- Re-runnable / incremental updates — one-shot is sufficient.
- A 9th `scrape_sessions.csv` from `daemon_state/scrapes/*/manifest.json` — skipped as metadata, not data. Can be added later if needed.

## Run results — 2026-05-05

Full consolidation against production data completed in **14 minutes** (wall clock). Output at `data/consolidated/`.

| File                     | Rows         | Size    |
|--------------------------|--------------|---------|
| markets.csv              |       95,708 |  34.8MB |
| trades.csv               |   52,651,343 |  16.2GB |
| price_histories.csv      |    4,173,718 | 365.0MB |
| orderbooks.csv           |   53,640,759 |  18.0GB |
| spot.csv                 |    2,065,361 | 101.7MB |
| traders.csv              |          500 |  51.0KB |
| daemon_events.csv        |       10,142 |   4.9MB |
| dashboard_history.csv    |        5,277 | 480.7KB |
| **Total**                | **112,642,808** | **34.7GB** |

`orderbooks.csv` row count breakdown: 525,724 REST snapshots + 53,115,035 live book_feed records.

`daemon_events.csv` ended up 44 columns wide (key union across all event types).

### Sanity checks

- **Trades counts match source byte-for-byte** — per-asset / per-source split in `trades.csv` exactly equals the corresponding `trades` and `ws_trades` row counts in the 5 SQLite DBs.
- **JSON ladders are parseable** — random sample of 100K `ws_book_feed` rows: 0 JSON-parse failures on `bids_json` / `asks_json`.
- **No cross-DB market duplicates** — 95,708 markets is exactly the sum of all per-DB markets row counts (no condition_id appears in more than one DB).

### Bugs found and fixed during validation

1. **EOFError on truncated `.jsonl.gz`** — daemon-killed scrapes leave incomplete gzip streams. Original Task 11 only caught `OSError`. Fixed in commit `1d1aae3` to also catch `EOFError`.
2. **`zlib.error` on corrupted compression blocks** — separate failure mode (different exception type, not a subclass of `OSError` or `EOFError`). Fixed in commit `43e1f34`.

About 30 corrupt files in `book_feed/2026-05-01/` were skipped with warnings; the remaining 3,500+ files contributed 53M records cleanly.
