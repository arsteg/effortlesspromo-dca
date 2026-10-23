# DCA-001 — Existing integration contracts (read-only inventory)

**Status:** documentary review artifact. No application code changed. All claims cite actual repository files (paths relative to `C:\Work\EffortlessPromo\apps\` unless noted). Verified 2026-10-23.

---

## 1. DCA location

- `effortlesspromo-dca/` **already exists** as a fresh standalone git repo (own `.git`, `origin` remote, branch `main`) containing only `README.md` ("Data Concierge Agent") and `.gitignore`. No FastAPI app, database, workers, or scheduler exist yet.
- It is **separate from** `effortlesspromo-ai` (confirmed: no DCA files under `effortlesspromo-ai/`). The polyrepo boundary (one repo per app, HTTP-only contracts — workspace `CLAUDE.md` §2, ADR-0005) is preserved by building here.

## 2. effortlesspromo-ai contracts (the service DCA will call)

### 2.1 Authentication — service identity only, NO tenant authorization
- `app/core/security.py:19-25` — single shared-secret header `X-Service-Auth`, compared with `hmac.compare_digest`, **fail-closed** (no token configured → 401 for every privileged call).
- `app/core/security.py:4-6` (docstring) — explicit: *"The AI Service performs NO end-user authorization (that is the API's job, BR-004); this is purely a caller-identity gate."*
- **Consequence for DCA (acceptance-critical):** possession of the shared secret authenticates *the service*, not any tenant. `businessId` in the envelope is **caller-asserted and unverified** by the AI service — it drives budget attribution (`gateway.py:213`, `:249`) and derived-write ledger rows (`gateway.py` via `/ai/gateway/derived-writes`). The shared secret must therefore never be treated as authority to invoke for arbitrary business IDs; tenant authorization must be enforced by the caller (today: the .NET API's tenancy layer; tomorrow: DCA's own tenant scoping). **This is a DCA-side obligation, not an inherited guarantee.**

### 2.2 Invocation envelope
- Canonical models: `app/gateway/envelope.py` — `InvocationRequest` (`:25-39`): `jobId`, `capability`, `businessId`, `correlationId`, `inputContext`, `modelHint`, `promptVersion`, `untrusted[]` (`UntrustedItem {source, content}`, `:13-22` — external text framed as data, never instruction), `dryRun`. `InvocationResponse` (`:79-105`): `status` (completed|failed), `structuredOutput`, `confidence`, `modelProvider/modelId/promptVersion`, `tokenUsage`, `costEstimate {amount, currency, estimated, pricingUnavailable}`, `validationStatus/validationReport` (6-screen pipeline), `humanReviewRequired`, `dataCategories`, `taint/taintSources/injectionSuspected`, timings, `error {code, message, retryable}`.
- Routes (all behind `require_service_auth`):
  - `POST /v1/{capability}` + `GET /v1/{capability}/schema` — `app/api/routes/capabilities.py:20-60`. Body = envelope minus `capability` (path-supplied, `:40`). The schema endpoint returns input/output schemas, `acceptsUntrusted`, `activePromptVersion`.
  - `POST /ai/gateway/invoke` + `GET /ai/gateway/derived-writes` — `app/api/routes/gateway.py:14-50`.
  - Body-level failures return **HTTP 200 with `status=failed`** (`gateway.py:25` comment; mirrored by the .NET client, `HttpAiServiceClient.cs:65-66`).

### 2.3 jobId ownership — **unresolved contract for DCA**
- `envelope.py:28`: `jobId` is documented as *"API-owned AIJob id; echoed back for write-back correlation."* The durable AIJob record lives in the .NET API's Postgres.
- A DCA-originated invocation has no .NET `AIJob` row. Whether DCA mints its own job-id namespace, or must register invocations with the platform, is **undefined by any existing contract**. ➜ **Blocker to resolve in the AI-capability story; stays disabled until then.**

### 2.4 Idempotency
- Header `X-Request-Id` (`capabilities.py:38`, `gateway.py:21`). Replay within TTL returns the cached response with no second provider call/cost — `app/gateway/idempotency.py:1-6` (docstring, BR-024/BR-124).
- Store is **in-memory, per-process**, TTL default **24h** (`app/core/config.py:66` `idempotency_ttl_seconds = 24*60*60`), swept at 10k entries (`idempotency.py:16`). The docstring is explicit that *"the API remains the durable system of record for AIJob idempotency."* ➜ DCA must keep its **own durable** idempotency/spend records; AI-service replay protection does not survive its restarts.

### 2.5 Dry-run cost projection
- `app/gateway/gateway.py:194-199` — `dryRun=true` short-circuits **before any provider call/spend** (AI-025), returning a projected `costEstimate` with `estimated=true` (`envelope.py:51`). Usable by DCA for pre-flight budgeting without spend.

### 2.6 Budgets
- Server-side per-tenant budget check before any provider call: `gateway.py:208-221` (soft/hard stops; hard → error "tenant AI budget exhausted", non-retryable); spend recorded per `businessId` (`gateway.py:249`). Attribution is keyed on the **asserted** businessId (see §2.1).

### 2.7 Timeout reconciliation (caller side today)
- Reference caller `effortlesspromo-api/src/EffortlessPromo.Infrastructure/Services/HttpAiServiceClient.cs`: sends `X-Service-Auth` + optional `X-Request-Id` (`:40-44`); `HttpRequestException`/`TaskCanceledException` → `AiServiceUnavailableException` (`:49-54`); HTTP ≥500 likewise (`:56-59`). The HttpClient is default-constructed (`Infrastructure/DependencyInjection.cs:362`) ⇒ .NET default **100 s** timeout; no custom per-request timeout configured.
- Reconciliation pattern: retry with the **same `X-Request-Id`** → served from the idempotency cache within TTL (no double spend). Callers set deterministic request ids (e.g. `Conversations/ResponseManagement.cs:134` uses `conversationMessageId`; `:620` uses `draft:{conversationMessageId}`). Caveat as §2.4: cache is in-memory — a timeout followed by an AI-service restart cannot be reconciled from the AI side; the caller's durable job record is the recovery source. DCA must replicate this pattern (deterministic request ids + durable job rows).

### 2.8 Config knobs relevant to a new caller
- `app/core/config.py`: `service_auth_token` (`:63`, must equal the caller's configured secret — .NET side `AiService:ServiceToken`), `idempotency_ttl_seconds` (`:66`), `untrusted_max_chars` default 20 000 (`:76`). Env prefix `EFFORTLESSPROMO_`.

## 3. Platform (.NET API) contracts — identity, tenancy, CRM, billing, entitlement, suppression, delivery

### 3.1 How they exist today: **in-process interfaces, not HTTP service contracts**
The verified seams are C# interfaces consumed inside the modular monolith:
- **Identity/tokens:** `Application/Common/Interfaces/ITokenService.cs:11` (`ITokenService`), JWT validated by the `"Header"` auth scheme (`Api/Authentication/HeaderAuthenticationHandler.cs`; dev-only raw-GUID fallback disabled in Production — workspace `CLAUDE.md` §3).
- **Tenant authorization:** `Api/Tenancy/TenantResolutionMiddleware.cs:17` (`BusinessIdHeader = "X-Business-Id"`) — requires an active `Membership`, rechecks `User.Status` per request, computes permissions as union of roles; scope attributes in `Api/Tenancy/ScopeAttributes.cs`.
- **Suppression (canonical):** `Application/Compliance/SuppressionService.cs:43-49` — `ISuppressionService.CheckAsync(Guid? businessId, string identifier, string targetType, string channel, …, string? region, Guid? topicId)`; global + business + domain-level matching (CMP-003).
- **Delivery:** `Application/Sending/OutboundSendPipeline.cs:72` — `IOutboundSendPipeline` (impl `Infrastructure/Sending/OutboundSendPipeline.cs`, ADR-0008). The ONLY outbound path; gates produce an auditable `SendDecision`. **Acceptance of a prospect is not recipient consent** — consent/suppression gates re-evaluate at send time regardless of upstream acceptance.
- **Billing/payments:** `Application/Billing/BillingServices.cs:40` (`IPaymentProvider`); usage/overage metering in `Application/Billing/BillingAdvancedServices.cs` (BIL-016..027).
- **Entitlements:** `Application/Common/Interfaces/ITrustGate.cs:31` (`IPlanEntitlements`); feature-level gating pattern exemplified by `Application/SocialMarketing/SocialEntitlementGate.cs`.
- **CRM:** CQRS commands/queries under `Application/Crm/` exposed only through business-scoped controllers.

### 3.2 The two existing HTTP doors into the API — neither fits a platform service
1. **First-party user traffic:** `Authorization: Bearer <jwt>` + `X-Business-Id`, authorized via Membership (§3.1). A service has no user/membership.
2. **Developer public API:** `PublicApiKeyAuthenticationHandler` with `epk_live_`/`epk_test_` keys (`Domain/Entities/DeveloperApiKey.cs`, `Application/Developer/DeveloperApiKeyService.cs`, `Api/Controllers/DeveloperApiKeysController.cs`) — keys are **business-bound** (per-tenant), rate-limited, not a platform-wide service credential.

### 3.3 ➜ Missing contract (explicit blocker)
**There is no service-to-service authentication path into the .NET API** (nothing analogous to the AI service's `X-Service-Auth` exists on the API side), and therefore no HTTP contract through which a separate DCA service can invoke identity, CRM, billing, entitlement, suppression, or delivery functionality today. Any DCA story needing these (suppression screening, entitlement checks, CRM writes, Art-14-style notices through the send pipeline) is **blocked** until a dedicated, scoped service contract is designed and accepted. Until then those capabilities stay disabled in DCA.

## 4. UI ownership inventory (no code changed)

### 4.1 Web — `effortlesspromo-web/` (clients / public forms)
- Root confirmed; service layer `src/lib/api.ts` (typed `api` object → `request<T>()` fetch wrapper: JSON, `Authorization: Bearer`, 20 s AbortController timeout, ProblemDetails→`ApiError`; empty `NEXT_PUBLIC_API_URL` ⇒ mock mode via `src/lib/mock.ts`). Auth/session `src/lib/auth-context.tsx` (JWT in local/sessionStorage, silent refresh, `X-Business-Id` injection, `hasPermission()`), nav `src/lib/nav.ts`. Installed Next.js 16 docs present: `node_modules/next/dist/docs/index.md`; repo `AGENTS.md` is the auto-generated Next.js 16.3.1 warning file.
- **Ownership:** all client-facing DCA screens (ICP editor, suggestions inbox, accept/dismiss) belong here, behind the existing session + `X-Business-Id` tenancy.

### 4.2 Admin — `admin/effortlesspromo-admin/` (platform operators)
- Root confirmed; `src/lib/api.ts` + `mock.ts` (unscoped `/api/admin/...`, token only), nav/labels/types present.
- **Auth is a login-less MVP:** `src/lib/auth-context.tsx:7` — *"session with every platform.\* permission"*; token literally `"mock-admin-token"` (`:77`, env-overridable). 
- **Ownership:** operator-facing DCA consoles (source catalog, rulepacks, agent health) belong here. **Blocker for client-support stories:** the story requires client-support actions to carry *scoped, expiring, revocable authority with actor/tenant audit*, and states Admin role alone is insufficient — Admin currently has **no real authentication or roles at all**, so no such authority mechanism exists. Any DCA client-support surface stays disabled until real admin auth + scoped-authority support exist.

## 5. Blockers & unresolved contracts (summary)

| # | Contract | Status | Evidence |
|---|---|---|---|
| B1 | DCA → .NET API service auth (identity/CRM/billing/entitlement/suppression/delivery over HTTP) | **Missing** — stays disabled | §3.2–3.3 |
| B2 | jobId ownership for non-API callers of the AI gateway | **Undefined** | `envelope.py:28`, §2.3 |
| B3 | Tenant authorization for AI invocations is caller-owned; shared secret ≠ per-business authority | **DCA obligation** | `security.py:4-6,19-25`, §2.1 |
| B4 | Durable idempotency/spend reconciliation across AI restarts | **Caller-owned** (DCA must persist) | `idempotency.py:1-6`, §2.4, §2.7 |
| B5 | Admin scoped/expiring/revocable client-support authority | **Missing** (admin auth is mock) | `admin .../auth-context.tsx:7,77`, §4.2 |
| B6 | DCA service scaffold (FastAPI, own DB/schema, workers, scheduler) | **Not yet built** (empty repo) | §1 |

## 6. Manual verification walkthrough (for the reviewer)

1. AI envelope citation: open `effortlesspromo-ai/app/gateway/envelope.py` lines 25-39 and 79-105; compare with §2.2.
2. Tenant-authorization denial semantics: open `effortlesspromo-ai/app/core/security.py` — confirm (a) fail-closed 401 with no configured token, (b) the "NO end-user authorization" docstring; then `TenantResolutionMiddleware.cs:17` for where real tenant authz lives.
3. Proposed DCA directory: list `apps/effortlesspromo-dca/` — only `README.md` + `.gitignore` (+ `.git/`), confirming nothing was embedded into `effortlesspromo-ai`.
4. Unresolved contract: grep the .NET API for any service-auth inbound handler (none besides user JWT `HeaderAuthenticationHandler` and business-bound `PublicApiKeyAuthenticationHandler`) — confirms B1.

No application behavior was changed anywhere; this file is the only addition.
