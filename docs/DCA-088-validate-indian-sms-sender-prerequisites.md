# DCA-088 — Validate Indian SMS sender prerequisites

Stage: **India channel eligibility** · key: `sms-setup` · source sections 5, 10.

## What this adds

One pure, deterministic decision function — `app/compliance/sms_sender_prerequisites.py` —
that answers a single question for the **Indian SMS** channel: *has the SENDER completed the
DLT (TCCCPR) registration prerequisites so a marketing SMS on this route may be offered to the
host's one send pipeline?* It composes the DLT sender-registration facts India's regime
requires before any commercial SMS can be routed. It introduces no new acquisition, no send,
no AI call, no DB write, and is wired into no pipeline/worker/HTTP path.

**This is sender readiness, not recipient permission.** It is the mirror of the DCA-087
recipient-eligibility line: the two are orthogonal gates and both must independently pass
(re-applied by the host's one send pipeline) before anything is sent.

### The combined DLT prerequisites (fail-closed)

| # | Gate | Input | Blocks when | Reason code |
|---|------|-------|-------------|-------------|
| 1 | Principal entity | `PrincipalEntity` | PE not registered / not `ACTIVE` / expired | `principal_entity_unregistered` |
| 2 | Header | `Header` | header missing / not `ACTIVE` / not bound to the PE | `header_not_registered` |
| 3 | Content template | `ContentTemplate` | template missing / not `ACTIVE` / not bound to the header | `content_template_not_registered` |
| 4 | Consent template | `ConsentTemplate` (consent routes only) | required but missing / not `ACTIVE` | `consent_template_not_registered` |
| 5 | Route / category | header + template categories vs `route` | a category does not match the requested route | `route_category_mismatch` |
| 6 | Delivery chain | `DeliveryChain` | TM/aggregator chain missing / not `ACTIVE` / not bound to the PE | `delivery_chain_incomplete` |

Only `ACTIVE` (and not past `expires_at`) registrations are usable — `NONE` / `PENDING` /
`SUSPENDED` / `EXPIRED` / `BLACKLISTED` all fail-closed. The consent template is an
*additional* prerequisite only on the consent-based routes (`promotional`,
`service_explicit`); on `transactional` / `service_implicit` its absence is not a blocker.

`READY` requires **all** applicable prerequisites to pass. Every failing prerequisite is
collected (not short-circuited) in a fixed order, so the result shows exactly which DLT setup
is missing or mismatched.

### Load-bearing invariants (acceptance criteria)

- **Missing/mismatched setup denies the route.** Any absent, not-`ACTIVE`, unbound or
  category-mismatched prerequisite yields `BLOCKED` with that gate's stable reason code.
- **Setup alone cannot authorize a recipient.** `authorizes_recipient` and `grants_consent`
  are always `False`. `recipient_consent_present` is carried for audit only and is **never**
  consulted — a DLT-ready sender is still not permitted to message a given recipient, and a
  recipient's consent cannot make an unregistered sender routable.
- **Pure and reproducible.** The decision reads nothing and writes nothing; a given
  `(request, now)` always yields the identical result — the "changing no state" criterion is
  satisfied intrinsically.

## Public surface

- `decide_sms_sender_readiness(request, *, now)` — the pure composition over
  `SmsSenderReadinessRequest` (one value object per prerequisite).
- `requires_consent_template(route)` — whether the route additionally requires a consent template.
- Value objects: `PrincipalEntity`, `Header`, `ContentTemplate`, `ConsentTemplate`,
  `DeliveryChain`, `SmsSenderReadinessRequest`, `SmsSenderReadinessDecision`,
  `PrerequisiteBlocker`.
- Enums: `DltRegistrationStatus`, `SmsRoute`, `SmsSenderReadiness`; the stable `REASON_*` codes.

## Tests

`tests/test_sms_sender_prerequisites.py` pins the behaviour: the reproducible `READY`, each
prerequisite's fail-closed block (unregistered PE, unregistered/unbound header, unregistered/
unbound content template, missing consent template on a consent route, route/category
mismatch, incomplete/unbound delivery chain), the expiry window, the consent-template
route-dependence, the collected-in-order blockers, and the headline invariant that `READY`
never authorizes a recipient and recipient consent never makes an unregistered sender routable.

The evaluator is a **pure function of structured DLT-registration inputs** — it opens no DB,
calls no adapter and consults no model — so there is no persisted read path to exercise (unlike
the DCA-087 recipient line, which read real `ConsentReceipt` rows over disposable PostgreSQL).
The "changing no state" acceptance criterion holds intrinsically: nothing is read or written.

## Manual check

```
python scripts/manual_check.py DCA-088
```

Runs, in isolated synthetic Businesses A/B:

1. a fully-registered, `ACTIVE`, bound, category-matched promotional sender → `READY`;
2. the same sender with the **header removed** → `BLOCKED` on `header_not_registered`;
3. the promotional header/template run on the **transactional route** → `BLOCKED` on
   `route_category_mismatch` (mismatched setup);
4. the promotional route with a **missing consent template** → `BLOCKED` on
   `consent_template_not_registered`;
5. Business B's own registered sender → `READY`, unaffected (tenant-isolated inputs), with
   recipient consent recorded but **not consulted** — `READY` still does not authorize the
   recipient (`authorizes_recipient = False`).

Local synthetic only; no AI, no paid action, no network, no send; the decision changes no state.
```
