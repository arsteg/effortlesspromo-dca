# DCA-124 — Extract facts from an approved business website

Stage 11 (Approved source access) · key `web-org-adapter` · source sections 6, 8, 9.

## What this adds

The **second concrete source adapter** (after DCA-123's dataset importer): an **organization-only
website adapter** that, for an organization observation the business already holds, **deterministically**
parses a bounded allowlist of **company** fields out of an **approved + entitled** business web page and
records them as provenanced observations — **without ever fetching and without any model call**.

- `app/sources/website_extract.py`
  - `WEBSITE_EXTRACT_CAPABILITY` — a DCA-122 organization-only capability descriptor
    (`LIST_CAPABILITIES` + `ENRICH_ORGANIZATION`, requires `collect`, `handles_personal_contacts=False`).
    Validated at import time, so it can neither declare nor execute a contact operation.
  - `extract_org_facts(body)` — a **pure, network-free** extractor: reads at most one value per
    `PERMITTED_ORG_FIELDS` key (`legal_name`, `description`, `industry`, `cin`, `gstin`) off the page
    via a fixed allowlist of regexes, and **counts** (so the caller can drop) any email/phone fragment.
    The body is only scanned (bounded to `MAX_SCAN_CHARS`) — nothing is parsed-as-code, decoded,
    rendered or evaluated.
  - `WebsiteFactExtractor.extract(request, page, actor)` — composes the **DCA-121 guarded fetcher**
    (`FetchPolicyGate`: rights **FIRST**, then crawl/robots), the **DCA-061** field-observation store
    and the **DCA-062** evidence store. It stages into the caller's session and never commits.
- `app/db/models.py` — `WebsiteFactExtraction` (tenant-owned, RLS) run-level provenance row.
- `migrations/versions/0050_website_fact_extractions.py` — dual-path create + RLS, mirrors 0049.

## The contract (`WebsiteFactExtractor.extract`)

Order — each stage writes nothing on failure:

1. **Request shape** — source key + version, purpose and a parseable page URL are required
   (`WebsiteExtractError`).
2. **Subject** — the facts must attach to an `OrgObservation` **in this business**; an unknown or
   cross-tenant id reads as not-found.
3. **Idempotency** — an already-extracted `(source, subject, page_ref)` is a deterministic **no-op**
   (skipped), checked **before** authorizing, so a replay reserves no crawl slot and writes nothing.
4. **Guarded fetcher (DCA-121)** — rights first (a license denial never touches the crawl limiter),
   then crawl/robots (a robots-disallowed path is a hard crawl deny). A denial returns un-authorized
   and writes nothing. On a crawl allow the in-flight slot is released immediately (the synthetic page
   is already in hand).
5. **Deterministic parse** — the allowlist extractor; contact fragments are dropped (counted).
6. **Write** — the page is captured as an `EvidenceObject` (`content_kind="business_website"`), each
   permitted fact becomes an **observed** `FieldObservation` whose `provenance_ref` **cites** the
   evidence (`<url>#evidence=<id>`), and a `WebsiteFactExtraction` provenance row ties the run together
   (idempotency key + `fields_recorded` / `contact_fields_dropped` witnesses).

## Acceptance criteria → evidence

- **Collects only permitted company fields; no unrelated contact field leaks.** Extraction reads only
  the `PERMITTED_ORG_FIELDS` allowlist (all organization-level, no contact channel); email/phone are
  detected and dropped (`contact_fields_dropped`), and the adapter is organization-only so there is no
  declared or executed path to a contact field (`PersonObservation`/`ContactPoint` counts stay 0).
- **Cannot crawl unrestricted profiles.** Only an org observation the business holds, from a
  source it is approved+entitled for, on a robots-allowed path can be extracted — a rights denial OR a
  robots-disallowed profile path extracts nothing.
- **Reproduce the result and the denial with no unintended state changes.** A denial writes no
  evidence/observation/provenance/audit; a re-extraction is idempotent.

## AI boundary

This story is **deterministic parsing only** — per the story ("AI extraction comes later"), the adapter
**calls no model and makes no AI-gateway request**, so it spends nothing. When AI extraction is added in
a later story it must go through the existing effortlesspromo-ai gateway client
(`app/clients/ai_client.py` + `app/services/ai_invoker.py`) — no provider SDK/key in DCA.

## Tests / manual

- `tests/test_website_extract.py` — approved extraction + provenance + cited evidence; contacts
  dropped; rights denial and robots-disallowed denial write nothing; untrusted content cannot inject an
  unpermitted field; idempotent re-extraction; unknown/cross-tenant subject refused; tenant isolation;
  a pure-extractor unit test; a Postgres path behind the `DCA_TEST_DATABASE_URL` skip guard.
- `tests/test_migrations.py` / `tests/test_rls.py` — 0050 create branch + the tenant-owned table set.
- `python scripts/manual_check.py DCA-124` — prints the real extracted facts, evidence + provenance,
  the idempotent re-extraction, and both denials (local synthetic SQLite, no network, no AI/paid).

## Boundaries

No network fetch (fetching stays a disabled future capability), no second AI gateway, no `.NET`/host-DB
changes. The migration touches only the DCA schema. Feature stays off by default; the page content is a
caller-supplied synthetic fixture treated as untrusted input.
