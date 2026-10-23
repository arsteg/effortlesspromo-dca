# effortlesspromo-dca — quick reference

Data Concierge Agent: standalone Python/FastAPI service (own DB, workers, scheduler) that acquires
prospect data per tenant, India-only. Read `docs/DCA-001-integration-contracts.md` before wiring
anything to another app — it records the real contracts and the open blockers (B1–B6).

## Build & test
- `pip install -r requirements-dev.txt` · `pytest` (in-memory SQLite, no network, no Docker).
- Run: `uvicorn app.asgi:app --reload --port 8100`. Env prefix `EFFORTLESSPROMO_DCA_` (see `.env.example`).
- `ruff check` manually (line-length 100, same as effortlesspromo-ai).

## Hard boundaries (violations fail review)
- All model execution via the existing effortlesspromo-ai gateway over HTTP (`app/clients/ai_client.py`
  + `app/services/ai_invoker.py`). **No provider SDKs/keys, no model routing, no second AI gateway.**
  New AI capabilities are added to effortlesspromo-ai only by an explicit AI-capability story.
- Never touch another service's database. This service's DB is its own.
- Tenancy: every table/query is `business_id`-scoped; by-id reads must check ownership (404, never
  cross-tenant). The `X-Service-Auth` gate authenticates the caller service, NOT a tenant (B3).
- Compliance is data: `app/compliance/rulepacks.py` (IN pack + DEFAULT=allow-nothing fallback).
  Screening is deterministic Python — never AI. Every raw candidate gets an `AcquisitionDecision`.
- Candidate acceptance ≠ recipient consent; no platform export exists yet (B1).
- Everything risky is config-gated OFF by default: worker loop, synthetic sources. Live sources
  don't exist; adding one is its own story with rulepack + rights review.

## Gotchas
- `app/main.py` is a side-effect-free factory; uvicorn entrypoint is `app/asgi.py`. Tests build
  apps with explicit `Settings` (`tests/conftest.py:make_settings`).
- SQLite returns naive datetimes — normalize tz before comparing (see `tenants.py` accept route).
- AI calls: persist the `AiInvocation` row first; retry timeouts with the SAME `X-Request-Id`
  (AI-side idempotency cache is in-memory, TTL 24h — DCA rows are the durable record).
- No Alembic yet: `auto_create_schema` creates tables; introduce migrations before prod.
