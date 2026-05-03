#!/usr/bin/env bash
# Smoke tests for `POST /webhooks/{source}` running against a local
# `docker compose up` instance.
#
# Five canonical cases:
#   1. happy path                 → 202 {status: "queued"}
#   2. duplicate Idempotency-Key  → 202 cached body, no second enqueue
#   3. bad signature              → 401 application/problem+json
#   4. expired timestamp          → 401 application/problem+json
#   5. missing Idempotency-Key    → 400 application/problem+json
#
# The HMAC matches `core/security.py:verify_hmac` — SHA-256 of
# "{timestamp}.{raw_body}", lowercase hex, sent in `X-Signature`.

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SECRET="${HUBSPOT_WEBHOOK_SECRET:-ci-test-secret}"

# A canonical HubSpot-shaped body. Whitespace matters — we sign these exact
# bytes and send them as-is.
BODY='[{"eventId":1,"subscriptionType":"contact.creation","portalId":12345,"objectId":999,"occurredAt":1700000000000}]'

# --- helper -------------------------------------------------------------

# sign <timestamp> <body> -> hex digest
#
# `awk '{print $NF}'` extracts the hex regardless of openssl's output
# shape: legacy LibreSSL prints `(stdin)= <hex>`, modern OpenSSL 3.x can
# print either `HMAC-SHA2-256(stdin)= <hex>` or just `<hex>`. Field $NF
# is always the hash itself.
sign() {
  local ts="$1"
  local body="$2"
  printf '%s.%s' "$ts" "$body" \
    | openssl dgst -sha256 -hmac "$SECRET" \
    | awk '{print $NF}'
}

post() {
  local label="$1"
  shift
  echo "─── ${label} ─────────────────────────────────────────────"
  curl --silent --show-error \
       --write-out '\nHTTP %{http_code}\n' \
       --output - \
       "$@"
  echo
}

NOW="$(date +%s)"

# --- 1. happy path ------------------------------------------------------

echo "== 1. Happy path: signed request, fresh Idempotency-Key =="
KEY1="$(uuidgen | tr '[:upper:]' '[:lower:]')"
SIG1="$(sign "$NOW" "$BODY")"
post "1. happy path" \
  -X POST "${BASE_URL}/webhooks/hubspot" \
  -H "Content-Type: application/json" \
  -H "X-Signature: ${SIG1}" \
  -H "X-Timestamp: ${NOW}" \
  -H "Idempotency-Key: ${KEY1}" \
  --data-binary "$BODY"

# --- 2. duplicate Idempotency-Key --------------------------------------

echo "== 2. Same Idempotency-Key replayed: cached body, same event_id =="
NOW2="$(date +%s)"
SIG2="$(sign "$NOW2" "$BODY")"
post "2. duplicate idempotency-key" \
  -X POST "${BASE_URL}/webhooks/hubspot" \
  -H "Content-Type: application/json" \
  -H "X-Signature: ${SIG2}" \
  -H "X-Timestamp: ${NOW2}" \
  -H "Idempotency-Key: ${KEY1}" \
  --data-binary "$BODY"

# --- 3. bad signature ---------------------------------------------------

echo "== 3. Bad signature: 401 + RFC 7807 problem+json =="
NOW3="$(date +%s)"
post "3. bad signature" \
  -X POST "${BASE_URL}/webhooks/hubspot" \
  -H "Content-Type: application/json" \
  -H "X-Signature: 00000000000000000000000000000000000000000000000000000000deadbeef" \
  -H "X-Timestamp: ${NOW3}" \
  -H "Idempotency-Key: $(uuidgen)" \
  --data-binary "$BODY"

# --- 4. expired timestamp ----------------------------------------------

echo "== 4. Expired timestamp (1 hour old): 401 =="
OLD_TS=$(( $(date +%s) - 3600 ))
SIG4="$(sign "$OLD_TS" "$BODY")"
post "4. expired timestamp" \
  -X POST "${BASE_URL}/webhooks/hubspot" \
  -H "Content-Type: application/json" \
  -H "X-Signature: ${SIG4}" \
  -H "X-Timestamp: ${OLD_TS}" \
  -H "Idempotency-Key: $(uuidgen)" \
  --data-binary "$BODY"

# --- 5. missing Idempotency-Key ----------------------------------------

echo "== 5. Missing Idempotency-Key header: 400 =="
NOW5="$(date +%s)"
SIG5="$(sign "$NOW5" "$BODY")"
post "5. missing idempotency-key" \
  -X POST "${BASE_URL}/webhooks/hubspot" \
  -H "Content-Type: application/json" \
  -H "X-Signature: ${SIG5}" \
  -H "X-Timestamp: ${NOW5}" \
  --data-binary "$BODY"

echo "All examples ran."
