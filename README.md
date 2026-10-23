# effortlesspromo-dca — Data Concierge Agent

Per-tenant, India-only prospect **data-acquisition service** for EffortlessPromo. Standalone
FastAPI service with its own database, workers and scheduler (polyrepo rule: no shared code, no
shared database — HTTP contracts only). All model execution is delegated to the existing
`effortlesspromo-ai` gateway over authenticated HTTP; this service holds **no provider SDKs/keys**.

Contract inventory & blockers: `docs/DCA-001-integration-contracts.md`.
Product/design brief: `../../docs/research/data-acquisition-agent/AGENT-DEVELOPMENT-PROMPT.md`.

## What exists (foundation increment)

- FastAPI app (`app/main.py` factory, `app/asgi.py` entrypoint), fail-fast config
  (`EFFORTLESSPROMO_DCA_*`, see `.env.example`).
- Inbound auth: `X-Service-Auth` shared secret, fail-closed (401 when unconfigured) —
  authenticates the calling *service* only; tenant authority is the platform's job (DCA-001 B3).
- Own schema (SQLAlchemy 2): `tenant_agents`, `icp_profiles`, `discovery_runs`,
  `prospect_candidates`, `acquisition_decisions`, `ai_invocations`.
- Discovery cycle (deterministic): plan → fetch → **rulepack screening** (India pack:
  official-registry tier only, role contacts only, org identifier mandatory; DEFAULT pack = allow
  nothing) → dedupe → suggested candidates, every record getting an auditable
  `AcquisitionDecision` reason code.
- Sources: **synthetic India registry fixtures only** (`synthetic.mca`), and even those are off
  until `EFFORTLESSPROMO_DCA_SYNTHETIC_SOURCES_ENABLED=true`. No live source exists yet.
- 24/7 worker: asyncio ticker over enabled tenants, **off by default** (`WORKER_ENABLED`).
- AI gateway client honouring the recorded contract: `POST /v1/{capability}`, `X-Service-Auth` +
  deterministic `X-Request-Id`, 200-with-`status=failed` passthrough, 5xx/transport →
  retryable `AiServiceUnavailable`, `dryRun` support. Every call is durably recorded in
  `ai_invocations` with DCA-namespaced ids (`dca:<uuid>`) — resolves blockers B2/B4 locally.
  **Nothing calls the gateway yet**: AI-assisted planning/scoring arrives with an explicit
  AI-capability story.
- Candidate lifecycle: suggested → accepted/dismissed/expired. **Acceptance is not recipient
  consent** and places nothing into platform audiences (platform-side export is a later story,
  blocked on DCA-001 B1 — no service-to-service path into the .NET API exists).

## Run

```bash
pip install -r requirements-dev.txt
pytest                                   # in-memory SQLite, no network
uvicorn app.asgi:app --reload --port 8100
```

Minimum env: `EFFORTLESSPROMO_DCA_ENVIRONMENT`, `EFFORTLESSPROMO_DCA_DATABASE_URL`,
`EFFORTLESSPROMO_DCA_SERVICE_AUTH_TOKEN` (else every privileged call 401s).
PostgreSQL: `pip install .[postgres]` and use `postgresql+psycopg://...`.

## Deliberate deferrals

- **No Alembic yet** — schema is created via `auto_create_schema` (dev/test). First production
  deploy must introduce migrations and set `EFFORTLESSPROMO_DCA_AUTO_CREATE_SCHEMA=false`.
- Suppression/objection registry, Art-14-style notification, platform export, web/admin UI,
  AI capabilities, live MCA/GSTIN adapters: later stories (several blocked — see B1/B5 in
  `docs/DCA-001-integration-contracts.md`).
