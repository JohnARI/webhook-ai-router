"""Request-scoped middleware (request id correlation)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER: Final = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach an ``X-Request-ID`` to every request/response and bind it
    into ``structlog.contextvars`` so all log records on this request are
    correlated automatically.

    If the inbound request already carries an ``X-Request-ID`` we honour
    it; otherwise we generate a fresh UUID4.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
