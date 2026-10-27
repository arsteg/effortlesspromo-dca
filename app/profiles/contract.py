"""DCA-037 — the typed *platform profile* contract + the client Protocol.

Shapes only — no transport, no store, and (critically) **no host-table access**. This is how the DCA
asks the platform "give me business B's own profile and its Brand KB facts" so it can stage a local,
provenance-bearing draft of them. A request names the business; a snapshot carries the facts the
platform reports plus the two things an imported draft must retain: the **source version** the facts
were read at and the **business scope** they belong to.

Mirrors the DCA-014 identity and DCA-022 entitlement contracts: shapes here, a deterministic synthetic
fake alongside (``app/profiles/fake.py``), and a live HTTP adapter deferred to a later story (B1). The
contract stays disabled in production until that adapter ships — the fake never stands in for it.

Authority is always the platform's (blocker B3): the DCA's ``X-Service-Auth`` authenticates the caller
service, NOT a tenant, so this contract only ever fetches a business the DCA has itself authorized.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class PlatformProfileRequest(BaseModel):
    """Fetch one business's platform profile snapshot (DCA -> platform, B1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    # Carried through so the (future) HTTP call and its logs share one id (DCA-012).
    correlation_id: str | None = None


class PlatformProfileSnapshot(BaseModel):
    """An immutable point-in-time view of a business's own profile + Brand KB facts, as the platform
    reports it.

    ``found`` is ``False`` when the platform has no profile for the business (an empty snapshot the
    importer refuses to stage, rather than inventing a blank draft). ``source`` / ``source_version``
    are the provenance an imported draft retains: the origin contract and the exact version the facts
    were read at. ``business_id`` is the scope the facts belong to. ``synthetic`` marks a non-production
    fixture so an imported draft can never be mistaken for one built from a real platform profile.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    found: bool = True
    # The business's own profile aspects (the DCA-036 canonical shape).
    products: tuple[str, ...] = ()
    business_type: str = ""
    value_proposition: str = ""
    languages: tuple[str, ...] = ()
    # Brand KB facts the platform reports as confirmed-true about the business.
    facts: tuple[str, ...] = ()
    as_of: datetime
    synthetic: bool = False


@runtime_checkable
class PlatformProfileClient(Protocol):
    """Port the DCA uses to fetch a platform profile snapshot.

    Synchronous, matching the existing service-to-service client style (identity/entitlement). The only
    implementation today is the dev/test fake; the live HTTP adapter is a later story and until it
    ships the contract is disabled in production.
    """

    def fetch(self, request: PlatformProfileRequest) -> PlatformProfileSnapshot: ...
