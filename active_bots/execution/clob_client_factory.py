"""Build a fully-authenticated py_clob_client_v2.ClobClient from env config.

Usage:
    from active_bots.execution.clob_client_factory import build_client
    client = build_client()

Never logs the private key. Logs the funder address once at startup so the
operator can eyeball it against their Polymarket profile.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("execution.clob_factory")

HOST = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137


class ClobFactoryError(RuntimeError):
    pass


def build_client():
    """Return a py-clob-client-v2 ClobClient with API creds set.

    Raises ClobFactoryError with a human-readable message if env is missing or
    py-clob-client-v2 is not installed.
    """
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient  # type: ignore
    except ImportError as exc:
        raise ClobFactoryError(
            "py-clob-client-v2 not installed. Run: uv pip install -r requirements.txt"
        ) from exc

    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
    api_key = os.environ.get("POLYMARKET_API_KEY", "").strip()
    api_secret = os.environ.get("POLYMARKET_API_SECRET", "").strip()
    api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
    try:
        chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", DEFAULT_CHAIN_ID))
    except ValueError:
        chain_id = DEFAULT_CHAIN_ID
    try:
        signature_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))
    except ValueError:
        signature_type = 1

    if not key or not key.startswith("0x") or len(key) < 10:
        raise ClobFactoryError(
            "POLYMARKET_PRIVATE_KEY missing or malformed (expected 0x-prefixed hex). "
            "Required for EIP-712 order signing — L2 API creds alone cannot place orders."
        )
    if not funder or not funder.startswith("0x"):
        raise ClobFactoryError(
            "POLYMARKET_FUNDER missing (expected 0x-prefixed address of proxy wallet)"
        )

    preseeded = all([api_key, api_secret, api_passphrase])
    client = ClobClient(
        HOST,
        chain_id=chain_id,
        key=key,
        signature_type=signature_type,
        funder=funder,
        creds=ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        if preseeded else None,
    )
    if not preseeded:
        # V2 renamed create_or_derive_api_creds() to create_or_derive_api_key().
        client.set_api_creds(client.create_or_derive_api_key())

    logger.info(
        "ClobClient ready: funder=%s chain=%d sig_type=%d creds=%s",
        funder, chain_id, signature_type,
        "preseeded" if preseeded else "derived",
    )
    return client
