# webhook-ai-router

[![CI](https://github.com/JohnARI/webhook-ai-router/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/JohnARI/webhook-ai-router/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/badge/coverage-80%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

> Production-grade ingestion service for incoming webhooks. Authenticates, deduplicates, enriches via LLM, and dispatches downstream — with idempotency, retries, and a dead-letter queue. Built to sit between webhook sources (HubSpot, Stripe, Calendly…) and no-code workflows (n8n, Make).

---

## Why this exists

No-code automation tools (n8n, Make) are excellent for orchestration but become fragile when handling:

- High-throughput webhooks (rate limits, race conditions, duplicate deliveries)
- Cryptographic signature verification with replay protection
- LLM-based enrichment as part of the ingestion pipeline
- Reliable fan-out to multiple downstream systems with per-target retry policies
- Persistent state, audit trails, and dead-letter handling

`webhook-ai-router` handles those concerns in a small, focused Python service. Your no-code workflow only sees clean, deduplicated, enriched events.

---

## Architecture

![architecture-diagram](docs/assets/architecture-diagram.png)

**Request flow:**

![request-flow-diagram](docs/assets/request-flow-diagram.png)

---

## Quickstart

```bash
git clone https://github.com/JohnARI/webhook-ai-router.git
cd webhook-ai-router
cp .env.example .env   # fill in HUBSPOT_WEBHOOK_SECRET + your LLM provider key (see below)
docker compose up
```

Service is up on `http://localhost:8000`. API docs at `/docs`.

### Pick an LLM provider

The worker picks its classifier from `LLM_PROVIDER`. Only the active
provider's key needs to be set:

| Provider  | `LLM_PROVIDER` value   | Default model       | API key var         |
| --------- | ---------------------- | ------------------- | ------------------- |
| Anthropic | `anthropic` (default)  | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| Gemini    | `gemini`               | `gemini-2.5-flash`  | `GEMINI_API_KEY`    |

Gemini Flash is cheaper/faster — that's the reason to pick it. For
parity-on-quality with Sonnet 4.6 set `GEMINI_MODEL=gemini-2.5-pro`.

### Send a test webhook

```bash
bash examples/curl-examples.sh
```

Five canonical cases: happy path, replay (cached), bad signature, expired timestamp, missing `Idempotency-Key`. Each prints the HTTP status and body so you can verify against [`src/webhook_ai_router/api/routes/webhooks.py`](src/webhook_ai_router/api/routes/webhooks.py).

---

## Calling from n8n

In your n8n **HTTP Request** node:

| Field   | Value                                                                                                  |
| ------- | ------------------------------------------------------------------------------------------------------ |
| Method  | `POST`                                                                                                 |
| URL     | `http://your-host/webhooks/{source}`                                                                   |
| Headers | `X-Signature: <hex>`, `X-Timestamp: <unix>`, `Idempotency-Key: <uuid>`                                 |
| Body    | Raw JSON event payload                                                                                 |

`X-Signature` is the lowercase hex of `HMAC-SHA256("{X-Timestamp}.{raw_body}", $HUBSPOT_WEBHOOK_SECRET)` — no `sha256=` prefix. The exact bytes you sign must be the bytes you POST.

The service responds `202 Accepted` with `{event_id, status: "queued"}` immediately. Enrichment + dispatch happen asynchronously in the worker.

A ready-to-import workflow is provided in [`examples/n8n-workflow.json`](examples/n8n-workflow.json) — see [`docs/n8n-integration.md`](docs/n8n-integration.md) for import steps and the production swap-in.

---

## Engineering choices

This repository is intentionally a reference implementation. Each pattern is included because it solves a class of production failure I have seen in the wild. Five load-bearing decisions are documented in [`docs/architecture.md`](docs/architecture.md); a quick tour:

- **Async-first**: FastAPI + `httpx.AsyncClient` + async SQLAlchemy + arq workers. No mixed sync/async traps.
- **Pydantic v2 throughout**: strict request/response models with discriminated unions per webhook source, never shared with SQLAlchemy ORM models.
- **HMAC verification**: constant-time comparison via `hmac.compare_digest`, plus a 5-minute timestamp window for replay protection ([`core/security.py`](src/webhook_ai_router/core/security.py)).
- **Idempotency-Key**: Stripe-style. Cached responses returned for duplicate `Idempotency-Key` headers within 24h, with a Redis lock to prevent concurrent processing ([`core/idempotency.py`](src/webhook_ai_router/core/idempotency.py)) and a Postgres unique constraint as defense-in-depth.
- **Retry with backoff**: `tenacity` with `wait_random_exponential(max=30)` + `stop_after_delay(120)` per target — a wall-clock budget, not a fixed attempt count ([`services/dispatch.py`](src/webhook_ai_router/services/dispatch.py)).
- **Dead-letter queue**: events that exhaust the 5-attempt arq retry budget get a row in `dead_letter_events` (FK to the original `WebhookEvent`, final error, retry count) and the exception is swallowed so arq stops retrying ([`workers/tasks.py`](src/webhook_ai_router/workers/tasks.py)).
- **Structured logging**: `structlog` with JSON output in prod, console in dev, `request_id` propagated through async tasks. Raw payloads are never logged.
- **Observability**: `/healthz`, `/readyz`, and Prometheus `/metrics` endpoints out of the box, with custom counters/histograms for received events, processing time, dispatch attempts, and DLQ rate ([`core/metrics.py`](src/webhook_ai_router/core/metrics.py)).
- **RFC 7807 error responses**: `application/problem+json`, never free-form strings ([`schemas/errors.py`](src/webhook_ai_router/schemas/errors.py)).

---

## Project structure

```text
src/webhook_ai_router/
├── api/         # FastAPI routes, middleware, dependencies
├── core/        # Cross-cutting: security, idempotency, logging, exceptions, metrics
├── schemas/     # Pydantic models (request/response/errors/dispatch/enrichment)
├── services/    # LLM client, dispatch, ingest parsing, event repository
├── workers/     # arq task definitions + worker entrypoint
├── db/          # SQLAlchemy 2.0 async models and session
└── infra/       # Redis client, arq pool factory
alembic/         # async Alembic migrations
```

---

## Development

```bash
make install   # uv sync + pre-commit install
make run       # local dev server with reload
make test      # pytest with coverage (gate: 80%)
make lint      # ruff check + mypy --strict
make fmt       # ruff format + ruff check --fix
make migrate   # alembic upgrade head
```

Pre-commit hooks run ruff and mypy on every commit. CI runs the full suite on every push: lint, mypy, alembic migrate against a real Postgres, pytest with the 80% coverage gate, plus a Docker image smoke build.

---

## Roadmap

- [ ] `GET /events/{id}` — query event status / enrichment / dispatch results by ID
- [ ] DLQ replay endpoint and ad-hoc reprocess CLI
- [ ] Make.com integration example (`examples/make-scenario.md`)
- [ ] OpenTelemetry tracing
- [ ] Pluggable enrichment providers (Anthropic, OpenAI, local models)
- [ ] Per-source rate limiting
- [ ] Web UI for DLQ inspection and replay

---

## License

MIT
