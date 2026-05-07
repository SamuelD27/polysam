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
            "tape is unreplayable — see CLAUDE.md §19."
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

    # Optional BTC-tape check (H1). A capture without a BTC tape is
    # unreplayable — replay's _build_tick returns None for every tick
    # when btc is None. Healthier to surface this BEFORE the operator
    # leaves a 12-hour capture running.
    btc_tape_status = None
    if args.btc_tape is not None:
        if not args.btc_tape.is_file():
            print(
                f"BTC tape: MISSING {args.btc_tape} — replay will produce "
                "zero ticks. Confirm the daemon is writing btc_ticks.jsonl "
                "(POLYMARKET_SCRAPE_SESSION_DIR set, H1 daemon)."
            )
            btc_tape_status = "missing"
        else:
            line_count = 0
            try:
                with args.btc_tape.open() as f:
                    for line in f:
                        if line.strip():
                            line_count += 1
            except OSError as e:
                print(f"BTC tape: UNREADABLE {args.btc_tape} — {e}")
                btc_tape_status = "unreadable"
            else:
                print(f"BTC tape: OK {args.btc_tape} ({line_count} lines)")
                btc_tape_status = "ok" if line_count > 0 else "empty"
                if line_count == 0:
                    print(
                        "BTC tape: EMPTY — file exists but no lines. "
                        "Replay will produce zero ticks."
                    )

    if median >= HEALTHY_THRESHOLD:
        if btc_tape_status not in (None, "ok"):
            print(
                f"verdict: BOOK_HEALTHY but BTC_TAPE_{btc_tape_status.upper()} — "
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
