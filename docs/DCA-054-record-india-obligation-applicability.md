# DCA-054 — Record India obligation applicability

Stage: Source rights and policy foundation · key: `india-law-review`

**Artifact date: 2026-10-28.** (The research date; every commencement/effective date below is relative
to it and MUST be re-verified against the official source at implementation and at release.)

**This is a documentary review artifact only.** It changes no application behaviour, enables no source
or channel, writes no rulepack/policy row, and promotes no version. It records, with official
references, the India obligations the DCA must honour and *which* of them remain **unresolved** and
therefore stay mapped to **disabled** future capabilities. Nothing here is treated as an in-force
rule; nothing here authorizes production behaviour.

Every statement below is recorded either as a **claim with an official reference** or as
**UNRESOLVED / reviewer-needed with a documented reason**. No obligation is asserted as settled law by
this service — the service authors no legal conclusion (same posture as DCA-053: cite the external
basis, record it, interpret nothing automatically).

## Prerequisite check (DCA-053 and the earlier sequence)

Verified present in this repository before starting (read-only inspection, no behaviour changed):

- **Git history shows the accepted sequence through DCA-053** (`git log --oneline`):
  `eac565b DCA-053-approve-a-policy-version`, `8592801 DCA-052-define-versioned-india-rulepack-schemas`,
  `ed0d121 DCA-051-document-the-first-indian-source-candidate`, back through `6a77b34 DCA-044`.
- **The policy foundation code exists end to end:**
  - `app/compliance/policy_rulepacks.py` (DCA-052) — the versioned, validated India **policy** rulepack
    schema. Every stored pack is `status='draft'`; `is_active` is always `False` (promotion and the
    policy *engine* are disabled future stories).
  - `app/services/policy_approval.py` (DCA-053) — the reviewed-policy lifecycle: a platform operator
    records references + reviewer + review date + explicit review expiry and promotes `draft`→`active`;
    `PolicyApprovalStore.evaluate` is a *lifecycle* check that interprets no rules.
  - `app/compliance/rulepacks.py` — the lightweight deterministic acquisition-screening pack (IN =
    official-registry tier, role-address contacts only; `DEFAULT` = allow-nothing fallback).
  - The matching per-story docs exist (`docs/DCA-044…053-*.md`).
- **No live Indian source or channel is enabled.** Only the clearly-labelled synthetic fixture
  (`app/sources/synthetic_india.py`) exists; there is no DPDP/TRAI enforcement engine wired to any send
  or acquisition path. This story documents obligations; it does not enforce them.

Conclusion: prerequisite satisfied. Proceeding with this documentary story only.

## Scope — what this records, and what it must not claim

Per Source-prompt §5 (India-specific policy design) and the acceptance criteria:

- **No blanket "DPDP is fully active" or "DPDP is wholly inactive" claim.** The regime is being
  commenced in **stages**; applicability is **per-provision** and must be stored per obligation, never
  as a single `dpdp_enabled` flag.
- **No "public data found online is unrestricted" claim.** The Act's public-data carve-out is about
  *particular publication circumstances*, not everything discoverable online. A directory listing is
  not blanket permission, and India does **not** get a GDPR-style general legitimate-interest ground
  via this build.
- **Acceptance of a prospect is not recipient consent.** This is a DCA invariant and is unchanged here.
- Children's data, sensitive-trait profiling, private-mobile harvesting, Aadhaar/PAN collection, and
  inferred health/religion/caste/political/similar traits are **out of DCA scope entirely** (§5) — not
  "pending", but excluded.

## Official sources relied on (cited, not fabricated)

| What | Official source (URI) |
|---|---|
| DPDP Act, 2023 — full text (India Code) | `https://www.indiacode.nic.in/bitstream/123456789/22037/1/a2023-22.pdf` |
| DPDP commencement / Rules notification (Nov 2025, staged) — MeitY | `https://www.meity.gov.in/static/uploads/2025/11/c56ceae6c383460ca69577428d36828b.pdf` |
| MeitY (Ministry of Electronics & IT) — issuing authority for DPDP Rules/notifications | `https://www.meity.gov.in/` |
| TRAI — advice to senders (commercial-communication sender obligations) | `https://www.trai.gov.in/advice-to-senders` |
| TRAI — TCCCPR 2018 (Telecom Commercial Communications Customer Preference Regulations) | `https://www.trai.gov.in/` (locate the TCCCPR 2018 regulation + amendments) |
| WhatsApp Business Messaging Policy (platform channel rules) | `https://whatsappbusiness.com/policy/` |
| Google Customer Match / Ads data policy (first-party-origin requirement) | `https://support.google.com/adspolicy/answer/6299717` |

> **Live-fetch note (honesty).** This artifact does **not** assert a successful automated fetch of
> these pages. The data.gov.in portal returned HTTP 403 to non-browser clients on 2026-10-28 (recorded
> in DCA-051); government/regulator PDFs and portals commonly block scripted clients and change URLs.
> A reviewer must open each URI **in a browser** and confirm the current text, the exact
> commencement/amendment status, and the precise provision numbers **at implementation and at release**
> (§5: "Verify … from official sources at implementation and release"). The provision-to-tranche
> mapping below is the *structure to verify against the official text*, flagged where it is
> reviewer-dependent — it is not a settled legal conclusion authored by this service.

## 1. DPDP commencement — per-provision, staged (NO blanket active/inactive claim)

The Nov-2025 commencement notification **stages** provisions rather than switching the whole Act on at
once. Per §5 and the notification, the staging structure is: some provisions on **publication**, a
tranche at **+1 year**, and a tranche at **+18 months** from the notification date. Store an effective
date **per obligation**; never a single flag.

| Tranche (relative to the Nov-2025 notification) | Obligation category (reviewer MUST confirm the exact sections against the official PDF) | Applicability status for DCA on the artifact date (2026-10-28) |
|---|---|---|
| **On publication** | Definitions, the regulator/Board constitution and initial administrative/machinery provisions. | **In force per the notification — but reviewer-confirmed.** These do not by themselves authorize any DCA acquisition or sending; they define the framework DCA must later comply with. |
| **+1 year** | Operational-duty provisions (e.g. the core notice/consent and data-principal-rights machinery as commenced). | **NOT yet treated as in force by DCA** as of the artifact date — pending reviewer confirmation of the exact commencement date and the precise provisions in this tranche. |
| **+18 months** | The remaining/later-commenced duties (e.g. the fuller compliance obligations the notification defers). | **NOT yet treated as in force by DCA** — pending reviewer confirmation. |

**Explicit reviewer need (do not skip):** the mapping of *which section number* lands in *which
tranche* is **UNRESOLVED in this artifact** and must be transcribed by a reviewer directly from the
official notification PDF, with the commencement date filled in. This artifact deliberately does not
invent section→date pairs. The acceptance rule is met precisely by *not* claiming the regime is
uniformly active or inactive and by making the reviewer step explicit.

**How this is stored once resolved:** per-obligation effective dates map onto the DCA-052 policy rule
field `effective_from` / `expires_at` (the rule's EXPLICIT validity window) and the pack-level
`evidence` reference — *per rule*, never a global DPDP toggle. Until a reviewer fills a rule's
`effective_from` and a platform operator approves that pack version (DCA-053), the rule is a
**`draft`** and authorizes nothing.

## 2. Public-data exclusion — circumstance-specific, NOT blanket permission

- The Act excludes personal data **made publicly available** only in *particular publication
  circumstances* (e.g. by the data principal themselves, or under a legal duty to publish). This is
  **not** a general "anything online is fair game" rule, and it is **not** a GDPR-style
  legitimate-interest ground.
- **Required evidence (reviewer-needed):** store *who* made the data public and under *what* basis, and
  the applicable review — a directory/registry listing alone is **insufficient**. This is why DCA-051
  recorded the MCA registry's `registered email` field as **DEFERRED / treat-as-personal** rather than
  acquirable.
- **A registry record is not recipient consent** for any channel. Channel permission is a separate gate
  (§5 channel table) and stays unaffected.

## 3. Data-Fiduciary vs Processor responsibilities (role allocation — not "processor-only")

§5 forbids labelling every DCA activity processor-only. Recorded role allocation (reviewer to confirm
per flow at review time):

| Activity | Likely role (reviewer-confirmed) | Responsibility that attaches (per DPDP, effective-date-gated) |
|---|---|---|
| Platform-chosen prospect **collection** from sources, and **distribution** of platform-provided prospect data to tenants | **EffortlessPromo as Data Fiduciary** (it determines purpose/means of this collection) — **not** processor-only. | Lawful basis / notice to the data principal where applicable, purpose limitation, minimisation, retention limits, erasure/correction routing, grievance routing, security safeguards — each gated on the relevant provision's effective date (§1). |
| Processing a **tenant's own first-party CRM/forms/uploads** on the tenant's behalf | **Tenant = Data Fiduciary; EffortlessPromo/DCA = Processor** for that tenant's data. | Process only on documented instruction; assist the tenant-fiduciary with data-principal rights, security and breach duties; no independent reuse. |
| Shared canonical (global master) observations | Role depends on the field's reviewed collection/reuse/display rights (§7: "global identity is not global permission"). | Only platform-wide-reviewed non-personal/role data may be shared; private contact values stay business-scoped. |

**UNRESOLVED (reviewer-needed), deliberately not invented:**
- **No generic indirect-collection notice deadline is asserted** (§5 explicitly forbids inventing one).
  When the notice obligation commences and *within what window* notice must issue is a reviewer item,
  to be transcribed from the official text — not guessed here.
- Whether a given platform-provided-prospect flow makes EP a Fiduciary or a joint arrangement for a
  specific tenant is a per-contract review item.

## 4. TRAI / TCCCPR / DLT — channel obligations (SMS), consent-first product default

Per §5 channel table and TRAI sender guidance:

- SMS promotional sending requires DLT **sender registration**, registered **header**,
  content/consent **template**, correct **route/category**, and **preference/consent (DND/NCPR)
  screening**, with the delivery chain going through the **existing platform send pipeline** — DCA adds
  no sending path (see Boundary).
- **DLT registration is NOT recipient permission**, and **a form checkbox is NOT proof of telecom
  consent registration**. DND/preference screening is **not** permission to use other channels.
- **Consent-first is the DCA product default** (a *stricter* product rule layered over law/provider
  rules). DCA must not assert that every legally available Indian promotional route carries identical
  consent conditions.
- **No unrestricted public DND API is assumed.** Where telecom checks are done by a delivery provider,
  record the integration, decision evidence, timestamps, and unavailable/error behaviour.
- WhatsApp (recipient-provided contact + opt-in + approved template + service window) and Google
  Customer Match (eligible first-party origin; bought/discovered records rejected even if hashed) are
  **separate, independently gated** channels — recorded here only to note they are not covered by SMS
  or email permission.

## 5. Obligation → DCA control mapping, and the disabled rules that remain

Each obligation maps to a DCA-052 **policy rule** (deterministic data the future, **disabled** policy
engine would consult). The three-way authority split (§5: track **legal rules**, **provider
requirements**, and stricter **product defaults** separately) maps onto the DCA-052 `RuleAuthority`
enum `law | provider | product` (precedence `law > provider > product`).

| Obligation | DCA-052 rule shape (action / channel / authority / condition) | State today |
|---|---|---|
| DPDP notice/consent before contacting a person | `contact` / `email`\|`sms` / `authority=law` / `conditions={consent: required}` + a `registration_required` condition for SMS | **DISABLED draft** — no in-force version; `effective_from` unset pending §1 reviewer confirmation |
| Out-of-scope / unmapped jurisdiction | handled by `app/compliance/rulepacks.py` `DEFAULT` pack = allow-nothing, **already active** as the strictest fallback | Active *as a deny-everything fallback* (safe) |
| SMS DLT/header/template/DND | `contact` / `sms` / `authority=provider` / `conditions={registration_required: true, consent: required}` | **DISABLED draft** — SMS acquisition/activation not enabled; feature-gated OFF |
| Consent-first B2B default | `contact` / `email` / `authority=product` / `conditions={consent: required}` | **DISABLED draft** — stricter product default, not yet promoted |

### Traced example (manual-test step 2 — one unresolved obligation → one disabled rule requirement)

**Unresolved obligation:** the DPDP *notice-before-collection/contact* duty — its **commencement date
is UNRESOLVED** in §1 above (reviewer must transcribe the exact provision + tranche from the official
PDF).

**Traces to this disabled rule requirement:** a DCA-052 `allow`/`contact` rule whose
`conditions={consent: required}` and whose **`effective_from` is UNSET**. Because `effective_from` is
unset and no reviewer has filled it:
- the pack remains `status='draft'` (DCA-052: every stored pack is a draft);
- `PolicyApprovalStore.evaluate(version)` returns **denied `not_approved`** (DCA-053: a draft/unapproved
  version is never in force);
- and even a well-formed pack cannot be promoted until a **platform operator** (scope
  `platform.policy.rulepack.approve`) records references + reviewer + review date + explicit expiry.

So the unresolved legal date is *physically* the reason a concrete rule cannot be in force. That is the
required trace from an open obligation to a disabled rule requirement — reproducible read-only below.

## 6. Open questions (explicitly recorded — reviewer-needed, not invented)

1. **Exact provision→tranche→date mapping** of the Nov-2025 DPDP commencement (§1). Transcribe from the
   official PDF; do not infer.
2. **Indirect-collection / notice timing** — no deadline is asserted (§5 forbids inventing one).
3. **Which flows make EP a Data Fiduciary vs Processor** per tenant contract (§3).
4. **Public-data carve-out evidence standard** — what proof of "who published, under what basis"
   satisfies the exclusion for a given registry field (§2); MCA `registered email` stays treat-as-personal.
5. **TCCCPR/DLT provider-integration evidence** — supported integration, decision evidence, error
   behaviour (§4); no unrestricted DND API assumed.
6. **Grievance-routing / data-principal-rights execution** (correction/erasure, synchronized with
   platform suppression) — commencement-gated, mechanics deferred to their own stories.

Each open question stays mapped to a **draft** (disabled) rule or an entirely future capability; none
is resolved by this artifact.

## 7. Why this stays disabled

- No policy rulepack version is promoted; `PolicyApprovalStore.evaluate` is `not_approved`/not-in-force
  for everything relevant.
- No per-obligation `effective_from` is filled, so no DPDP/TRAI rule can be in force even if a pack were
  approved.
- The policy **engine** that would consult an in-force pack to permit/deny an action remains a
  **disabled future capability** (DCA-052/053).
- The strictest-fallback acquisition pack (`DEFAULT` = allow-nothing) is the only thing "active", and it
  only ever *denies* — the safe direction.
- No live source or channel is enabled; DCA adds no sending path.

## Acceptance criteria — how this artifact meets them

- **No blanket active/inactive DPDP claim; no "public data unrestricted" claim; evidence and reviewer
  needs explicit.** §1 records staged, per-provision commencement with an explicit "reviewer must
  transcribe section→date" need; §2 records the circumstance-specific public-data carve-out and the
  who-published evidence requirement. No GDPR-style legitimate-interest ground is assumed.
- **Provision-specific commencement, Fiduciary/Processor responsibilities, and open questions are
  present.** §1 (staged tranches), §3 (role allocation, not processor-only), §6 (open questions).
- **Unresolved production rules stay disabled.** §5/§7: every unresolved obligation maps to a `draft`
  (disabled) DCA-052 rule or a future capability; the only active pack denies everything.
- **Reproduce the result and the relevant denial without unintended state changes.** The manual test
  below reproduces both the cited sources and the `not_approved` denial read-only — no running app, no
  DB write, no promotion, no enablement.

## Developer manual test

This is a documentary review; **no running application, migration, or synthetic database is needed** for
steps 1–2. Step 3 is an optional read-only reproduction of the denial on in-memory SQLite.

1. **Open this document and the cited artifacts side by side.** For each URI in "Official sources",
   open it in a **browser** and confirm the current text (the Act, the Nov-2025 commencement
   notification, TRAI sender guidance). Record the date you verified — commencement status changes over
   time.
2. **Trace one unresolved obligation to a disabled rule requirement** (§5 traced example): confirm that
   the DPDP notice/consent commencement date is recorded as UNRESOLVED in §1, and that the corresponding
   DCA-052 rule would carry an **unset `effective_from`**, keeping its pack a `draft` that
   `PolicyApprovalStore.evaluate` denies as `not_approved`. Read-only confirmation in `app/`:

   ```bash
   grep -n "effective_from\|status.*draft\|is_active" \
     effortlesspromo-dca/app/compliance/policy_rulepacks.py
   grep -n "not_approved\|REASON_NOT_APPROVED\|def evaluate" \
     effortlesspromo-dca/app/services/policy_approval.py
   ```

   Expected: the policy store writes `draft` packs with optional `effective_from`; `evaluate` denies an
   unapproved version with `not_approved`. No state is changed by reading.
3. *(Optional)* **Reproduce the denial** read-only against the existing DCA-053 scenario — it records a
   draft and shows the unapproved version denied `not_approved`, with no promotion:

   ```bash
   cd effortlesspromo-dca
   python scripts/manual_check.py DCA-053
   ```

   Expected output includes the draft rulepack recorded and the unapproved version evaluated to
   **denied `not_approved`** (plus the tenant-admin `not_platform_operator` refusal) — i.e. the relevant
   denial, reproduced with scoped cleanup and no committed state change. (This runs DCA-053's scenario;
   DCA-054 adds no new scenario because it is a purely documentary story.)
4. **Verify no prohibited change occurred.** Confirm `git status` shows only this one new file under
   `docs/`; no `Source`/rulepack/approval row written, no migration, no config, no source or channel
   enabled, no model call, no prospect messaged.

Leave human sign-off to the developer.

## Boundary confirmation

Work stayed inside this story's documentation boundary: **one** new file under the DCA `docs/` area and
nothing else. No DCA/AI/Web/Admin application code, schema, migration, config, or test was changed. No
rulepack version authored or promoted; no source or channel enabled; no model call made; no prospect
messaged.

**Owning app for any *future* screen** that would surface this obligation review for operator sign-off:
**Admin** (platform-operator policy review, consistent with DCA-053's `platform.policy.rulepack.approve`
scope), **not** Web. Recorded here only to identify ownership — not built. Client-support visibility, if
ever added, would require scoped, expiring, revocable authority with actor/tenant audit (Admin role
alone is insufficient).

## Traceability

Source sections: 5, 6; key: `india-law-review`. This story's stop rule overrides the reference prompt's
broad continue-working instructions.
