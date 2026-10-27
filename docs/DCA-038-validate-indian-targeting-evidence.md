# DCA-038 — Validate Indian targeting evidence

**Status:** implemented. Adds **structured geography** (country, state/UT, city, district, PIN /
service area, and atomic location evidence) and a **deterministic recipient-jurisdiction resolver** +
India-only targeting gate. The three geographic contexts — **legal address**, **operational area**,
**recipient location** — are kept strictly separate: only the recipient's own evidence governs whether
DCA personal-data acquisition is in-jurisdiction. Backend-only (Python DCA); no provider SDKs/keys, no
model routing, no second AI gateway, no writes to another service's database, and **no AI call**
(jurisdiction screening is deterministic, like the acquisition rulepacks).

## What it does

The agent can validate whether a target's location evidence actually establishes an Indian recipient
before treating that person as in-scope. It refuses to guess: a `+91` phone code or a `.in` domain (or
a language, an IP-geo guess, or an employer's HQ) is a **weak signal** that never proves the
recipient's jurisdiction on its own.

| Behaviour | How |
|---|---|
| **Structured geography** | `app/geography/evidence.py`: `GeoLocation` (country, state/UT, district, city, PIN, service-area km) and `LocationEvidence` (atomic signals with a typed `EvidenceKind`). `app/geography/india.py` holds the controlled reference data — the 28 states + 8 UTs (ISO 3166-2:IN), PIN format (`^[1-9]\d{5}$`), and GSTIN state codes — and field validation/normalization. |
| **Three contexts kept separate** | `GeoContext` = `LEGAL_ADDRESS` / `OPERATIONAL_AREA` / `RECIPIENT_LOCATION`. `TargetingGeography` carries each as its own evidence bag; they are never merged. |
| **Deterministic resolution** | `app/geography/resolver.py:resolve_jurisdiction` → `JurisdictionResolution` (`in` / `foreign` / `unresolved`). One strong proof resolves its country; conflicting strong proofs are `unresolved`; weak-only or no evidence is `unresolved`. |
| **`+91` / `.in` alone proves nothing** | Phone country code, domain TLD, language, IP-geo and employer-HQ are `WEAK_KINDS` — `proven_country()` returns `None` for them, so they can never resolve a jurisdiction. The headline acceptance criterion. |
| **India-only gate, recipient-governed** | `validate_targeting` allows acquisition only when the **recipient** resolves to India. A foreign or unresolved recipient is blocked even when the business's legal address / operational area is Indian (the decision records a `note` explaining the separation). |
| **Stateless, reproducible** | `POST /v1/targeting/validate` reads/writes no row, so a result and its denial are fully reproducible with no side effect. |
| **Future capabilities stay disabled** | No sources, no AI, no platform export, no persistence are added — this is pure validation over supplied evidence. |

Validating targeting evidence is unrelated to recipient consent; this story touches no sending/consent
path and never enrolls anyone.

## Where it lives

- **Reference data** — `app/geography/india.py`: `STATES_AND_UTS`, `normalize_country`,
  `normalize_state_ut`, `is_valid_pin`, `is_valid_gst_state_code`.
- **Value objects** — `app/geography/evidence.py`: `GeoContext`, `EvidenceKind`, `STRONG_KINDS` /
  `WEAK_KINDS`, `LocationEvidence` (`proven_country`), `GeoLocation` (`iter_evidence`),
  `TargetingGeography` (`from_locations`), `evidence_from_dicts`.
- **Resolver + gate** — `app/geography/resolver.py`: `JurisdictionStatus`, `JurisdictionResolution`,
  `resolve_jurisdiction`, `TargetingDecision`, `validate_targeting`, and the stable reason codes
  (`resolved_in`, `resolved_foreign`, `unresolved_no_evidence`, `unresolved_weak_signal_only`,
  `unresolved_conflicting_evidence`, `targeting_allowed_in`, `blocked_foreign_jurisdiction`,
  `blocked_unresolved_jurisdiction`).
- **Route** — `app/api/routes/targeting.py`: `POST /v1/targeting/validate` (structured contract for
  later Web/Admin consumers) and `GET /v1/targeting/reference`. Both `X-Service-Auth`-gated
  (fail-closed); stateless; no business scope (hold no tenant data).
- **Wiring** — `app/main.py` includes `targeting.router`.

No migration: this story adds no table or column (nothing is persisted).

## Tests

- `tests/test_geography_resolver.py` — the reference data + resolver + gate: PIN/state/GST/country
  validation; `+91` / `.in` / all-weak-signals-together are unresolved; a valid PIN / state-UT / GST /
  declared-IN / Indian postal address resolves `in`; a declared foreign country or foreign address is
  `foreign`; conflicting strong proofs are `unresolved`; the gate allows only an Indian recipient; an
  Indian legal address / operational area never rescues a weak recipient; contexts resolve
  independently; structured decomposition (city/district are never proofs); purity/repeatability.
- `tests/test_targeting_api.py` — the HTTP contract: service-auth fail-closed; India allowed; the
  `+91`-with-no-location denial; `.in`-alone denial; empty-body denial; foreign block; conflicting
  block; the separation (Indian legal/operational, weak recipient → blocked); structured-field
  validation echoed; the reference route.
- `tests/test_manual_check.py` — asserts the `DCA-038` scenario is registered, runs on local
  synthetic SQLite, and leaves all row counts unchanged (stateless).

`ruff check` / `ruff format` / `mypy` (on the new modules) / `pytest` all pass. (The in-memory SQLite
surface is sufficient here — nothing is persisted; the Postgres transaction/RLS/migration paths are
untouched.)

## Developer manual test

```bash
# Backend, inspectable scenario (local synthetic SQLite, AI disabled, auto-cleaned):
python scripts/manual_check.py DCA-038
```

It drives `POST /v1/targeting/validate` over seven synthetic cases — Indian recipient (allowed), a
`+91` contact with no location evidence (blocked), `.in` alone (blocked), empty (blocked), a foreign
recipient (blocked), an ambiguous Indian-PIN-plus-declared-US recipient (blocked), and a `+91`
recipient with an Indian legal address + operational area (still blocked) — printing each real
response, then proving the tenant/discovery/candidate/profile row counts are identical before and
after (the validator is stateless, so every allow/deny is reproduced with no state change).

## Boundary

Inside the DCA Python service only (new `app/geography/` package + one stateless route + tests + docs).
No AI service, Web, Admin, platform-API, database-schema or other-service changes. Validation is
deterministic Python; no provider SDKs/keys, no model routing, no second AI gateway. Acceptance of a
target is not recipient consent; no suppression/consent/sending path is touched.
