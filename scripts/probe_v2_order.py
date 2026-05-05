#!/usr/bin/env python3
"""Standalone probe for py-clob-client-v2 order posting.

Posts a single FAK market order via the migrated V2 client and prints
the full response dict verbatim. NOT integrated with the daemon — this
is a one-shot tool for verifying that V1->V2 signing works against the
post-cutover Polymarket CLOB V2 exchange (order_version_mismatch
diagnostic).

Usage:
    python scripts/probe_v2_order.py <token_id> <side> <size_usdc>
    python scripts/probe_v2_order.py <token_id> <side> <size_usdc> --dry-run

    side := BUY | SELL
    size_usdc is interpreted as the V2 SDK convention: USD for BUY,
    shares for SELL (mirrors LiveExecutor's _place_market_order).

--dry-run constructs the MarketOrderArgs and prints what would have
been sent (token_id, side, amount, order_type, options) without ever
calling create_and_post_market_order. Use this first to confirm imports
+ env config before risking a real order.

Env requirements (same as the daemon):
    POLYMARKET_PRIVATE_KEY     — 0x-prefixed EOA owner key
    POLYMARKET_FUNDER          — 0x-prefixed proxy wallet address
    POLYMARKET_API_KEY         — optional; will derive if absent
    POLYMARKET_API_SECRET      — optional
    POLYMARKET_API_PASSPHRASE  — optional
    POLYMARKET_CHAIN_ID        — default 137
    POLYMARKET_SIGNATURE_TYPE  — default 1 (POLY_PROXY)

Outcomes to expect:
    success                — print full response, capture field names
                              for _extract_fills V2 branch update
    order_version_mismatch — V2 SDK signing path is broken for this
                              signature_type too; revert migration,
                              escalate to py-clob-client-v2 issue #32
    other 4xx              — print verbatim; triage case-by-case
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Load .env so the probe works from any shell (the daemon loads it via
# dotenv at startup; a bare `python scripts/probe_v2_order.py …` invocation
# doesn't, which previously failed at build_client() with a missing
# POLYMARKET_PRIVATE_KEY). Optional — silently no-ops if env is already
# exported (the launcher's path) or if python-dotenv isn't installed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Repo-root on sys.path so we can reuse the factory.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Probe py-clob-client-v2 with a single FAK market order",
    )
    p.add_argument("token_id", help="CLOB token id (long decimal string)")
    p.add_argument("side", choices=["BUY", "SELL"], help="order side")
    p.add_argument(
        "size_usdc", type=float,
        help="USD for BUY, shares for SELL (V2 SDK amount convention)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="construct the order but do not POST",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    try:
        from py_clob_client_v2 import (
            MarketOrderArgs,
            OrderType,
            Side,
        )
    except ImportError as exc:
        print(f"ERROR: py-clob-client-v2 not installed: {exc}", file=sys.stderr)
        return 2

    side_const = Side.BUY if args.side == "BUY" else Side.SELL
    order_args = MarketOrderArgs(
        token_id=args.token_id,
        amount=float(args.size_usdc),
        side=side_const,
        order_type=OrderType.FAK,
    )

    print("=" * 60)
    print("PROBE — py-clob-client-v2 single FAK market order")
    print("=" * 60)
    print(f"token_id   : {args.token_id}")
    print(f"side       : {args.side}  (Side.{args.side} = {side_const})")
    print(f"amount     : {args.size_usdc}")
    print("order_type : OrderType.FAK")
    print("options    : None  (defer to V2 internal tick-size resolution)")
    print(f"dry_run    : {args.dry_run}")
    print()

    if args.dry_run:
        print("DRY RUN — not posting.")
        print(f"MarketOrderArgs repr: {order_args!r}")
        return 0

    try:
        from active_bots.execution.clob_client_factory import build_client
    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: build_client import failed: {exc}", file=sys.stderr)
        return 2

    try:
        client = build_client()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: build_client() failed: {exc}", file=sys.stderr)
        return 3

    print(f"client built: {client.__class__.__module__}.{client.__class__.__name__}")
    print("posting…")
    print()

    try:
        resp = client.create_and_post_market_order(
            order_args=order_args,
            options=None,
            order_type=OrderType.FAK,
        )
    except Exception as exc:  # pylint: disable=broad-except
        print("=" * 60)
        print("EXCEPTION")
        print("=" * 60)
        print(f"type    : {type(exc).__name__}")
        print(f"message : {exc!s}")
        # py-clob-client-v2 raises PolyApiException with .status_code,
        # .error_message; print everything we can introspect.
        for attr in ("status_code", "error_message", "response", "args"):
            val = getattr(exc, attr, None)
            if val is not None:
                print(f"{attr:8}: {val!r}")
        return 1

    print("=" * 60)
    print("RESPONSE")
    print("=" * 60)
    print(f"type : {type(resp).__name__}")
    if isinstance(resp, dict):
        # Print verbatim — keys are exactly what _extract_fills will see.
        print(json.dumps(resp, indent=2, default=str))
        print()
        print("KEY ROLL-CALL (for _extract_fills V2 branch update):")
        for k in sorted(resp.keys()):
            print(f"  {k}: {type(resp[k]).__name__}")
    else:
        print(repr(resp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
