# DCA-090 — Evaluate WhatsApp eligibility

Stage: **India channel eligibility** · key: `whatsapp-policy` · source sections 5, 10.

## What this adds

One pure, deterministic decision function — `app/compliance/whatsapp_eligibility.py` — that
answers a single question for the **WhatsApp** channel: *may a MARKETING (promotional) WhatsApp
message to this recipient proceed to the host's one send pipeline?* It composes two deliberately
**distinct concern-families** that WhatsApp's Business policy keeps separate. It introduces no new
acquisition, no send, no AI call, no DB write, and is wired into no pipeline/worker/HTTP path.

### A) Marketing permission — the *authority* to send promotional content (fail-closed)

| # | Gate | Input | Blocks when | Reason code |
|---|------|-------|-------------|-------------|
| 1 | Basis | `MarketingOptIn.basis` | a non-`consent` basis is claimed (no B2B exception) | `basis_not_consent` |
| 2 | Opt-in in force | `MarketingOptIn.permission_state` | not `GRANTED` on the WhatsApp scope (none/withdrawn/declined) | `opt_in_not_in_force` |
| 3 | Contact origin | `MarketingOptIn.contact_origin` | origin is **public phone**, **inbound service inquiry**, or unknown | `contact_origin_not_opt_in` |

Only `WhatsAppContactOrigin.EXPLICIT_OPT_IN` qualifies. A **public phone** (scraped/listed) and an
**inbound service inquiry** (the user DM'd support) are explicitly non-authorizing.

### B) Message-delivery constraint — the *format/window* mechanics, **separate from permission**

| # | Gate | Input | Blocks when | Reason code |
|---|------|-------|-------------|-------------|
| 4 | Delivery mechanism | `ApprovedTemplate` + `ServiceWindow` | no `APPROVED` marketing-category template **and** the 24h service window is closed | `approved_template_required` |

A promotional message is deliverable via **either** an approved marketing template (any time) **or**
the recipient's open 24-hour service window (opened by their last inbound message, within which a
free-form session message may ride). This is a *delivery* mechanic — **it never substitutes for
marketing permission.**

`ELIGIBLE` requires **both families** to pass. Every failing gate is collected (not short-circuited)
in a fixed order — permission family first, then the message-constraint family — and each blocker
carries its `family` so a reviewer can see the separation.

### Load-bearing invariants (acceptance criteria)

- **A public phone or inbound service inquiry alone cannot authorize future promotional messages.**
  Both origins fail gate 3, and gate 2 independently requires an in-force opt-in. The headline case:
  an inbound service inquiry **opens the service window** (gate 4 satisfied — `message_delivery_available`
  is `True`) yet the message is still `BLOCKED` on the marketing-permission family
  (`marketing_permission_granted` is `False`, decisive blocker `family == "marketing_permission"`). The
  window never substitutes for permission.
- **The delivery constraint is real and distinct.** A fully opted-in recipient outside the window with
  no approved template is `BLOCKED` on `approved_template_required` — proving gate 4 denies
  independently of permission.
- **Pure and reproducible.** The decision reads nothing and writes nothing; a given `(request, now)`
  always yields the identical result — the "changing no state" criterion holds intrinsically.

`ELIGIBLE` is **not** recipient consent and **not** a send authorization. The actual WhatsApp send
stays a disabled future capability behind the host's one send pipeline, which re-gates everything.

## Public surface

- `decide_whatsapp_eligibility(request, *, now)` — the pure composition over
  `WhatsAppEligibilityRequest` (one value object per family).
- `evaluate_whatsapp_eligibility(...)` — convenience that derives the opt-in gate from a DCA-075
  `PermissionProjection` + the recorded contact origin, with the template/window as resolved facts.
- `opt_in_from_projection(projection, contact_origin, *, basis)` / `origin_is_opt_in(origin)`.
- Value objects: `MarketingOptIn`, `ApprovedTemplate`, `ServiceWindow`, `WhatsAppEligibilityRequest`,
  `WhatsAppEligibilityDecision`, `EligibilityBlocker`.
- Enums: `WhatsAppContactOrigin`, `TemplateStatus`, `WhatsAppCategory`, `WhatsAppEligibility`; the
  stable `REASON_*` / `FAMILY_*` / `DELIVERY_*` codes.

Decision properties worth noting: `marketing_permission_granted` and `message_delivery_available` are
the two families reported *independently*, `permits_channel` is WhatsApp-only, `grants_consent` is
always `False`.

## Tests

`tests/test_whatsapp_eligibility.py` pins the behaviour: the reproducible `ELIGIBLE` (template path and
session-window path), the acceptance cases (public phone blocked despite a usable template; an inbound
inquiry that opens the window still blocked on permission; an inbound inquiry blocked on origin even if
a GRANTED state is claimed), the separate-and-real delivery constraint (opted-in but no template +
closed window), pending/wrong-category templates, the consent-first basis rejection, WhatsApp-only
channel scoping, a non-WhatsApp projection failing the opt-in gate, the service-window boundaries
(edge/future/none), reproducibility, and the fixed family order of collected blockers.

The evaluator is a **pure function of structured inputs** — it opens no DB, calls no adapter and
consults no model — so there is no persisted read path to exercise (as on the DCA-087 recipient line,
which read real `ConsentReceipt` rows over disposable PostgreSQL). The "changing no state" acceptance
criterion holds intrinsically: nothing is read or written, so no disposable-PostgreSQL harness is
required for this story.

## Manual check

```
python scripts/manual_check.py DCA-090
```

Runs, in isolated synthetic Businesses A/B:

1. a properly **opted-in** recipient with an approved marketing template → `ELIGIBLE`;
2. a **public phone** alone → `BLOCKED` on `contact_origin_not_opt_in` / `opt_in_not_in_force`, even
   though the approved template makes delivery possible (`message_delivery_available = True`);
3. an **inbound service inquiry** that opens the 24h window → delivery satisfied, yet still `BLOCKED`
   on the marketing-permission family (the window never substitutes for permission);
4. the opted-in recipient **outside the window with no approved template** → `BLOCKED` on
   `approved_template_required` (delivery constraint is real and separate);
5. the opted-in recipient **inside the window** → `ELIGIBLE` via the session path.

Local synthetic only; no AI, no paid action, no network, no send; the decision changes no state.
```
