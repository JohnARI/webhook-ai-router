# n8n integration

`examples/n8n-workflow.json` is a minimal workflow that POSTs a signed
HubSpot-shaped payload to a locally-running webhook-ai-router. Use it as a
template — copy the **Build signed request** node into any production
workflow that needs to deliver to this service.

The workflow has three nodes:

1. **Manual Trigger** — fires when you click *Test workflow*.
2. **Build signed request** — a Code node that computes
   `HMAC-SHA256("{timestamp}.{raw_body}", $SECRET)` and a fresh UUID
   `Idempotency-Key`. It does *not* parse the body before signing — the
   exact bytes that get hashed are the ones that get sent.
3. **POST /webhooks/hubspot** — an HTTP Request node that forwards the
   request with `Content-Type: application/json`, `X-Signature`,
   `X-Timestamp`, and `Idempotency-Key`.

## Import the workflow

1. Start the service locally: `docker compose up -d` from the repo root.
2. Open n8n (cloud or self-hosted).
3. **Workflows → New** → click the **⋯** menu (top right) → **Import from File**.
4. Select `examples/n8n-workflow.json`.

## Configure the secret + URL

The Code node reads from environment variables when present, falling back
to the dev defaults:

| Variable                  | Default                                  | What it does                                          |
| ------------------------- | ---------------------------------------- | ----------------------------------------------------- |
| `HUBSPOT_WEBHOOK_SECRET`  | `ci-test-secret`                         | HMAC shared secret. Must match `.env` on the service. |
| `WEBHOOK_ROUTER_URL`      | `http://localhost:8000/webhooks/hubspot` | Endpoint the HTTP node POSTs to.                      |

Two ways to set them:

- **Recommended (n8n self-hosted)**: pass them to the n8n container,
  e.g. `docker run -e HUBSPOT_WEBHOOK_SECRET=… -e WEBHOOK_ROUTER_URL=… n8nio/n8n`.
- **Quick demo**: edit the `SECRET` and `TARGET_URL` constants at the top
  of the Code node and save.

If n8n is running in the same Docker network as webhook-ai-router (e.g.
both are services in the same `docker compose` file), use
`http://app:8000/webhooks/hubspot` so the two containers reach each other
by service name instead of `localhost`.

## Run it

Click **Test workflow**. You should see the HTTP node return:

```json
{
  "event_id": "0d7c…",
  "status": "queued"
}
```

with HTTP 202. The **Build signed request** node mints a fresh
`Idempotency-Key` on every run, so each click enqueues a new event.

## What to expect on failure

| Symptom                                            | Likely cause                                                                                                              |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `401 Invalid webhook signature`                    | `HUBSPOT_WEBHOOK_SECRET` mismatch with `.env`.                                                                            |
| `401 Webhook timestamp expired`                    | Workflow ran more than 5 minutes after the timestamp was minted (queued or paused). Re-run to mint a fresh timestamp.     |
| `400 Idempotency-Key header is required`           | The HTTP Request node's headers were edited; re-import the workflow.                                                      |
| `409 Concurrent request with same Idempotency-Key` | A previous run is still in-flight; wait a second or click again to mint a fresh key.                                      |
| Connection refused                                 | Service not running, or `WEBHOOK_ROUTER_URL` points at the wrong host (use the docker service name, not `localhost`).     |

## Production swap-in

For real HubSpot deliveries, replace the **Manual Trigger** with a
**Webhook** node, expose its public URL to HubSpot, and have the Code
node forward the inbound body verbatim instead of synthesising one. The
signing logic and HTTP Request node are unchanged.
