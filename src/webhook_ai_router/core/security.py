"""HMAC verification with replay protection."""

from __future__ import annotations

import hashlib
import hmac
import time

from webhook_ai_router.core.exceptions import (
    SignatureInvalidError,
    TimestampExpiredError,
)


def verify_hmac(
    secret: str,
    body: bytes,
    signature: str,
    timestamp: str,
    max_age_seconds: int = 300,
    *,
    now: float | None = None,
) -> None:
    """Verify an HMAC-SHA256 signature over ``f"{timestamp}.{body}"``.

    Raises :class:`SignatureInvalidError` if the signature doesn't match (or
    the inputs aren't well-formed) and :class:`TimestampExpiredError` if the
    timestamp falls outside the ``max_age_seconds`` window in either
    direction (replay protection).

    The comparison uses :func:`hmac.compare_digest`. Never use ``==``.

    ``now`` is injectable for tests; production callers should leave it
    ``None`` so :func:`time.time` is used.
    """
    try:
        ts = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise SignatureInvalidError("Timestamp is not an integer") from exc

    current = now if now is not None else time.time()
    skew = current - ts
    if skew > max_age_seconds:
        raise TimestampExpiredError(
            f"Timestamp older than {max_age_seconds} seconds",
        )
    if skew < -max_age_seconds:
        raise TimestampExpiredError("Timestamp is too far in the future")

    message = timestamp.encode("ascii") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise SignatureInvalidError("Signature does not match expected value")
