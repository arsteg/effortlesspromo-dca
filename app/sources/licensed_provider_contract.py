"""DCA-125 — the licensed-provider adapter contract: the typed port to ONE paid B2B data provider.

Key: ``licensed-adapter`` · source sections 6, 8, 9. Stage: Approved source access.

The DCA never calls a paid/licensed data provider itself today. A licensed provider is an external,
metered API whose every call COSTS money, so the only shape the DCA can legitimately define now is a
typed *fetch-organization* port plus a deterministic synthetic fake — the LIVE adapter stays DISABLED.
This mirrors the DCA-014 identity and DCA-096 delivery contracts exactly: typed shapes + a client
``Protocol`` here, a synthetic fake alongside (``app/sources/licensed_provider_fake.py``), and a live
HTTP adapter deferred to a later story. The contract stays disabled in production until that adapter
ships; the fake never stands in for it.

The provider is modelled as an ORGANIZATION-ONLY enrichment source: given an opaque organization
reference the business already holds, it returns organization-level fields (and, in a synthetic fixture,
possibly a stray personal/contact field — which the DCA-125 adapter DROPS, never records). The call is
metered: the result carries the provider's REPORTED actual cost (in INR minor units — the service is
India-only) and an outcome that is SUCCEEDED / FAILED / UNCERTAIN, so the adapter can reconcile the
up-front budget hold against what actually happened (DCA-114) and keep a timeout-after-dispatch
(``UNCERTAIN``, a cost MAY exist) strictly distinct from a confirmed no-call (``FAILED``, no cost).

Nothing here performs a call, spends, or reaches a network: these are pure value objects + a Protocol.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# The one currency a licensed-provider call is priced in. The service is India-only, so a synthetic
# provider quotes INR minor units (paise); the DCA-113/114 budget ledger is INR, so the reported cost
# reconciles directly against the hold with no fabricated foreign-exchange conversion.
PROVIDER_CURRENCY = "INR"


class LicensedProviderOutcome(str, Enum):
    """What a licensed-provider call reports happened — the three outcomes the adapter reconciles.

    - ``SUCCEEDED`` — the provider CONFIRMED completion: the returned fields and the reported actual cost
      are known, so the tied budget hold is settled at the actual cost (DCA-114).
    - ``FAILED`` — a CONFIRMED no-call failure: it is KNOWN no billable call took effect (a pre-dispatch
      refusal or a definitive provider rejection), so the tied hold is released in full. No cost occurred.
    - ``UNCERTAIN`` — the call was dispatched and then its outcome could NOT be confirmed (a timeout /
      lost response after possible external acceptance). A billable cost MAY exist, so it is kept strictly
      distinct from ``FAILED``: the tied hold is PRESERVED (counts in full) awaiting the DCA-116 resolver,
      and NO returned data is trusted/recorded.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class LicensedField(BaseModel):
    """One field a licensed provider returned about an organization (``field_key`` + ``field_value``).

    Shape only. The DCA-125 adapter records a returned field ONLY when its key is on the adapter's
    organization-field allowlist AND its value is not a personal/contact fragment — so an organization-
    only adapter can never record a contact field the provider happens to return."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_key: str = Field(min_length=1)
    field_value: str = Field(min_length=1)


class LicensedProviderRequest(BaseModel):
    """One would-be licensed-provider organization fetch, as the adapter hands it to the port.

    ``org_ref`` is the OPAQUE organization reference the provider looks the record up by (e.g. a
    registration identifier the business already holds); it is never a personal identifier. ``request_id``
    is the gateway-style idempotency identity the metered call would be issued under (so a retried call is
    the SAME logical call, never double-charged). ``requested_fields`` optionally narrows which
    organization fields are asked for (empty ⇒ the provider's default set). ``correlation_id`` is carried
    through so a (future) HTTP call and its logs share one id. There is deliberately no credential/secret
    here — the live adapter would hold those, and it is DISABLED."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    org_ref: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    requested_fields: tuple[str, ...] = ()
    correlation_id: str | None = None


class LicensedProviderResult(BaseModel):
    """What a licensed-provider call returns — the fields, the metered cost, and the outcome.

    ``outcome`` is the load-bearing field (see :class:`LicensedProviderOutcome`): it decides whether the
    adapter settles, releases or preserves the budget hold. ``fields`` are the organization fields the
    provider returned (the adapter still allowlist-filters and contact-screens them — a returned contact
    field is DROPPED). ``cost_minor`` is the provider's REPORTED actual cost in INR **minor units**
    (meaningful only on ``SUCCEEDED``; a failed call cost nothing and an uncertain call's cost is unknown
    and never fabricated). ``outcome_code`` is an opaque provider outcome/receipt code; the token counts
    are optional numeric usage recorded on a success. ``synthetic`` marks a non-production fixture so a
    fake answer can never be mistaken for a real paid one."""

    model_config = ConfigDict(frozen=True)

    outcome: LicensedProviderOutcome
    fields: tuple[LicensedField, ...] = ()
    cost_minor: int = 0
    currency: str = PROVIDER_CURRENCY
    provider: str = Field(min_length=1)
    outcome_code: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    synthetic: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome is LicensedProviderOutcome.SUCCEEDED


@runtime_checkable
class LicensedProviderClient(Protocol):
    """Port the DCA uses to fetch organization fields from ONE paid/licensed B2B data provider.

    Synchronous, matching the other service-to-service clients (identity / delivery / AI). There is
    exactly one method — ``fetch_organization`` — and it is the metered, chargeable call the DCA-125
    adapter wraps in a budget reservation + provider-call lifecycle + reconciliation. The ONLY
    implementation today is the dev/test synthetic fake; the live HTTP adapter is a later story and until
    it ships the contract stays DISABLED in production (``build_licensed_provider_client`` refuses it), so
    no real/paid call can ever occur.
    """

    def fetch_organization(self, request: LicensedProviderRequest) -> LicensedProviderResult: ...
