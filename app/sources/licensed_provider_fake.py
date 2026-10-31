"""DCA-125 — deterministic, in-memory synthetic licensed-provider client for local tests.

The ONLY licensed-provider implementation today: the live HTTP adapter to a real paid B2B data provider
is a later story and stays DISABLED. It is dev/test-only — production refuses it
(:func:`build_licensed_provider_client`), mirroring the DCA-014 identity and DCA-096 delivery fakes and
the seed's dev-only stance. **It performs NO network call, holds NO provider SDK/key, and spends
NOTHING** — it returns a deterministic synthetic answer so the DCA-125 adapter's reserve → call →
reconcile path is reviewable without a real/paid call. Every answer is flagged ``synthetic=True`` so a
fixture can never be mistaken for a real paid one.

Three knobs make the behaviour and its failure/ambiguous cases testable without a wall clock or a real
provider:

- ``outcome`` — ``SUCCEEDED`` (default) returns the synthetic organization fields and a reported cost;
  ``FAILED`` returns no fields and zero cost (a confirmed no-call); ``UNCERTAIN`` returns no trusted
  fields and leaves the cost unknown (a dispatch that could not be confirmed).
- ``cost_minor`` — the provider's reported actual cost in INR minor units on a success (so settle /
  over-run reconciliation is exercised).
- ``include_contact_field`` — when ``True`` (default) the synthetic answer SMUGGLES one personal/contact
  field, proving the organization-only adapter DROPS it (counted, never recorded).
"""

from __future__ import annotations

from typing import Any

from app.sources.licensed_provider_contract import (
    LicensedField,
    LicensedProviderOutcome,
    LicensedProviderRequest,
    LicensedProviderResult,
)

# The synthetic provider name (the cost bucket a reservation/call is attributed to) and a plainly-fake
# marker version. Obviously synthetic; no real vendor.
SYNTHETIC_PROVIDER = "synthetic_licensed_b2b"
SYNTHETIC_VERSION = "fake-licensed-provider-v1"

# A conservative default reported cost (₹12.00) so the settle path has a real number to reconcile.
DEFAULT_COST_MINOR = 1_200


class LicensedProviderDisabled(RuntimeError):
    """Raised when a licensed-provider client is requested where the live adapter is not wired
    (production). The synthetic fake is dev/test-only; a real/paid provider call stays disabled."""


class SyntheticLicensedProviderClient:
    """A deterministic :class:`~app.sources.licensed_provider_contract.LicensedProviderClient`.

    Pure: no I/O, no network, no provider SDK/key, no spend. ``outcome`` chooses the reported outcome;
    ``cost_minor`` the reported cost on a success; ``include_contact_field`` whether the synthetic answer
    carries a stray personal/contact field (to prove the adapter drops it). The returned organization
    fields are DETERMINISTIC in ``org_ref`` so a replay is reproducible. An accepted answer is never a
    real call — ``synthetic`` is always ``True``.
    """

    def __init__(
        self,
        *,
        outcome: LicensedProviderOutcome = LicensedProviderOutcome.SUCCEEDED,
        cost_minor: int = DEFAULT_COST_MINOR,
        include_contact_field: bool = True,
        provider: str = SYNTHETIC_PROVIDER,
    ) -> None:
        self._outcome = outcome
        self._cost_minor = cost_minor
        self._include_contact_field = include_contact_field
        self._provider = provider

    def fetch_organization(self, request: LicensedProviderRequest) -> LicensedProviderResult:
        """Return a deterministic synthetic answer for ``request`` — never a real/paid call.

        A ``FAILED`` outcome returns no fields and zero cost (confirmed no-call); an ``UNCERTAIN`` outcome
        returns no trusted fields and zero reported cost (unknown — the adapter preserves the hold rather
        than settling it); a ``SUCCEEDED`` outcome returns the synthetic organization fields (plus, when
        ``include_contact_field`` is set, one personal field the organization-only adapter must DROP) and
        the reported cost."""
        if self._outcome is LicensedProviderOutcome.FAILED:
            return LicensedProviderResult(
                outcome=LicensedProviderOutcome.FAILED,
                fields=(),
                cost_minor=0,
                provider=self._provider,
                outcome_code="provider_rejected",
                synthetic=True,
            )
        if self._outcome is LicensedProviderOutcome.UNCERTAIN:
            return LicensedProviderResult(
                outcome=LicensedProviderOutcome.UNCERTAIN,
                fields=(),
                cost_minor=0,
                provider=self._provider,
                outcome_code="gateway_timeout",
                synthetic=True,
            )

        # SUCCEEDED — deterministic organization fields derived from the opaque org_ref.
        fields: list[LicensedField] = [
            LicensedField(field_key="legal_name", field_value=f"Org {request.org_ref}"),
            LicensedField(field_key="industry", field_value="Information Technology"),
            LicensedField(field_key="employee_count", field_value="250"),
            LicensedField(field_key="annual_revenue_band", field_value="INR 50-100 Cr"),
            LicensedField(field_key="hq_state", field_value="MH"),
        ]
        if self._include_contact_field:
            # A stray PERSONAL field the provider returns — the organization-only adapter DROPS it.
            fields.append(
                LicensedField(field_key="founder_email", field_value="founder@synthetic.example")
            )
        return LicensedProviderResult(
            outcome=LicensedProviderOutcome.SUCCEEDED,
            fields=tuple(fields),
            cost_minor=self._cost_minor,
            provider=self._provider,
            outcome_code="completed",
            prompt_tokens=180,
            completion_tokens=60,
            total_tokens=240,
            synthetic=True,
        )


def build_licensed_provider_client(settings: Any) -> SyntheticLicensedProviderClient:
    """The licensed-provider client for this deployment.

    Only the dev/test synthetic fake exists; the live adapter to a real paid B2B provider is unbuilt.
    Production therefore has NO licensed-provider client — the contract stays DISABLED until that story
    ships (with its own credential, rulepack and rights review). Calling this under production
    configuration raises rather than silently handing back a fake (which would risk treating a synthetic
    answer as a real paid one), so no real/paid provider call can ever occur.
    """
    if getattr(settings, "is_production", False):
        raise LicensedProviderDisabled(
            "licensed-provider contract is disabled in production: the live paid adapter is not built "
            "yet and the synthetic fake is dev/test-only (no real/paid provider call is permitted)"
        )
    return SyntheticLicensedProviderClient()
