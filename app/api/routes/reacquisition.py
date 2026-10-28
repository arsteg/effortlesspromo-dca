"""DCA-086 — prevent reacquisition of restricted identities: the tombstone-management API.

Structured HTTP contract (for a later Web/Admin operator console) over
:class:`~app.services.reacquisition.RestrictedIdentityTombstoneStore`. It records the protected minimal
tombstones that stop a restricted platform-sourced identity from being silently re-acquired by the next
discovery cycle, and reads them back for review. The scope-aware comparison itself runs inside the
discovery cycle (:func:`~app.services.reacquisition.reacquisition_decision`); this surface only manages
the markers.

Business-plane only, reusing the SAME ``data.erase`` administrator authority that executes an erasure
(no new scope):

- **Record a tombstone** — ``POST /v1/tenants/{business_id}/restricted-identities``. Requires
  ``data.erase`` for THIS business (an ordinary user, a read-only viewer and a non-member — incl. a
  platform operator — are denied before anything is written). The raw identifier is fingerprinted
  immediately and NEVER stored. A bad subject type / origin / purpose / retention / missing identifier
  is a clear 422; the write is idempotent.
- **Read the tombstones** — ``GET /v1/tenants/{business_id}/restricted-identities``. The same authority
  reads the durable ledger; the projection carries the pseudonymous fingerprint + provenance only, never
  a raw identifier.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.audit.sink import AuditPayloadError
from app.core.security import require_business_permission_on_path
from app.db.models import (
    ReacquisitionPurpose,
    RestrictedIdentityOrigin,
    RestrictedIdentityTombstone,
    RestrictionRetentionMode,
)
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.services.reacquisition import (
    ReacquisitionAuthorizationError,
    ReacquisitionError,
    ReacquisitionPrincipal,
    RestrictedIdentityTombstoneStore,
)

router = APIRouter(prefix="/v1/tenants", tags=["reacquisition"])

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]
# Recording / reading reacquisition tombstones is part of the restriction/erasure authority: an
# admin/owner of the business holds ``data.erase``; an ordinary acting user, a read-only viewer and a
# non-member (incl. a platform operator) do not.
TombstoneDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.DATA_ERASE))
]


class TombstoneBody(BaseModel):
    # extra='forbid' so an UNSUPPORTED field (a typo / a field from a future capability) is a clear 422.
    model_config = ConfigDict(extra="forbid")

    # The subject kind the restricted identity is (organization | person).
    subjectType: str
    # The raw identifier to restrict (e.g. an org registry id). Fingerprinted immediately, NEVER stored.
    identifier: str
    # The sourcing origin this tombstone speaks for (default: platform).
    origin: str = RestrictedIdentityOrigin.PLATFORM.value
    # OPTIONAL descriptive scheme of the identifier (e.g. "org_identifier").
    identityScheme: str = ""
    # The declared retention purpose (default: prevent_reacquisition).
    purpose: str = ReacquisitionPurpose.PREVENT_REACQUISITION.value
    # Retention: until_lifted (default, indefinite) or until_date (then retainUntil is required).
    retentionMode: str = RestrictionRetentionMode.UNTIL_LIFTED.value
    retainUntil: datetime | None = None
    # OPTIONAL opaque provenance of the restriction/erasure this tombstone answers.
    originKind: str = ""
    originRef: str = ""


def _tombstone_view(tombstone: RestrictedIdentityTombstone) -> dict:
    """The structured projection — the PSEUDONYMOUS fingerprint + provenance only.

    Carries the identity fingerprint (a pseudonym, explicitly labelled with its ``hashAlgorithm``, never
    "anonymous") but NEVER the raw identifier, so a reviewer can see WHAT is restricted and WHY without
    the identity being recoverable from this projection.
    """
    return {
        "tombstoneId": tombstone.id,
        "businessId": tombstone.business_id,
        "subjectType": tombstone.subject_type,
        "identityScheme": tombstone.identity_scheme or None,
        "identityHash": tombstone.identity_hash,
        "hashAlgorithm": tombstone.hash_algorithm,
        "origin": tombstone.origin,
        "purpose": tombstone.purpose,
        "retentionMode": tombstone.retention_mode,
        "retainUntil": tombstone.retain_until.isoformat() if tombstone.retain_until else None,
        "originKind": tombstone.origin_kind or None,
        "originRef": tombstone.origin_ref or None,
        "authorityPlane": tombstone.authority_plane,
        "authorityScope": tombstone.authority_scope,
        "createdAt": tombstone.created_at.isoformat() if tombstone.created_at else None,
    }


@router.post("/{business_id}/restricted-identities", status_code=201)
def record_tombstone(
    business_id: str,
    body: TombstoneBody,
    context: TombstoneDep,
    session: SessionDep,
    idem: IdempotencyDep,
) -> dict:
    """Record a reacquisition tombstone for one identity and return it.

    Requires the ``data.erase`` scope: an ordinary user, a read-only viewer or a non-member (incl. a
    platform operator) is denied (403) before anything is written. A bad subject type / origin / purpose
    / retention or a missing identifier is a clear 422 — each staging nothing. The raw identifier is
    fingerprinted immediately and never stored. Idempotent: re-restricting the same identity reuses the
    existing tombstone. Retry-safe with an ``Idempotency-Key``.
    """
    store = RestrictedIdentityTombstoneStore(session, context.business_id)
    principal = ReacquisitionPrincipal(subject=context.subject, roles=context.roles)
    try:
        tombstone = store.record(
            body.subjectType,
            body.identifier,
            principal,
            origin=body.origin,
            identity_scheme=body.identityScheme,
            purpose=body.purpose,
            retention_mode=body.retentionMode,
            retain_until=body.retainUntil,
            origin_kind=body.originKind,
            origin_ref=body.originRef,
        )
    except ReacquisitionAuthorizationError as exc:  # pragma: no cover - dependency gates first
        raise HTTPException(status_code=403, detail=exc.code) from exc
    except ReacquisitionError as exc:
        # unknown subject type / origin / purpose / retention / missing identifier → a clear 422.
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except AuditPayloadError as exc:
        # Opaque provenance carried raw contact/secret data — refuse, writing nothing.
        raise HTTPException(status_code=422, detail="reacquisition_reference_not_opaque") from exc

    session.flush()
    view = _tombstone_view(tombstone)
    return commit_idempotent(session, idem, view, status_code=201)


@router.get("/{business_id}/restricted-identities")
def list_tombstones(business_id: str, context: TombstoneDep, session: SessionDep) -> list[dict]:
    """Every reacquisition tombstone for this business, oldest first (authorized operator only).

    Requires the ``data.erase`` scope. Read-only; the projection carries the pseudonymous fingerprint +
    provenance only, so it cannot be used to recover a raw identifier.
    """
    store = RestrictedIdentityTombstoneStore(session, context.business_id)
    return [_tombstone_view(t) for t in store.tombstones()]
