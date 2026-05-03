# Architecture decisions

Five load-bearing decisions, one paragraph each. Each links to the code
that enacts it. Read this before changing any of them — every choice
trades something and has been picked deliberately.

## 1. Idempotency-Key (caller-supplied) over payload-hash (server-derived)

The unique key for dedup is an `Idempotency-Key` header the caller
supplies, not a hash of the request body. Two failure modes argue for
this. First, retries from senders like HubSpot, Stripe, and n8n send a
correlation ID per logical event but may re-render the body byte-for-byte
differently across retries (whitespace, field ordering, signature
recalculation) — a body-hash would treat each retry as a brand new event
and double-process. Second, identical bodies *can* be legitimately
distinct events (two users hitting the same form a second apart), and a
body-hash collapses them. The cost is that we trust the caller to mint a
key per logical event; the mitigation is documenting the contract and
returning 400 when the header is missing
([routes/webhooks.py](../src/webhook_ai_router/api/routes/webhooks.py)).

## 2. Async-first across the whole stack

Every I/O boundary is async — FastAPI route handlers, `httpx.AsyncClient`
for outbound dispatch, `redis.asyncio` for the idempotency cache, async
SQLAlchemy with `asyncpg`, and arq for the worker queue. We never mix
sync and async sessions or sync HTTP libraries; doing so reliably
deadlocks under load (event-loop blocking via a sync `requests` call
while an async task awaits I/O on the same loop). The cost is a
slightly heavier toolbox (no `requests`, no `psycopg` sync driver) and
the discipline of `# Async or nothing` enforced in
[CLAUDE.md](../CLAUDE.md). The benefit is one mental model: every I/O is
awaited, every collaborator is an async client, and concurrent fan-out
to N dispatch targets is `asyncio.gather` instead of a thread pool.

## 3. Pydantic schemas and SQLAlchemy models never share classes

Two separate object hierarchies: Pydantic v2 models live in
[`src/webhook_ai_router/schemas/`](../src/webhook_ai_router/schemas/) and
describe wire formats (request bodies, response bodies, queue messages,
external API payloads); SQLAlchemy 2.0 ORM models live in
[`src/webhook_ai_router/db/models.py`](../src/webhook_ai_router/db/models.py)
and describe persisted rows. A single `EventRepository` in
[`services/events.py`](../src/webhook_ai_router/services/events.py) is
the only place that converts between them. Trying to use one class for
both — even via a "Pydantic model with `from_attributes=True`" — couples
wire shape to storage shape: every column rename leaks into the API,
every API field bloats the schema, and validation rules end up living in
two places that disagree. The duplication is the point.

## 4. Tenacity retry budget is a wall-clock cap, not a fixed attempt count

Per-target dispatch retries use
`tenacity.AsyncRetrying(wait=wait_random_exponential(max=30),
stop=stop_after_delay(120))`
([services/dispatch.py](../src/webhook_ai_router/services/dispatch.py))
— retries until 120 s of total wall-clock have elapsed since the first
attempt, regardless of how many attempts that is. A fixed
`stop_after_attempt(N)` would either over-retry slow downstreams (5
attempts × 30 s back-off = 2.5 minutes blocking the worker) or
under-retry fast downstreams (5 attempts in 5 seconds when we had budget
to spare). 5xx responses raise `TransientHTTPError` so the same retry
predicate catches them; 4xx returns a `DispatchResult(success=False)`
immediately because retrying doesn't change anything.

## 5. DLQ is a Postgres table, written on the final retry

When a worker exhausts its 5-attempt arq retry budget, we insert one row
into `dead_letter_events` and *swallow* the exception so arq stops
retrying
([workers/tasks.py](../src/webhook_ai_router/workers/tasks.py)). The
durable record of the failure is the DLQ row, not arq's failed-job key.
This is deliberate: arq's job result has a 1-hour TTL by default and is
keyed by job ID — fine for ops but useless three days later when someone
wants to know *which webhooks didn't make it*. The DLQ row carries the
original `WebhookEvent.id` as a foreign key, the final error string, and
the retry count, so reconciliation is a single `SELECT … JOIN
webhook_events`. Replay is an out-of-band script (not yet built —
flagged as a TODO in [tasks.py](../src/webhook_ai_router/workers/tasks.py))
that re-enqueues by event_id; the queue-level dedup on
`_job_id=event_id` keeps replays from double-dispatching.
