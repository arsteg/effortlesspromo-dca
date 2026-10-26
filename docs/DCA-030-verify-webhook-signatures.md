# DCA-030 — Verify webhook signatures

**Status:** implemented. Adds **transport authentication** to the inbound integration-event path:
HMAC-SHA256 **signature verification**, a **timestamp-tolerance** replay guard, and **key-rotation
overlap** — the gate a receiver runs **first**, before it parses the DCA-029 envelope or touches any
state. No event-specific business behavior; no DB write, no transport, no other service, no UI, no
AI-service internals. Fail-closed and OFF by default.

## What it does

`app/integration/webhook.py` defines `verify_webhook(...)` — the single sanctioned transport-auth
entry point — plus `WebhookKeyring` / `WebhookKey` and the producer-side `sign_body(...)` so a test
(and any future sender) share one scheme. It is a **pure function**: no session, no write, no I/O, so a
rejection happens strictly before anything is mutated.

Signature header format (compact, order-independent `name=value` list):

```
t=<unix_ts>,k=<key_id>,v1=<hex_hmac_sha256_over "<t>.<raw_body>">
```

The producer signs the **raw body** with HMAC-SHA256 keyed by a shared secret; the timestamp is bound
into the signed content (`<t>.<body>`) so an attacker cannot rewrite `t` to dodge the replay window.
`k` is an operator hint only — verification tries **every** active key regardless.

`verify_webhook` runs in a deliberate order:

1. **fail-closed** if no keyring is configured (`keyring=None` ⇒ `WebhookNotConfiguredError`);
2. parse the header (missing/garbled ⇒ `MalformedSignatureError`; header length and signature count
   are capped first);
3. **timestamp tolerance** (stale/future beyond `±tolerance_seconds` ⇒ `StaleWebhookError`) — checked
   **before** the signature, so a validly-signed but expired/replayed event is refused regardless of
   how it was signed;
4. **signature** against every active key in constant time (`hmac.compare_digest`); none match ⇒
   `SignatureMismatchError`. Checking all keys is what lets a rotation overlap accept the old or the
   new key.

On success it returns a `VerifiedWebhook(key_id, timestamp)` carrying nothing sensitive (safe to
audit).

### Key rotation

`WebhookKeyring` is an ordered set of active keys; the first is *current* (signs new traffic), every
key verifies. Rotation = add the new key as current, keep the old key in the ring through an
**overlap** until every in-flight producer has moved, then drop it — never a flag-day cutover that
would drop a legitimately-signed event. `WebhookKeyring.from_settings` builds a 1- or 2-key ring
(current + optional previous) from config.

### Fail-closed config

All settings are OFF/empty by default (`app/core/config.py`): `webhook_verification_enabled=False`,
no `webhook_signing_secret`. `from_settings` returns `None` (verification disabled) unless enabled
**and** a current secret is present, so an unconfigured or half-configured deployment never verifies
open. `validate_required` additionally refuses an enabled-but-secretless deploy, a negative tolerance,
and a previous secret with no current one. The transport itself is wired into **no** live route — a
later, still-disabled capability.

### No sensitive value echoed

Every rejection raises a `WebhookAuthError` subclass with a **shape-only** message — the tolerance (a
bound), the *kind* of malformed field, or a size — never the body, the submitted timestamp, a
signature, or (above all) a signing secret.

## Tests

`tests/test_webhook_signature.py`, two layers:

```
# always-on unit tests (no database) — the transport-auth contract + every rejection + no-echo:
pytest tests/test_webhook_signature.py

# "rejected before mutation" on a disposable PostgreSQL (DCA-028 receipt untouched by rejections):
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55475:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55475/dca"
pytest tests/test_webhook_signature.py
```

The unit layer pins: a validly-signed body is accepted; a tampered body / single flipped byte / wrong
key fails; a stale or far-future timestamp fails even when validly signed (and staleness fires before
the signature); the tolerance boundary is accepted; a missing/garbled/oversized/too-many-signatures
header is rejected; an unconfigured keyring fails closed; a **rotation overlap accepts both the old and
the new key** while the retired key is rejected once the overlap closes; `from_settings` builds the
right ring; and **no rejection message echoes the body, timestamp, signature or secret**. The Postgres
layer runs a guarded receiver (verify → parse envelope → record a DCA-028 inbox receipt) and asserts a
tampered and a stale event leave **no row** (exactly one receipt — the valid event), and that an
overlap records both old-key and new-key events. No migration is needed (no schema change).

Quality gate (all clean): `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Manual check

```
python scripts/manual_check.py DCA-030
```

For synthetic Business A, a guarded receiver verifies the signature then records a DCA-028 inbox
receipt only on success: (1) a valid current-key fresh event is recorded; (2) a tampered body and (3)
a stale timestamp are each rejected before any mutation; (4) during a rotation overlap both an old-key
and a new-key event are recorded. It prints the persisted `inbox_receipts` rows (expect exactly three
— valid + old-key + new-key), each rejection message, a check that no signing secret appears in any
rejection, and the unconfigured-keyring fail-closed path. Local synthetic SQLite only; no AI/paid
action; the ephemeral DB is cleaned up.

## Boundary

Inside the DCA only: new `app/integration/webhook.py` (+ exports from `app/integration/__init__.py`),
new config keys in `app/core/config.py`, focused tests, this doc, and the DCA-030 manual-check
scenario. No model/migration (pure verification, no schema change), no other service, no UI, no
AI-service internals. The verifier is **not** wired into the sinks or any live inbound route — that
remains a separate, disabled capability. It is a ready, independently reviewable transport-auth
capability. Future capabilities stay disabled.
