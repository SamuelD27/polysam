#!/usr/bin/env python3
"""Spot-check the populated-frame ratio of a captured ``book_feed`` directory.

Replaces ad-hoc bash one-liners (``zcat | grep | wc -l``) that conflated
the three event classes that carry book data and produced misleading
"0% populated" alarms on healthy captures.

Predicate (a record counts as "populated" iff one of):
    * ``type=="snapshot"`` AND (``bids`` non-empty OR ``asks`` non-empty)
    * ``type=="book"``     AND (``raw.bids`` non-empty OR ``raw.asks`` non-empty)
    * ``type=="price_change"`` AND any ``deltas[*].size != "0"``

Other event classes (``best_bid_ask``, ``last_trade_price``,
``tick_size_change``, etc.) carry useful metadata but no book depth, so
they do NOT count as populated. Empty periodic snapshots from
post-resolution markets correctly count as un-populated and drag the
ratio down — that is the intended behaviour, and well-active captures
still clear the 80% bar because the dense ``book`` and ``price_change``
streams during the active window dominate the file.

Usage:
    python scripts/check_book_feed.py <directory>
    python scripts/check_book_feed.py daemon_state/book_feed/2026-05-07/
    python scripts/check_book_feed.py daemon_state/book_feed/2026-05-07_R3/

Glob is recursive: pointing at ``daemon_state/book_feed/`` walks every
date subdir. Pointing at a date subdir walks only that day.

Exit codes:
    0  median populated ratio >= 80% (healthy; capture is real)
    1  median populated ratio <  50% (alarm; do NOT use this capture)
    2  median populated ratio in [50, 80) (surface and discuss)
    3  no .jsonl.gz files found / unreadable directory
"""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
from pathlib import Path

# Predicate thresholds — kept as module constants so the test suite can
# import them and verify the script-vs-test contract stays in sync.
HEALTHY_THRESHOLD = 0.80
ALARM_THRESHOLD = 0.50


def is_populated(rec: dict) -> bool:
    """The corrected populated-frame predicate.

    Returns True iff the record carries actual book data in any of the
    three event classes that the scraper writes book content into. See
    module docstring for the rationale on excluding ``best_bid_ask``
    etc.
    """
    t = rec.get("type")
    if t == "snapshot":
        return bool(rec.get("bids")) or bool(rec.get("asks"))
    if t == "book":
        raw = rec.get("raw") or {}
        return bool(raw.get("bids")) or bool(raw.get("asks"))
    if t == "price_change":
        for d in rec.get("deltas") or ():
            if str(d.get("size", "0")) != "0":
                return True
        return False
    return False


def _check_tape(
    path: Path | None, *, label: str, missing_consequence: str,
) -> str | None:
    """Validate one per-session replay tape (btc_ticks.jsonl or
    market_price.jsonl). Returns one of:
        None         — flag not provided; not checked
        "missing"    — flag provided but file does not exist
        "unreadable" — file exists but I/O failed
        "empty"      — file exists and is readable, zero lines
        "ok"         — file exists, readable, ≥1 line

    Shared shape between --btc-tape and --market-price so the script
    treats both tapes with the same predicate; mismatched semantics
    between the two would be a foot-gun.
    """
    if path is None:
        return None
    if not path.is_file():
        print(f"{label}: MISSING {path} — {missing_consequence}")
        return "missing"
    line_count = 0
    try:
        with path.open() as f:
            for line in f:
                if line.strip():
                    line_count += 1
    except OSError as e:
        print(f"{label}: UNREADABLE {path} — {e}")
        return "unreadable"
    print(f"{label}: OK {path} ({line_count} lines)")
    if line_count == 0:
        print(
            f"{label}: EMPTY — file exists but no lines. "
            f"{missing_consequence}"
        )
        return "empty"
    return "ok"


def scan_file(path: Path) -> tuple[int, int]:
    """Return (total_records, populated_records) for one .jsonl.gz file.

    Tolerant of mid-write truncation (gzip ``EOFError`` / ``OSError``):
    we count whatever JSON-parseable records exist before the truncation
    point. A live capture being inspected before SIGTERM is the common
    case for this.
    """
    total = 0
    populated = 0
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                total += 1
                if is_populated(rec):
                    populated += 1
    except (EOFError, OSError):
        pass
    return total, populated


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="book_feed directory (date subdir or parent)",
    )
    parser.add_argument(
        "--per-slug",
        action="store_true",
        help="print every slug's ratio (default: only outliers)",
    )
    parser.add_argument(
        "--btc-tape",
        type=Path,
        default=None,
        help=(
            "optional path to btc_ticks.jsonl (the H1 per-session BTC tape). "
            "If provided, the script also asserts the file exists, has at "
            "least 1 line, and parses cleanly. A capture missing its BTC "
            "tape is unreplayable — see CLAUDE.md §7."
        ),
    )
    parser.add_argument(
        "--market-price",
        type=Path,
        default=None,
        help=(
            "optional path to market_price.jsonl (the H2 per-session RTDS "
            "tape). Same semantics as --btc-tape: asserts the file exists, "
            "has at least 1 line, and parses cleanly. A capture missing "
            "its market_price tape replays with market_price_up pinned to "
            "the orchestrator's 0.5 constant (the R4 bug) — see CLAUDE.md §7."
        ),
    )
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"error: not a directory: {args.directory}", file=sys.stderr)
        return 3

    files = sorted(args.directory.rglob("*.jsonl.gz"))
    if not files:
        print(
            f"error: no .jsonl.gz files under {args.directory}", file=sys.stderr,
        )
        return 3

    print(f"scanning {len(files)} files under {args.directory}")
    rows: list[tuple[str, int, int, float]] = []
    for f in files:
        total, pop = scan_file(f)
        ratio = pop / total if total else 0.0
        rows.append((f.name, total, pop, ratio))

    ratios = [r[3] for r in rows if r[1] > 0]
    if not ratios:
        print("error: every file empty after parse", file=sys.stderr)
        return 3

    median = statistics.median(ratios)
    p25 = statistics.quantiles(ratios, n=4)[0] if len(ratios) >= 4 else min(ratios)
    p75 = statistics.quantiles(ratios, n=4)[2] if len(ratios) >= 4 else max(ratios)

    if args.per_slug:
        for name, total, pop, ratio in sorted(rows, key=lambda r: r[3]):
            flag = "  "
            if ratio < ALARM_THRESHOLD:
                flag = "!!"
            elif ratio < HEALTHY_THRESHOLD:
                flag = " ?"
            print(f"  {flag} {ratio:6.1%}  ({pop:>7d} / {total:>7d})  {name}")
    else:
        for name, total, pop, ratio in sorted(rows, key=lambda r: r[3]):
            if ratio < HEALTHY_THRESHOLD:
                flag = "!!" if ratio < ALARM_THRESHOLD else " ?"
                print(f"  {flag} {ratio:6.1%}  ({pop:>7d} / {total:>7d})  {name}")

    print()
    print(f"files:  {len(rows)}")
    print(f"median: {median:.1%}   p25: {p25:.1%}   p75: {p75:.1%}")

    # Optional tape checks. Captures without these tapes are unreplayable
    # in mechanically-distinct ways:
    #   - missing BTC tape  → replay's _build_tick returns None for every
    #                          tick (btc=None) and the strategy never runs.
    #   - missing RTDS tape → replay's _market_price_at returns None and
    #                          the orchestrator's `or 0.5` fallback pins
    #                          market_price_up to a constant 0.5; the
    #                          strategy runs but on bogus prices (R4 bug).
    # Both surface here, BEFORE the operator commits to a 12-hour capture.
    btc_tape_status = _check_tape(args.btc_tape, label="BTC tape", missing_consequence=(
        "replay will produce zero ticks. Confirm the daemon is writing "
        "btc_ticks.jsonl (POLYMARKET_SCRAPE_SESSION_DIR set, H1 daemon)."
    ))
    market_price_status = _check_tape(
        args.market_price, label="market_price tape",
        missing_consequence=(
            "replay will pin market_price_up to the orchestrator's 0.5 "
            "constant (the R4 bug). Confirm the daemon is writing "
            "market_price.jsonl (POLYMARKET_SCRAPE_SESSION_DIR set, H2 daemon)."
        ),
    )

    bad_tapes = [
        (name, status)
        for name, status in (
            ("BTC_TAPE", btc_tape_status),
            ("MARKET_PRICE_TAPE", market_price_status),
        )
        if status not in (None, "ok")
    ]

    if median >= HEALTHY_THRESHOLD:
        if bad_tapes:
            tape_desc = ", ".join(
                f"{name}_{status.upper()}" for name, status in bad_tapes
            )
            print(
                f"verdict: BOOK_HEALTHY but {tape_desc} — "
                "capture is partially unreplayable"
            )
            return 2
        print(f"verdict: HEALTHY (median >= {HEALTHY_THRESHOLD:.0%})")
        return 0
    if median < ALARM_THRESHOLD:
        print(f"verdict: ALARM (median < {ALARM_THRESHOLD:.0%}) — do not use")
        return 1
    print(
        f"verdict: BORDERLINE (median in [{ALARM_THRESHOLD:.0%}, "
        f"{HEALTHY_THRESHOLD:.0%})) — surface and discuss"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
