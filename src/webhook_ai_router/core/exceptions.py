"""Exception hierarchy for the webhook router.

All exceptions raised by the router inherit from :class:`WebhookError`, which
carries an HTTP ``status_code`` and a short human-readable ``title``. The
FastAPI exception handlers in :mod:`webhook_ai_router.main` translate these
into RFC 7807 problem responses.
"""

from __future__ import annotations

from http import HTTPStatus


class WebhookError(Exception):
    """Base class for all webhook-related errors."""

    status_code: int = HTTPStatus.BAD_REQUEST
    title: str = "Webhook error"

    def __init__(self, detail: str | None = None) -> None:
        self.detail: str = detail if detail else self.title
        super().__init__(self.detail)


class SignatureInvalidError(WebhookError):
    status_code = HTTPStatus.UNAUTHORIZED
    title = "Invalid webhook signature"


class TimestampExpiredError(WebhookError):
    status_code = HTTPStatus.UNAUTHORIZED
    title = "Webhook timestamp expired"


class PayloadInvalidError(WebhookError):
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Invalid webhook payload"
