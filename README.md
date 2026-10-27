# effortlesspromo-dca — Data Concierge Agent

Per-tenant, India-only prospect **data-acquisition service** for EffortlessPromo. Standalone
FastAPI service with its own database, workers and scheduler (polyrepo rule: no shared code, no
shared database — HTTP contracts only). All model execution is delegated to the existing
`effortlesspromo-ai` gateway over authenticated HTTP; this service holds **no provider SDKs/keys**.

Contract inventory & blockers: `docs/DCA-001-integration-contracts.md`.
Product/design brief: `../../docs/research/data-acquisition-agent/AGENT-DEVELOPMENT-PROMPT.md`.

## What exists (foundation increment)

- FastAPI app (`app/main.py` factory, `app/asgi.py` entrypoint), fail-fast config
  (`EFFORTLESSPROMO_DCA_*`, see `.env.example`). Typed runtime settings cover environment,
  the DCA-owned database + `db_schema`, `deployment_mode` (`fake` default | `live`),
  `secret_source` (+ `*_secret_ref` references — values stay out-of-band), and the
  effortlesspromo-ai base URL / token references. **Production rejects `fake` mode** and every
  fail-open default (missing inbound token, synthetic sources on, `auto_create_schema` on);
  AI, when its base URL is set, requires its service token. **No model-provider API key exists
  here** — those belong only to effortlesspromo-ai. Validation errors name the offending key only,
  never a value.
- Inbound auth: `X-Service-Auth` shared secret, fail-closed (401 when unconfigured) —
  authenticates the calling *service* only; tenant authority is the platform's job (DCA-001 B3).
- Own schema (SQLAlchemy 2): `tenant_agents`, `icp_profiles`, `discovery_runs`,
  `prospect_candidates`, `acquisition_decisions`, `ai_invocations`, `audit_events`,
  `outbox_messages`, `inbox_receipts`, `revision_projections`, `revision_pending_events`,
  `idempotency_records`, `business_profile_drafts`.
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
- Platform HTTP transport (DCA-032, `app/clients/platform_transport.py`): a hardened, generic
  transport **mechanism** for calling the .NET platform API — single allowlisted origin, mandatory
  scoped credential (redacted in logs), explicit timeouts, correlation ids, stable/safe error
  mapping. **Off by default** (needs base URL + credential) and **not wired to any route**; the
  business adapters built on it stay disabled until B1 ships. See `docs/DCA-032-*.md`.
- Outbox publisher (DCA-034, `app/outbox/publisher.py`): the idempotent relay that drains PENDING
  outbox messages (DCA-027) through the DCA-032 transport, keyed by each message's **stable** event
  id (the `Idempotency-Key`), so a lost acknowledgment is retried as the **same** logical event —
  never a fresh key. Only a 2xx ack marks a row `DELIVERED`; a timeout/unreachable/5xx/429 leaves it
  PENDING with its identity intact; a 4xx is a surfaced denial that also changes no state. **Off by
  default** (needs the transport + `platform_outbox_publish_path`); no live relay route is wired —
  driven against the DCA-033 fake server in tests. See `docs/DCA-034-*.md`.
- Request idempotency (DCA-035, `app/idempotency/`, `app/api/idempotency.py`): a write route carrying
  an `Idempotency-Key` header runs **at most once** — a byte-identical retry replays the first
  response (`Idempotent-Replay: true`) with no second state change; the same key reused for a
  different body is rejected (`409 idempotency_key_reuse`); keys never collide across tenants or
  routes. The stored record (`idempotency_records`, tenant-owned + RLS) is committed **atomically**
  with the business change, so a concurrent duplicate loses the unique-constraint race and replays
  the winner's result. Opt-in: no key ⇒ served normally. See `docs/DCA-035-*.md`.

- Database sessions: a **sync** engine backs the SQLite-friendly dev/test surface; an **async**
  engine (`app/db/base.py` `make_async_engine` / `transactional_session`, dependency
  `app/api/deps.py:get_async_session`) provides one session per request/task with an explicit
  transaction scope for the DCA-owned PostgreSQL — commit on success, rollback **before** the
  connection returns to the pool. Async is wired only for `postgresql*` URLs (reusing the `postgres`
  extra's psycopg3 driver); on SQLite the async dependency is fail-closed (503). The async
  transaction/isolation tests run against a disposable Postgres, never SQLite:

  ```bash
  pip install .[postgres]
  docker run --rm -d --name dca-pg -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55432:5432 postgres:16-alpine
  DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca" pytest tests/test_async_session.py
  ```

  (On Windows, psycopg3 async needs the selector event loop — the test module sets it; the Linux
  deployment is unaffected.)

## Run

```bash
pip install -r requirements-dev.txt      # dev loop (or: pip install .  — installable package)
pytest                                   # in-memory SQLite, no network
uvicorn app.asgi:app --reload --port 8100
effortlesspromo-dca                      # console entry point (after pip install .)
```

`requirements.lock` pins the full transitive runtime set (generated from a clean `pip install .`;
regenerate with `pip freeze` in a fresh venv when bumping dependencies).

### Quality commands

The dev extra (`pip install -r requirements-dev.txt`, or `pip install .[dev]`) installs the full
quality gate. Each command exits **nonzero on failure** (CI-ready); config lives in `pyproject.toml`.

```bash
ruff check .            # lint
ruff format --check .   # formatting gate (drop --check to auto-format)
mypy                    # type check (configured to scan the app/ package)
pytest                  # tests (in-memory SQLite, no network, no Docker)
```

Run all four in sequence before pushing:

```bash
ruff check . && ruff format --check . && mypy && pytest
```

Minimum env: `EFFORTLESSPROMO_DCA_ENVIRONMENT`, `EFFORTLESSPROMO_DCA_DATABASE_URL`,
`EFFORTLESSPROMO_DCA_SERVICE_AUTH_TOKEN` (else every privileged call 401s).
PostgreSQL: `pip install .[postgres]` and use `postgresql+psycopg://...`.

### Migrations (Alembic)

Alembic is configured against the **DCA-owned schema only** (`EFFORTLESSPROMO_DCA_DB_SCHEMA`,
default `dca`). `migrations/env.py` resolves the DB URL + schema from DCA settings (never from
`alembic.ini`) and refuses to migrate into a host-owned schema (`app/db/schema_guard.py` —
PostgreSQL system schemas, `public`, and platform names are rejected). Everything, including
`alembic_version`, lives in the DCA schema (the connection `search_path` is pinned to it).

```bash
pip install .[postgres]
EFFORTLESSPROMO_DCA_DATABASE_URL="postgresql+psycopg://…" alembic upgrade head
```

Production must run `alembic upgrade head` and set `EFFORTLESSPROMO_DCA_AUTO_CREATE_SCHEMA=false`
(the `auto_create_schema` metadata path remains for SQLite dev/test only).

### Seed test data (development only)

`effortlesspromo-dca seed` (or `python -m app.cli seed`) idempotently seeds two isolated synthetic
test businesses — **A** (`…00000000000a`) and **B** (`…00000000000b`) — each a `TenantAgent` plus a
synthetic `IcpProfile`. All data is fake (deterministic GUIDs, synthetic operator labels, synthetic
org identifiers `SYNTH-A/B-*`); **no real contacts or credentials**. Re-running is idempotent (row
counts and ids stay stable). The command **refuses under production** (`EFFORTLESSPROMO_DCA_ENVIRONMENT=production` → exit 1, no writes).

```bash
EFFORTLESSPROMO_DCA_ENVIRONMENT=development \
EFFORTLESSPROMO_DCA_DATABASE_URL="postgresql+psycopg://…" \
effortlesspromo-dca seed        # run twice → identical rows; config is the only input
```

### Manual-check runner (developer-controlled)

`python scripts/manual_check.py <STORY-ID>` runs one story's narrow, inspectable scenario against
**local synthetic resources only**, printing real state (rows/decisions) for a human to eyeball. It
is not a test runner and never marks approval.

- `--list` shows registered scenarios; an unknown id exits nonzero with the known list.
- Default datastore is an ephemeral synthetic SQLite file, **deleted at the end** (`--keep` retains
  it; `--database-url` uses your own store and is never deleted — point it at a disposable Postgres
  for real persistence checks).
- A production `--environment` is **refused** unless `--allow-production` is passed; AI stays
  disabled either way (no paid/model action). Scenarios may still refuse on their own (the DCA-010
  self-check seeds, and seeding is dev-only).

Each future story registers its own scenario with `@register("DCA-0NN", "…")` in
`scripts/manual_check.py`.

```bash
python scripts/manual_check.py DCA-010          # seed synthetic A/B, list rows, auto-clean
python scripts/manual_check.py DCA-010 --keep   # keep the synthetic db for inspection
```

## Deliberate deferrals

- **Autogenerate not wired** — the baseline revision is driven from `Base.metadata`; subsequent
  schema changes get explicit, hand-written revisions. `auto_create_schema` (metadata `create_all`)
  remains for SQLite dev/test; production uses Alembic (above).
- Suppression/objection registry, Art-14-style notification, platform export, web/admin UI,
  AI capabilities, live MCA/GSTIN adapters: later stories (several blocked — see B1/B5 in
  `docs/DCA-001-integration-contracts.md`).
