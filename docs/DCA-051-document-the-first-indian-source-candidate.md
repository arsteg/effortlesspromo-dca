# DCA-051 — Document the first Indian source candidate

Stage: Source rights and policy foundation · key: `source-matrix`

**This is a documentary review artifact only.** It changes no application behaviour, enables no
source, ships no adapter, and writes no catalog row. It evaluates **one** real, read-only Indian
organization dataset against the DCA source-catalog rights model so the candidate can be reviewed and
accepted independently. Every capability below is recorded as a *claim with evidence* or as
**UNAVAILABLE with a documented reason**; nothing here is treated as an approved right.

## Prerequisite check (DCA-050 and the earlier sequence)

Verified present in this repository before starting (read-only inspection, no behaviour changed):

- Source-rights foundation code exists end to end: `app/services/source_catalog.py` (DCA-044 draft
  store), `source_terms.py` (DCA-045 versioned field/operation rights), `source_approval.py` (DCA-046
  approval), `source_entitlement.py` (DCA-047 per-business narrowing), `source_preflight.py` (DCA-048
  authorize-before-fetch), `source_expiry.py` (DCA-049 time expiry), `source_revocation.py` (DCA-050
  revocation).
- Git history shows the accepted sequence through DCA-050 (`e033efc DCA-050-revoke-a-source-version`).
- The matching per-story docs exist (`docs/DCA-044…050-*.md`). The only Indian source consulted by any
  code today is the **synthetic** fixture adapter (`app/sources/synthetic_india.py`, key
  `synthetic.mca`), which is "obviously fake" and config-gated OFF — there is **no** live Indian source
  catalogued or enabled. This story documents the first *candidate* for one; it does not add it.

Conclusion: prerequisite satisfied. Proceeding with this documentary story only.

## The candidate

**MCA "Company Master Data" published on the Open Government Data (OGD) Platform India
(data.gov.in).**

This is the candidate the implementation brief itself names first (Section 6, Indian source strategy,
point 2: "Evaluate specific India OGD/MCA-related downloadable datasets for permitted *organization*
facts"). It is chosen over the live MCA portal, GST/GSTIN lookup, and Udyam precisely because it is the
one with (a) a published, named licence and (b) a legitimate bulk/download access route for
**organization-level, non-personal** facts — i.e. the only one of the four that does not require
assuming rights the brief forbids us to assume.

- **Candidate catalog key (proposed, NOT created):** `ogd.mca.company_master`
- **Kind:** `OFFICIAL` (`SourceKind.OFFICIAL`) — a government dataset.
- **Entity type:** organization (company / LLP registry master record). **Not** a contact source.
- **India coverage:** nationwide (all RoC jurisdictions). Dataset is partitioned per Registrar of
  Companies (RoC); the union is all-India.

### Official evidence (cited, not fabricated)

| What | Source |
|---|---|
| Dataset catalog page (title, contributor, per-RoC resources) | `https://data.gov.in/catalog/company-master-data` |
| Government Open Data License – India (GODL) terms | `https://data.gov.in/Godl` ; full text mirror: `https://en.wikisource.org/wiki/Government_Open_Data_License_(India)` ; summary template: `https://en.wikipedia.org/wiki/Template:GODL-India` |
| National Data Sharing & Accessibility Policy (the GODL basis) | `https://en.wikipedia.org/wiki/National_Data_Sharing_and_Accessibility_Policy` |
| DPDP Act 2023 (personal-data boundary that limits any contact reuse) | `https://www.indiacode.nic.in/bitstream/123456789/22037/1/a2023-22.pdf` |
| DPDP commencement notification (Nov 2025, staged) | `https://www.meity.gov.in/static/uploads/2025/11/c56ceae6c383460ca69577428d36828b.pdf` |

> **Access-denial evidence (reproducible, see Manual test).** Both `https://data.gov.in/Godl` and
> `https://data.gov.in/catalog/company-master-data` returned **HTTP 403 Forbidden** to an automated
> fetch on 2026-10-28. This is recorded as fact: the portal's edge blocks non-browser clients, so the
> dataset is **not** silently crawlable/scriptable from a server — a human browser session (and, for
> any programmatic use, a registered OGD API key — see Credentials) is required. This supports the
> brief's rule "do not automate restricted portals" and is itself part of why live access stays
> disabled.

## Mapping to the DCA source-catalog rights model (Section 6 fields)

Recorded as the *draft* this candidate would become — a **DISABLED `draft`, zero permissions** row via
`SourceCatalogStore.create_draft` (DCA-044). Promotion/approval is a later, still-disabled capability.

| Catalog field (Section 6) | Value for this candidate | Basis |
|---|---|---|
| `Key` | `ogd.mca.company_master` | proposed |
| `Kind` | `OFFICIAL` (OfficialDataset) | govt dataset |
| `Status` | `DRAFT` — disabled | DCA-044 rule: every source is a disabled draft |
| `AccessMethod` | `BULK_DOWNLOAD` (per-RoC downloadable files from the portal). **NOT** `API` — see note. | catalog page offers downloadable resources per RoC |
| `SupportedCountries` | India only | OGD = Govt of India data |
| `SupportedEntityTypes` | Company, LLP (organization master) | dataset scope |
| `AllowedFields` *(see field table below)* | organization-master facts only (CIN/LLPIN, name, status, class, category, sub-category, date of registration, RoC, registered state, authorized capital, paid-up capital, registered address, principal/NIC business activity, listed status) | catalog field list |
| `AllowedPurposes` | **UNDECIDED — requires rights review.** Candidate purpose = organization firmographic enrichment / ICP targeting signal. NOT contact acquisition. | deferred to approval story |
| `AllowedOperations` | **Proposed for review, deny-by-default:** `COLLECT`, `STORE`, `DERIVE`, `DISPLAY` of org-level non-personal fields. `EXPORT` / `SHARE` to tenants left **withheld pending GODL redistribution review** (GODL permits derivative works, but attribution + no-sensitive-data conditions attach). `MODEL_PROCESSING` only via the existing AI gateway if later approved. | DCA-045 per-field/op model; GODL |
| `LicenseVersion` | Government Open Data License – India (GODL), version as published on the dataset page at approval time (record the exact revision then). | `data.gov.in/Godl` |
| `ContractEvidenceRef` | GODL licence URL + archived copy of the dataset catalog page + resource IDs per RoC, captured at approval. | required by DCA-045 `add_version` (missing evidence is refused) |
| `CollectionReview` | Pending: confirm the data is published under GODL and contains no personal/sensitive data excluded by GODL (see Exclusions). | GODL exclusions clause |
| `EndCustomerDisplayRights` | Likely allowed for org-level facts with attribution; confirm at review. | GODL permissible-use grant |
| `RedistributionRights` | **Not assumed.** GODL permits adapted/derivative publication *with attribution*, but redistribution of raw rows to tenants must be reviewed against GODL + DPDP (a registered company's registered email can identify a sole proprietor → treat as potentially personal). | GODL + DPDP boundary |
| `ExportRights` | Withheld pending review (see above). | — |
| `DerivationRights` | Candidate: allowed for non-personal firmographic derivation (e.g. size band from capital). | GODL derivative-works grant |
| `ModelProcessingRights` | Only through `app/clients/ai_client.py` → effortlesspromo-ai gateway; no DCA-side model calls. | DCA hard boundary |
| `RetentionPolicy` | To be set at approval; snapshot-with-refresh (dataset is a periodic dump, not live). | — |
| `Attribution` | **MANDATORY** — GODL requires acknowledging provider, source, and licence incl. DOI/URL/URI. Must be stored and surfaced wherever a derived fact is shown/exported. | GODL attribution clause |
| `RobotsPolicy` / `RateLimits` | Portal blocks automated fetch (observed 403). No scripted crawling. Bulk files downloaded via the human portal; any programmatic path uses the registered OGD API within its documented limits. | observed 403 + OGD API key model |
| `PricingVersion` | Free (public funds / OGD). No per-record fee. | OGD |
| `CredentialsRef` | **None for a logged-in human bulk download.** Programmatic access to OGD resources requires a free **registered data.gov.in API key** (per-account, rate-limited). No MCA paid credential, no provider secret, no DCA-held provider key. | OGD API-key model |
| `ResidencyConstraints` | Indian govt data; store within existing DCA residency rules. | — |
| `ReviewedBy` / `ReviewedAt` / `ReviewDueAt` | Unset — this artifact is the *candidate dossier*, not an approval. A platform operator with `SOURCE_TERMS_APPROVE` performs the actual review in DCA-046's path later. | — |
| `EffectiveFrom` / `ExpiresAt` | Unset — no terms version is in force; nothing is enabled. | — |

### Fields the dataset actually exposes (organization master, non-personal)

Per the catalog description: **CIN / LLPIN, Company/LLP name, Company status, Company class, Company
category, Company sub-category, Date of registration/incorporation, Registrar of Companies (RoC),
Registered state, Authorized capital (INR), Paid-up capital (INR), Registered address, Principal/NIC
business-activity code, Listed status.** Some RoC extracts also carry a *registered email*. The
registered email is the **one field that can be personal** (sole proprietors / named individuals), so
it is recorded here as **DEFERRED / treat-as-personal** — excluded from any candidate contact-acquisition
use and gated by DPDP review, never auto-collected.

## Reuse limits (explicitly recorded)

- **GODL is conditional, not public domain.** Worldwide, royalty-free, non-exclusive right to use,
  adapt, publish, and make derivative works for lawful commercial/non-commercial purposes **only with
  attribution** and **only for data that is non-personal, non-sensitive, non-copyrighted, non-military**.
  Personal/sensitive data is **excluded** from the licence. Non-compliance terminates the licence
  automatically. No warranty; provider not liable.
- **Attribution is mandatory and must propagate** to any displayed/exported derived fact.
- **A registry record is not recipient consent.** Acceptance of a prospect ≠ consent (DCA invariant).
  Nothing here establishes email/SMS/WhatsApp permission; channel eligibility is a separate gate
  (Section 5 channel table) and stays unaffected.

## Capabilities explicitly recorded as UNAVAILABLE / not assumed

Per the acceptance criterion "unavailable bulk access is explicitly recorded" and the brief's
instruction not to assume MCA/GST/Udyam bulk rights:

- **MCA live portal / MCA21 V3 search — NO bulk right.** The live MCA "master data" search and
  signatory/director data are an *identifier-verification / per-company lookup* surface, often behind
  login/fees, not a redistributable bulk feed. **Not** used; must not be turned into bulk discovery.
- **GST / GSTIN — NO bulk right.** GSTIN search/verification endpoints are identifier verification,
  not a prospect database; no redistributable bulk organization export is assumed.
- **Udyam (MSME) registration — NO bulk right.** No bulk download of Udyam registrant records is
  assumed; treated as an evaluation candidate only, left disabled with this reason.
- **Any automated crawl of data.gov.in — BLOCKED.** Observed HTTP 403 to non-browser clients
  (2026-10-28). No scripted scraping of the portal.
- **EXPORT / SHARE of raw rows to tenants — WITHHELD** pending GODL-redistribution + DPDP review of the
  potentially-personal registered-email field.

## Why this stays disabled

- No `Source` row is created by this story; if one were, DCA-044 forces `status='draft'`,
  `enabled=False`, `permissions=[]`. No code path writes `ENABLED`.
- No `SourceTerms` version is added or approved; `SourceApprovalStore.is_in_force` is `False` for
  everything, so the DCA-048 preflight denies at the source-status gate for any attempted live use.
- No live adapter is added to `app/sources/`; only the clearly-labelled synthetic fixture exists.
- Future capabilities (approval, enable, live fetch, export) remain their own later, disabled stories.

## Acceptance criteria — how this artifact meets them

- **Evidence supports each claimed capability.** Each row above cites the official source (GODL text,
  OGD catalog page, DPDP Act/notification). Capabilities not evidenced are marked UNDECIDED/WITHHELD,
  not asserted.
- **Unavailable bulk access is explicitly recorded.** MCA-live/GST/Udyam bulk rights and tenant export
  are each recorded as UNAVAILABLE/WITHHELD with a reason.
- **Reproduce the result and relevant denial without unintended state changes.** The developer manual
  test below reproduces both the dataset/licence evidence and the 403 access denial read-only — no
  running app, no DB, no catalog write, no enablement.

## Developer manual test

This is a documentary review; **no running application, migration, or synthetic database is needed.**

1. Open this document and the cited artifacts side by side.
2. **Dataset + fields:** open `https://data.gov.in/catalog/company-master-data` in a browser and confirm
   the contributor (Ministry of Corporate Affairs), the per-RoC downloadable resources, and that the
   listed attributes match the "Fields the dataset actually exposes" table above (CIN/LLPIN, name,
   status, class, category, sub-category, date of registration, RoC, state, authorized/paid-up capital,
   address, activity). Confirm the attributes are **organization-level**, not a contact list.
3. **Licence:** open `https://data.gov.in/Godl` (or the Wikisource mirror) and confirm the licence name
   "Government Open Data License – India", the attribution requirement, the no-warranty clause, and the
   personal/sensitive-data **exclusion**. Confirm GODL is conditional, not public-domain.
4. **Reproduce the access denial (read-only, no state change):**

   ```text
   curl -sS -o /dev/null -w "%{http_code}\n" https://data.gov.in/Godl
   curl -sS -o /dev/null -w "%{http_code}\n" https://data.gov.in/catalog/company-master-data
   ```

   Expected: a non-200 (observed **403** on 2026-10-28) for a non-browser client, confirming the portal
   is not scriptable without a browser/registered API key. (Exact code may vary by edge policy/time;
   the point is that unauthenticated automated access is refused — record whatever you observe.) No
   prohibited change occurs: these are GET reads of public URLs; nothing in DCA is written, no source is
   enabled, no prospect is contacted.
5. **Verify disabled state in-repo:** confirm `app/sources/` contains only `synthetic_india.py`
   (no live MCA/OGD adapter) and that this story added no `Source` row, migration, or config change.

Leave human sign-off to the developer.

## Boundary confirmation

Work stayed inside this story's documentation boundary: **one** new file under the DCA `docs/` area and
nothing else. No DCA/AI/Web/Admin application code, schema, migration, config, or test was changed. No
source enabled. No model call made. No prospect messaged. Owning app for any *future* screen that would
surface this candidate for operator review: **Admin** (platform-operator catalog review under
`SOURCE_TERMS_APPROVE`), not Web — recorded here only to identify ownership, not built.

## Traceability

Source sections: 5, 6; key: `source-matrix`. This story's stop rule overrides the reference prompt's
broad continue-working instructions.
