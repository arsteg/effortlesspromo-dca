# DCA-002 — Service data ownership matrix

**Status:** documentary review artifact. No application behavior changed. Companion to
`DCA-001-integration-contracts.md` (contract citations and blockers B1–B6 referenced below).
Paths relative to `C:\Work\EffortlessPromo\apps\`. Verified against code 2026-10-23.

## 1. Ownership matrix — one authority per responsibility

| Responsibility | Sole authority | Evidence (actual code) | Others' relationship |
|---|---|---|---|
| Prospect discovery (runs, candidates, suggestions) | **DCA** | `effortlesspromo-dca/app/services/discovery.py`; tables `discovery_runs`, `prospect_candidates` (`app/db/models.py`) | Platform/Web read results via future contract (B1); never write |
| Acquisition provenance & screening decisions | **DCA** | `acquisition_decisions` table; `app/compliance/rulepacks.py` (deterministic, per-jurisdiction) | AI service never screens; platform may mirror later, DCA record is authoritative |
| DCA permissions (which business has an agent, enabled state, ICP) | **DCA** | `tenant_agents`, `icp_profiles` tables; routes `app/api/routes/tenants.py` | Registration asserts a platform business id; DCA cannot mint one (see §3) |
| DCA jobs & schedules (24/7 loop) | **DCA** | `app/workers/scheduler.py` (`worker_enabled`, interval in `app/core/config.py`) | Not the platform's Postgres job engine; no rows in the .NET `jobs` tables |
| AI budget **reservations/spend bookkeeping on DCA's side** | **DCA** | `ai_invocations` table (cost_amount/currency per call); `app/services/ai_invoker.py` | AI service separately enforces its own per-tenant budget — see next row |
| Per-tenant AI budget **enforcement** (soft/hard stop) | **AI service** | `effortlesspromo-ai/app/gateway/gateway.py:208-221` (`BudgetLedger`) | DCA records what it spent; it cannot raise or bypass the gateway's stop |
| Model execution, provider secrets, routing, prompts, output validation, retries, usage reporting | **AI service** | `gateway/gateway.py` (pipeline), `providers.py`, `prompts.py`, `validation.py`, `pricing.py`; keys only in its `Settings` (`core/config.py`) | DCA holds **zero** provider SDKs/keys — its only path is `app/clients/ai_client.py` → `POST /v1/{capability}` |
| Identity (users, memberships, tenant authorization) | **Platform (.NET API)** | `TenantResolutionMiddleware.cs`, `HeaderAuthenticationHandler.cs` (DCA-001 §3.1) | DCA/AI authenticate *services* only (`X-Service-Auth`); neither can authorize a tenant (B3) |
| Subscriptions/entitlements & billing | **Platform** | `IPlanEntitlements` (`ITrustGate.cs:31`), `Application/Billing/*` | DCA has no billing tables; metering DCA usage into platform billing = future story over B1 |
| CRM / audiences / the master prospect graph | **Platform** | `Application/Crm/*`, global Organisation/Contact masters | DCA candidates stay in DCA until an explicit, user-triggered export contract exists (B1). Acceptance ≠ consent (`tenants.py` accept route returns `consent: false`) |
| Message delivery, consent, suppression | **Platform** | `OutboundSendPipeline.cs` (ADR-0008), `ISuppressionService` (`SuppressionService.cs:43`) | DCA sends nothing, ever — no channel adapters exist in the repo |
| Client self-service UI & public forms | **Web** (`effortlesspromo-web`) | `src/lib/api.ts` (Bearer + `X-Business-Id`) | Future DCA screens (ICP editor, suggestions inbox) live here, behind existing sessions |
| Platform operations UI & delegated client support | **Admin** (`admin/effortlesspromo-admin`) | `src/lib/api.ts` (`/api/admin/...`) | Client-support actions blocked until scoped/expiring/revocable authority exists (B5 — admin auth is a mock) |
| Backend business rules for DCA | **DCA (Python)** | rulepacks, discovery, lifecycle transitions all server-side | Frontend validation is UX sugar only; Web/Admin never re-implement or replace DCA rules; no Node backend for DCA rules |

No responsibility above has two authorities. Where a second service *touches* a concern (e.g. DCA records AI cost), the row states which side is authoritative for what.

## 2. API boundaries — no shared database writes anywhere

- **DCA → AI service:** HTTPS only — `POST /v1/{capability}` with `X-Service-Auth` + `X-Request-Id` (`app/clients/ai_client.py`; contract DCA-001 §2). DCA never reads/writes the AI service's in-process stores; AI service has no database.
- **DCA → Platform:** **no path exists** (B1). When one is designed it will be HTTP with its own scoped credential — never a connection string into the platform Postgres. DCA's `database_url` must point at a DCA-owned database (`app/db/base.py` docstring enforces intent; config in `app/core/config.py`).
- **Web/Admin → DCA:** no direct path; UI stories reach DCA data via the platform once B1 is resolved (Web must keep exactly one API origin + tenancy model). Until then, DCA's API is ops/test-only.
- **AI service → DCA:** none; the AI service is called, never a caller.

## 3. What DCA explicitly CANNOT do (acceptance-criteria denials, with evidence)

1. **Cannot create accounts/businesses.** `POST /v1/tenants` (`tenants.py:register`) only stores a caller-supplied business id string in DCA's own `tenant_agents` table — there is no code path to the platform's `Businesses`/`Users` tables or endpoints (no platform client exists in the repo at all; grep: the only HTTP client is `ai_client.py`).
2. **Cannot send campaigns/messages.** No channel adapter, SMTP/SES/SMS code, or send endpoint exists in `effortlesspromo-dca`; delivery is platform-only via `OutboundSendPipeline`. Candidate "accept" is a local status change returning `"consent": false`.
3. **Cannot bypass the AI service.** No provider SDK/key in dependencies (`pyproject.toml`: fastapi/uvicorn/pydantic/httpx/sqlalchemy only); `ai_client.py` targets only the gateway base URL; with `ai_base_url` unset the client raises `AiDisabled` (`test_ai_client.py::test_disabled_without_config`).
4. **Cannot act cross-tenant through its API.** Every query filters `business_id`; by-id reads 404 on foreign rows (`tenants.py:_candidate`; proven by `test_discovery_api.py::test_tenant_isolation_candidate_ids_do_not_cross`).

## 4. Freshness expectations per boundary

| Data | Producer → consumer | Freshness contract |
|---|---|---|
| Discovery candidates/runs | DCA worker → DCA API readers | Eventual, worker-interval cadence (`worker_interval_seconds`, default 3600s); on-demand via `POST .../discovery/run` |
| Candidate validity | DCA | TTL = rulepack `retention_days` (IN: 30); expired candidates refuse acceptance (`tenants.py:accept_candidate`) |
| AI responses | AI service → DCA | Synchronous per call; idempotent replay window = AI-side TTL 24h **in-memory only** — DCA's `ai_invocations` is the durable record (B4) |
| Tenant authority & entitlements | Platform → DCA | **Must be re-checked per request at the platform edge** when B1 lands; DCA never caches platform authority (today it receives none — B3) |
| Audience handoff (accepted candidates → platform CRM) | DCA → Platform | Undefined until B1; design expectation: explicit user-triggered export, at-least-once with idempotency key, platform dedup gate authoritative on merge |

## 5. Trace: one discovery request end-to-end (sole authority at each hop)

1. User clicks "run discovery" in **Web** → Web calls **Platform** (`Bearer` + `X-Business-Id`) — *platform is sole authority on identity/tenant/entitlement*.
2. Platform forwards to **DCA** over the (future, B1) service contract — *DCA is sole authority on whether the agent is enabled, the ICP, which sources run, and every screening decision* (`discovery.py`, `rulepacks.py`).
3. If an AI capability is involved (future story), DCA calls the **AI service** via `ai_client.py` — *AI service is sole authority on provider choice, prompts, validation, and budget stops*; DCA records the durable invocation row.
4. User accepts a suggestion → status change inside **DCA** (`consent: false`).
5. Export to an audience → **Platform** CRM/dedup gate is sole authority on master records; any eventual send is gated solely by the platform's send pipeline + suppression + consent.
   Steps 2 and 5's transport does not exist yet (B1) — both stay disabled; nothing in DCA fakes them.

## 6. Reviewer walkthrough

1. Open this file + `DCA-001-integration-contracts.md`; spot-check two matrix rows against the cited files (e.g. `gateway.py:208-221` budget stop; `tenants.py` accept route).
2. Trace §5 and confirm each hop's authority column in §1.
3. Denials: confirm `effortlesspromo-dca` contains no platform HTTP client, no channel/send code, no provider SDKs (`pyproject.toml`), and run `pytest` — the isolation test passes without any new state.
