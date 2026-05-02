"""Prometheus metrics — domain-specific counters and histograms.

Wired into the FastAPI app by :func:`webhook_ai_router.main.create_app` via
:class:`prometheus_fastapi_instrumentator.Instrumentator`, which also adds
the default request/response/latency histograms. The four metrics below are
the application-specific signals; they are incremented from the route, the
worker, the dispatch service, and the global exception handler.

All metrics are exposed at ``GET /metrics`` (no auth). In a private
network that's fine; if you ever expose it publicly, sit it behind basic
auth or scrape it from a sidecar.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

from prometheus_client import Counter, Histogram

WEBHOOK_RECEIVED_TOTAL: Final = Counter(
    "webhook_received_total",
    "Webhook deliveries received, by source and acceptance status.",
    labelnames=("source", "status"),
)

WEBHOOK_PROCESSING_SECONDS: Final = Histogram(
    "webhook_processing_seconds",
    "End-to-end worker processing time per webhook (LLM + dispatch).",
    labelnames=("source",),
    # Buckets cover sub-second LLM-only paths up to the 120 s dispatch
    # budget. Anything past 120 s is unexpected and ends up in +Inf.
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

DISPATCH_ATTEMPTS_TOTAL: Final = Counter(
    "dispatch_attempts_total",
    "HTTP dispatch attempts to downstream targets, by target host and outcome.",
    labelnames=("target", "outcome"),
)

DLQ_EVENTS_TOTAL: Final = Counter(
    "dlq_events_total",
    "Events shunted to the dead-letter queue, by source.",
    labelnames=("source",),
)


def host_from_url(url: str) -> str:
    """Extract a stable host label from a URL.

    We label by hostname so cardinality stays bounded — a per-path label
    would explode when downstream URLs include IDs.
    """
    parsed = urlparse(url)
    return parsed.netloc or "unknown"
