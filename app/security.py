"""HMAC-SHA256 webhook signature verification.

Scheme (documented in README):

    signature_payload = f"{X-Webhook-Timestamp}.{raw_request_body}"
    signature         = HMAC_SHA256(webhook_secret, signature_payload).hexdigest()
    header            = X-Webhook-Signature: sha256=<hex>

The timestamp is checked against a tolerance window to mitigate replay attacks.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException


def compute_signature(secret: str, timestamp: int | str, body: bytes) -> str:
    payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    timestamp_header: str | None,
    signature_header: str | None,
    body: bytes,
    tolerance_seconds: int,
) -> None:
    if not timestamp_header or not signature_header:
        raise HTTPException(status_code=401, detail="missing signature headers")

    try:
        ts = int(timestamp_header)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid signature timestamp")

    now = int(time.time())
    if abs(now - ts) > tolerance_seconds:
        raise HTTPException(status_code=401, detail="stale signature timestamp")

    provided = signature_header
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):]

    expected = compute_signature(secret, ts, body)
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")
