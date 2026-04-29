"""Tests for HMAC verification + replay protection."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from webhook_ai_router.core.exceptions import (
    SignatureInvalidError,
    TimestampExpiredError,
)
from webhook_ai_router.core.security import verify_hmac

SECRET = "shh-it-is-a-secret"
BODY = b'[{"eventId": 1, "objectId": 42}]'
NOW = 1_700_000_000.0


def _sign(body: bytes, ts: int, secret: str = SECRET) -> str:
    msg = str(ts).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def test_valid_signature_passes() -> None:
    ts = int(NOW)
    sig = _sign(BODY, ts)
    verify_hmac(SECRET, BODY, sig, str(ts), now=NOW)


def test_invalid_signature_raises() -> None:
    ts = int(NOW)
    with pytest.raises(SignatureInvalidError):
        verify_hmac(SECRET, BODY, "deadbeef" * 8, str(ts), now=NOW)


def test_signature_with_wrong_secret_raises() -> None:
    ts = int(NOW)
    sig = _sign(BODY, ts, secret="other-secret")
    with pytest.raises(SignatureInvalidError):
        verify_hmac(SECRET, BODY, sig, str(ts), now=NOW)


def test_tampered_body_raises() -> None:
    ts = int(NOW)
    sig = _sign(BODY, ts)
    tampered = BODY + b"x"
    with pytest.raises(SignatureInvalidError):
        verify_hmac(SECRET, tampered, sig, str(ts), now=NOW)


def test_expired_timestamp_raises() -> None:
    old_ts = int(NOW) - 600  # older than default 300s window
    sig = _sign(BODY, old_ts)
    with pytest.raises(TimestampExpiredError):
        verify_hmac(SECRET, BODY, sig, str(old_ts), now=NOW)


def test_future_timestamp_raises() -> None:
    future_ts = int(NOW) + 600
    sig = _sign(BODY, future_ts)
    with pytest.raises(TimestampExpiredError):
        verify_hmac(SECRET, BODY, sig, str(future_ts), now=NOW)


def test_non_integer_timestamp_raises() -> None:
    ts = int(NOW)
    sig = _sign(BODY, ts)
    with pytest.raises(SignatureInvalidError):
        verify_hmac(SECRET, BODY, sig, "not-a-number", now=NOW)


def test_max_age_boundary_allows_just_inside() -> None:
    """Right at the edge of the window should still validate."""
    edge_ts = int(NOW) - 300
    sig = _sign(BODY, edge_ts)
    verify_hmac(SECRET, BODY, sig, str(edge_ts), max_age_seconds=300, now=NOW)


def test_custom_max_age_rejects_outside_window() -> None:
    old_ts = int(NOW) - 60
    sig = _sign(BODY, old_ts)
    with pytest.raises(TimestampExpiredError):
        verify_hmac(SECRET, BODY, sig, str(old_ts), max_age_seconds=30, now=NOW)
