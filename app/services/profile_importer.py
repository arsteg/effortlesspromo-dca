"""DCA-037 — import an authorized platform profile snapshot into a provenance-bearing draft.

The one job of this story: fetch a business's own profile + Brand KB facts through the typed
:mod:`app.profiles` contract (the fake today; no host-table access) and stage them as a new
:class:`~app.db.models.BusinessProfileDraft` **version** that retains the snapshot's source version and
the business scope and stays a reviewable ``draft``.

It composes the DCA-036 :class:`~app.services.profile_store.ProfileStore` (the only sanctioned draft
writer): the importer never inserts a row itself, so every DCA-036 guarantee holds unchanged — a new
draft preserves prior versions, unknown aspects are retained explicitly, and no confirmed profile is
ever created. Like the store it stages into the *caller's* session and never commits.

Fail-closed, with no state change on refusal:

- **Contract disabled** (production, until the live adapter ships, B1) → the client is ``None`` and
  the importer refuses with :data:`REASON_IMPORT_CONTRACT_DISABLED` before touching the session.
- **Nothing to import** (the platform reports no profile for the business) → the fetched snapshot is
  ``found=False`` and the importer refuses with :data:`REASON_PROFILE_NOT_FOUND` before staging
  anything, rather than inventing a blank draft.

Acceptance of a prospect / a platform profile is unrelated to recipient consent; this story touches no
sending/consent path and performs no AI call (importing a typed snapshot is deterministic).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import BusinessProfileDraft
from app.profiles import (
    PlatformProfileClient,
    PlatformProfileContractDisabled,
    PlatformProfileRequest,
    build_platform_profile_client,
)
from app.services.profile_store import ProfileDraftInput, ProfileStore

# Stable reason codes (mapped to HTTP at the route boundary). Both refuse before the session is
# mutated, so a denial leaves no draft and no partial state.
REASON_IMPORT_CONTRACT_DISABLED = "profile_import_contract_disabled"
REASON_PROFILE_NOT_FOUND = "platform_profile_not_found"


class ProfileImportError(Exception):
    """A profile import was refused with a stable code. Raised before the session is mutated."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_platform_profile_client(settings: Settings) -> PlatformProfileClient | None:
    """The platform-profile client for this deployment, or ``None`` when the contract is disabled
    (production, until the live adapter ships) — in which case an import fails closed."""
    try:
        return build_platform_profile_client(settings)
    except PlatformProfileContractDisabled:
        return None


def import_profile_snapshot(
    session: Session,
    client: PlatformProfileClient | None,
    business_id: str,
    *,
    correlation_id: str | None = None,
) -> BusinessProfileDraft:
    """Fetch the platform profile snapshot for ``business_id`` and stage it as a new draft version.

    Returns the staged draft (uncommitted — the caller owns the unit of work). The draft retains the
    snapshot's ``source``/``source_version`` as provenance and the business scope, and is always a
    ``draft`` (never a confirmed profile). Raises :class:`ProfileImportError` — before any state change
    — when the contract is disabled or the platform has no profile for the business.
    """
    if client is None:
        # Contract disabled (production, B1): fail closed before touching the session.
        raise ProfileImportError(
            REASON_IMPORT_CONTRACT_DISABLED,
            "the platform profile contract is not enabled in this deployment",
        )

    snapshot = client.fetch(
        PlatformProfileRequest(business_id=business_id, correlation_id=correlation_id)
    )
    if not snapshot.found:
        raise ProfileImportError(
            REASON_PROFILE_NOT_FOUND,
            "the platform reports no profile for this business; nothing to import",
        )

    provenance: dict[str, Any] = {
        "source": snapshot.source,
        "source_version": snapshot.source_version,
        # The source business scope the facts were fetched for (equals the draft's own business_id).
        "business_id": snapshot.business_id,
        "as_of": snapshot.as_of.isoformat(),
        "synthetic": snapshot.synthetic,
        "imported": True,
    }
    # The draft is always stored under the tenant it was fetched for (its business scope). The store,
    # not this importer, writes the row and stamps BusinessId via the tenant scope at the route.
    draft_input = ProfileDraftInput(
        products=list(snapshot.products),
        business_type=snapshot.business_type,
        value_proposition=snapshot.value_proposition,
        languages=list(snapshot.languages),
        confirmed_facts=list(snapshot.facts),
        source=snapshot.source,
        source_version=snapshot.source_version,
        provenance=provenance,
    )
    return ProfileStore(session, business_id).save_draft(draft_input)
