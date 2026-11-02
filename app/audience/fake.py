"""DCA-154 — deterministic, in-memory EffortlessPromo audience-sync fake for local tests.

The only audience-sync implementation today: the live HTTP adapter into the platform's CRM is a later
story (blocked on DCA-001 B1 — there is no service-to-service contract into the .NET API yet). It is
dev/test-only — production refuses it (``build_audience_sync_port``), mirroring the delivery / platform-
profile fakes and the seed's dev-only stance. Every ack it produces is flagged ``synthetic``, so a test
answer can never be mistaken for a real one. It performs NO CRM write and holds NO audience: a PENDING /
ACKNOWLEDGED ack only models the platform taking / completing the request; a refused request is DISABLED.

One knob makes the DCA-154 outcomes testable without a wall clock or a real CRM:

- ``acknowledges`` — when ``True`` the fake platform returns an ACKNOWLEDGED ack carrying synthetic
  external ids (a completed write); when ``False`` it returns a PENDING hand-off (queued, no external
  ids). The two outcomes differ by construction, which is the acceptance criterion.

The refusal cases are NOT a knob: they come from the contract's own :func:`authorize_audience_sync`
gate — a sendable sync, a restricted CONTACT field under a ``research_only`` acceptance, or a disallowed
purpose each yields a DISABLED ack with the deny reason, never a silent accept and never a promotion.
External ids are DETERMINISTIC in the request key, so a replayed sync returns the same handles.
"""

from __future__ import annotations

from typing import Any

from app.audience.contract import (
    AudienceSyncAck,
    AudienceSyncDecision,
    AudienceSyncRequest,
    AudienceSyncStatus,
    ExternalRecordRef,
    authorize_audience_sync,
)

SYNTHETIC_VERSION = "fake-audience-sync-v1"


class AudienceSyncContractDisabled(RuntimeError):
    """Raised when an audience-sync port is requested where the contract is not wired (production).

    Mirrors :class:`~app.delivery.fake.DeliveryContractDisabled` /
    :class:`~app.profiles.fake.PlatformProfileContractDisabled`.
    """


class FakeEffortlessPromoAudienceSyncPort:
    """A deterministic :class:`~app.audience.contract.AudienceSyncPort`. Pure: no I/O, no CRM, no store.

    ``acknowledges=False`` (the default) makes every allowed request a PENDING hand-off (queued, no
    external ids); ``acknowledges=True`` returns an ACKNOWLEDGED ack carrying synthetic external ids (a
    completed write). A refused request (restricted field / disallowed purpose / sendable) is always a
    DISABLED ack regardless of this knob. External ids are DETERMINISTIC in the request key so a replayed
    sync returns the same handles — the contract's idempotency, visible. A sync is never recipient
    consent: ``confers_consent`` on the ack is always ``False``.
    """

    def __init__(
        self,
        *,
        acknowledges: bool = False,
        version: str = SYNTHETIC_VERSION,
    ) -> None:
        self._acknowledges = acknowledges
        self._version = version

    def sync(self, request: AudienceSyncRequest) -> AudienceSyncAck:
        """Request a CRM sync through the (fake) platform. Fail-closed: a sendable subject, a restricted
        CONTACT field under a ``research_only`` acceptance, or a disallowed purpose returns a DISABLED ack
        carrying the deny reason — never a silent accept, never a promotion. A cleared request is taken
        into the CRM: PENDING (queued, no external ids) unless the host acknowledges, in which case
        ACKNOWLEDGED with deterministic external ids."""
        decision = authorize_audience_sync(request)
        if not decision.allowed:
            return AudienceSyncAck(
                accepted=False,
                status=AudienceSyncStatus.DISABLED,
                reason=decision.reason,
                request_key=request.request_key,
                synthetic=True,
            )
        if self._acknowledges:
            external_ref = ExternalRecordRef(
                crm_record_id=f"synthetic-crm-{request.request_key}",
                audience_id=f"synthetic-audience-{request.business_id}",
                external_version=self._version,
            )
            return AudienceSyncAck(
                accepted=True,
                status=AudienceSyncStatus.ACKNOWLEDGED,
                reason=AudienceSyncDecision.OK,
                request_key=request.request_key,
                external_ref=external_ref,
                synthetic=True,
            )
        return AudienceSyncAck(
            accepted=True,
            status=AudienceSyncStatus.PENDING,
            reason=AudienceSyncDecision.OK,
            request_key=request.request_key,
            synthetic=True,
        )


def build_audience_sync_port(settings: Any) -> FakeEffortlessPromoAudienceSyncPort:
    """The audience-sync port for this deployment.

    Only the dev/test fake exists; the live adapter into the platform's CRM is unbuilt (DCA-001 B1).
    Production therefore has NO audience-sync port — the contract stays disabled until that story ships.
    Calling this under production configuration raises rather than silently handing back a fake (which
    would risk treating a synthetic ack as a real CRM write — the exact fail-open this story forbids).
    """
    if getattr(settings, "is_production", False):
        raise AudienceSyncContractDisabled(
            "audience-sync contract is disabled in production: the live adapter into the platform CRM "
            "is not built yet (DCA-001 B1) and the synthetic fake is dev/test-only"
        )
    return FakeEffortlessPromoAudienceSyncPort()
