# Legacy scripts — parked, not imported

These files are dead code: not imported by any module under `active_bots/`,
`tests/`, `polyhustle/`, `daemon_base_v1.py`, or `tui/`. Each is kept here
for reference until the operator decides to remove it permanently.

| File                    | Replaced by                                                      |
|-------------------------|------------------------------------------------------------------|
| `rtds_monitor.py`       | `daemon_base_v1.py:rtds_feed` + `daemon_state/events.jsonl`      |
| `web_monitor.py`        | `tui/python/dashboard.py` + `tui/rust/`                          |
| `orderbook_monitor.py`  | `daemon_base_v1.py:clob_book_feed` + `daemon_state/book_feed/`   |
| `scrape.py`             | `scrap/0[1-7]_*.py` modules orchestrated by `scrape_all.sh`      |

Adding new code? Don't put it here. New code lives in `polyhustle/`,
`active_bots/`, or `scripts/` depending on its purpose.
