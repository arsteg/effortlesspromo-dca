"""DCA-148 — route missing fields through a provider waterfall.

The single job of this story: given a set of still-MISSING fields and a pool of candidate providers,
decide — per field — the ORDERED fallback of providers to try, and STOP as soon as the field's
requirement is met. It is a pure PLANNING decision, the direct field-level analogue of
:func:`~app.services.discovery_planner.resolve_allowed_sources`: it composes the already-accepted seams
(the DCA-048 rights preflight, the DCA-044 catalog coverage) and performs no provider/model/paid work of
its own. There is no second gateway, no fetch, and no state change — reproducing a route (or a denial)
is perfectly deterministic.

Each candidate provider is ranked for a field on the FOUR dimensions the story names:

- **rights** — the business must still be approved+entitled to ``COLLECT`` the field from that source for
  the purpose, decided ONLY through the sanctioned :class:`~app.services.source_preflight.SourcePreflight`
  gate (catalogued+ENABLED + platform ceiling + business entitlement, at ``now``). A denied provider is
  recorded (:data:`OUTCOME_DENIED_RIGHTS`, carrying the preflight reason) and is NEVER selected. In the
  running service every source is a disabled draft, so the preflight denies every provider and the
  waterfall routes nothing — acquisition stays a disabled future capability.
- **coverage** — the field must be one the catalog source actually offers (:attr:`Source.fields`). A
  provider that does not cover the field is not a candidate for it (:data:`OUTCOME_NO_COVERAGE`).
- **freshness** — a provider whose data is older than the field requirement's ``max_age_days`` is too
  stale to satisfy it (:data:`OUTCOME_STALE`).
- **marginal cost** — the eligible (rights+coverage+fresh) providers are ordered CHEAPEST-first (ties
  broken by freshest, then by source key) so the waterfall spends the least to meet the requirement.

**The two acceptance guarantees:**

- **No redundant next-provider call after success.** The walk stops the instant a field's requirement is
  met; every cheaper-ranked-after provider is recorded :data:`OUTCOME_NOT_REACHED` and is never selected
  — there is no "call the next provider anyway". ``FieldRoute.selected`` is exactly the providers the
  requirement needed.
- **Shared upstream sources do not count as independent corroboration.** Corroboration is counted by
  DISTINCT upstream origin: a provider whose ``upstream_key`` is already represented adds no independent
  evidence, so it is recorded :data:`OUTCOME_SKIPPED_SHARED_UPSTREAM` and does not advance the
  independent-source count (two resellers of one registry count as one).

The planner is bound to ONE verified business scope (like the DCA-047 entitlement store / the DCA-048
preflight) so a tenant's routing can only consult that tenant's entitlements; it stages nothing and
commits nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field as dc_field
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import SourceOperation
from app.services.discovery_planner import PLAN_PURPOSE
from app.services.source_catalog import SourceCatalogStore
from app.services.source_preflight import PreflightRequest, SourcePreflight

# The purpose the waterfall's source access is bound to — the same purpose-limited grant the discovery
# plan runs under (rights are purpose-bound, never blanket). The caller may override it.
DEFAULT_PURPOSE = PLAN_PURPOSE

# The source operation a field route is authorized for: collecting the field's value.
ROUTE_OPERATION = SourceOperation.COLLECT.value

# Per-provider outcome codes recorded on a field route — why a candidate was selected, skipped or
# excluded. Stable strings so a reader can audit exactly how the waterfall resolved.
OUTCOME_SELECTED = "selected"  # chosen — contributes an independent source for the field
OUTCOME_SKIPPED_SHARED_UPSTREAM = (
    "skipped_shared_upstream"  # fresh+entitled but a duplicate upstream
)
OUTCOME_NOT_REACHED = (
    "not_reached"  # the requirement was already met before this provider (no call)
)
OUTCOME_DENIED_RIGHTS = "denied_rights"  # the preflight withheld rights (reason carries the code)
OUTCOME_NO_COVERAGE = "no_coverage"  # the source does not offer this field at all
OUTCOME_STALE = "stale"  # the provider's data is older than the field's max_age_days

# Route-level reason codes — why a field could NOT be satisfied (``None`` when satisfied). Each is
# reproducible with no state change (the "relevant denial" the acceptance criterion asks to reproduce).
REASON_NO_CANDIDATE = "no_candidate_provider"  # no provider even covers the field
REASON_NO_ENTITLED = "no_entitled_provider"  # covering providers exist, but all rights-denied
REASON_ALL_STALE = "all_candidates_stale"  # covering+entitled, but all fail the freshness bar
REASON_INSUFFICIENT = (
    "insufficient_independent_corroboration"  # eligible, but too few distinct upstreams
)


@dataclass(frozen=True)
class FieldRequirement:
    """One still-missing field to route, plus how much the caller needs to consider it satisfied.

    ``min_independent_sources`` is the corroboration bar: how many DISTINCT upstream origins must supply
    the field (default 1 — a single source; >1 asks for independent corroboration). ``max_age_days`` is
    the freshness bar: a provider whose data is older is too stale to satisfy the field (``None`` ⇒ any
    age accepted). ``min_independent_sources`` is clamped to at least 1 — a field always needs a source.
    """

    field: str
    min_independent_sources: int = 1
    max_age_days: int | None = None

    @property
    def required_independent_sources(self) -> int:
        return max(1, int(self.min_independent_sources))


@dataclass(frozen=True)
class ProviderCandidate:
    """One candidate provider the waterfall may route a field through — a declarative descriptor only.

    ``source_key`` names a catalog :class:`~app.db.models.Source` (coverage + rights are read from it;
    an uncatalogued key is simply denied by the preflight). ``age_days`` is the age of this provider's
    data (freshness), ``marginal_cost_minor`` the incremental INR **minor units** (paise) to collect the
    field from it (the ordering key), and ``upstream_key`` the shared-origin group: providers sharing it
    are NOT independent corroboration (empty ⇒ the provider is its own independent origin, keyed by its
    source key). No value here authorizes anything — the rights chain still decides, and nothing is
    fetched. These are caller-supplied / synthetic descriptors (there is no live provider egress).
    """

    source_key: str
    age_days: int = 0
    marginal_cost_minor: int = 0
    upstream_key: str = ""

    @property
    def origin(self) -> str:
        """The independent-origin key: the explicit shared upstream, else the provider's own source."""
        return (self.upstream_key or "").strip() or (self.source_key or "").strip()


@dataclass(frozen=True)
class FieldConsideration:
    """One provider's place in a field's waterfall — the auditable per-candidate record.

    ``outcome`` is one of the ``OUTCOME_*`` codes; ``rank`` is the 1-based position in the ordered
    eligible waterfall (``0`` for a candidate filtered out before ordering — no coverage / denied /
    stale). ``reason`` carries the preflight denial code for :data:`OUTCOME_DENIED_RIGHTS`.
    """

    source_key: str
    outcome: str
    rank: int = 0
    origin: str = ""
    age_days: int | None = None
    marginal_cost_minor: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class FieldRoute:
    """The resolved waterfall for ONE field — inert and fully inspectable (nothing was fetched).

    ``selected`` is exactly the providers the requirement needed, in the order they were chosen;
    ``considered`` records every evaluated candidate and its outcome (ranked eligible first, then the
    filtered-out ones). ``reason`` names the stable denial code when the field could not be satisfied.
    """

    field: str
    satisfied: bool
    required_independent_sources: int
    independent_sources: int
    selected: tuple[str, ...]
    considered: tuple[FieldConsideration, ...]
    reason: str | None = None

    @property
    def calls_planned(self) -> int:
        """How many provider calls the waterfall would make — exactly the selected providers, no more."""
        return len(self.selected)

    @property
    def providers_not_reached(self) -> tuple[str, ...]:
        """The eligible providers never reached because the requirement was already met (the witness
        that there is no redundant next-provider call after success)."""
        return tuple(c.source_key for c in self.considered if c.outcome == OUTCOME_NOT_REACHED)


@dataclass(frozen=True)
class WaterfallResult:
    """The inert outcome of routing a set of missing fields for one business."""

    business_id: str
    purpose: str
    routes: tuple[FieldRoute, ...] = dc_field(default_factory=tuple)

    @property
    def fully_satisfied(self) -> bool:
        """True iff every routed field met its requirement."""
        return bool(self.routes) and all(r.satisfied for r in self.routes)

    def route_for(self, field: str) -> FieldRoute | None:
        """The route for one field, or ``None`` if it was not routed."""
        key = (field or "").strip()
        for route in self.routes:
            if route.field == key:
                return route
        return None


class ProviderWaterfallPlanner:
    """Route missing fields through a per-field provider waterfall, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the DCA-048 :class:`SourcePreflight` (rights) and
    the DCA-044 :class:`SourceCatalogStore` (coverage). Every query is confined to this business — there
    is no argument to widen the scope. Pure: it stages nothing, commits nothing and calls no adapter.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id
        self._preflight = SourcePreflight(session, business_id)
        self._catalog = SourceCatalogStore(session)

    def route_fields(
        self,
        requirements: Sequence[FieldRequirement],
        providers: Sequence[ProviderCandidate],
        *,
        purpose: str = DEFAULT_PURPOSE,
        now: datetime | None = None,
    ) -> WaterfallResult:
        """Route every missing field in ``requirements`` through the candidate ``providers``.

        A pure, deterministic read: the same inputs always yield the same routes, and nothing is
        fetched, staged or committed — so both a satisfied route and a denial are perfectly
        reproducible. ``purpose`` binds the rights check (purpose-limited, never blanket).
        """
        resolved_purpose = (purpose or "").strip() or DEFAULT_PURPOSE
        routes = tuple(
            self._route_field(req, providers, purpose=resolved_purpose, now=now)
            for req in requirements
        )
        return WaterfallResult(
            business_id=self._business_id, purpose=resolved_purpose, routes=routes
        )

    def _route_field(
        self,
        requirement: FieldRequirement,
        providers: Sequence[ProviderCandidate],
        *,
        purpose: str,
        now: datetime | None,
    ) -> FieldRoute:
        """Resolve the ordered waterfall for one field (the core decision)."""
        field_name = (requirement.field or "").strip()
        required = requirement.required_independent_sources

        # 1) Filter each candidate on coverage → rights → freshness (deny-by-default, fixed order).
        eligible: list[ProviderCandidate] = []
        rejected: list[FieldConsideration] = []
        covering = 0
        entitled = 0
        for cand in providers:
            source_key = (cand.source_key or "").strip()

            # coverage — the catalog source must actually offer this field.
            source = self._catalog.get(source_key)
            covers = source is not None and field_name in {str(f) for f in (source.fields or [])}
            if not covers:
                rejected.append(
                    FieldConsideration(
                        source_key=source_key,
                        outcome=OUTCOME_NO_COVERAGE,
                        origin=cand.origin,
                        age_days=cand.age_days,
                        marginal_cost_minor=cand.marginal_cost_minor,
                    )
                )
                continue
            covering += 1

            # rights — the sanctioned preflight gate (purpose-bound, at ``now``). Never bypassed.
            decision = self._preflight.authorize(
                PreflightRequest(
                    source_key=source_key,
                    operation=ROUTE_OPERATION,
                    purpose=purpose,
                    field=field_name,
                ),
                now=now,
            )
            if not decision.allowed:
                rejected.append(
                    FieldConsideration(
                        source_key=source_key,
                        outcome=OUTCOME_DENIED_RIGHTS,
                        origin=cand.origin,
                        age_days=cand.age_days,
                        marginal_cost_minor=cand.marginal_cost_minor,
                        reason=decision.reason,
                    )
                )
                continue
            entitled += 1

            # freshness — too old to satisfy the field's max_age bar.
            if requirement.max_age_days is not None and cand.age_days > requirement.max_age_days:
                rejected.append(
                    FieldConsideration(
                        source_key=source_key,
                        outcome=OUTCOME_STALE,
                        origin=cand.origin,
                        age_days=cand.age_days,
                        marginal_cost_minor=cand.marginal_cost_minor,
                    )
                )
                continue

            eligible.append(cand)

        # 2) Order the eligible providers: CHEAPEST marginal cost first, ties broken by freshest
        #    (lowest age), then by source key — a deterministic ordered fallback.
        ordered = sorted(
            eligible,
            key=lambda c: (c.marginal_cost_minor, c.age_days, c.source_key),
        )

        # 3) Walk the waterfall, counting DISTINCT upstream origins; STOP once the requirement is met.
        independent: set[str] = set()
        selected: list[str] = []
        walked: list[FieldConsideration] = []
        for rank, cand in enumerate(ordered, start=1):
            if len(independent) >= required:
                # Requirement already met — no redundant next-provider call.
                outcome = OUTCOME_NOT_REACHED
            elif cand.origin in independent:
                # Shared upstream — adds no INDEPENDENT corroboration, so it is not selected/called.
                outcome = OUTCOME_SKIPPED_SHARED_UPSTREAM
            else:
                independent.add(cand.origin)
                selected.append(cand.source_key)
                outcome = OUTCOME_SELECTED
            walked.append(
                FieldConsideration(
                    source_key=cand.source_key,
                    outcome=outcome,
                    rank=rank,
                    origin=cand.origin,
                    age_days=cand.age_days,
                    marginal_cost_minor=cand.marginal_cost_minor,
                )
            )

        satisfied = len(independent) >= required
        reason = self._denial_reason(
            satisfied=satisfied, covering=covering, entitled=entitled, eligible=len(eligible)
        )
        return FieldRoute(
            field=field_name,
            satisfied=satisfied,
            required_independent_sources=required,
            independent_sources=len(independent),
            selected=tuple(selected),
            considered=tuple(walked) + tuple(rejected),
            reason=reason,
        )

    @staticmethod
    def _denial_reason(
        *, satisfied: bool, covering: int, entitled: int, eligible: int
    ) -> str | None:
        """The stable reason a field could not be satisfied (``None`` when it was)."""
        if satisfied:
            return None
        if covering == 0:
            return REASON_NO_CANDIDATE
        if entitled == 0:
            return REASON_NO_ENTITLED
        if eligible == 0:
            return REASON_ALL_STALE
        return REASON_INSUFFICIENT


__all__ = [
    "DEFAULT_PURPOSE",
    "ROUTE_OPERATION",
    "OUTCOME_SELECTED",
    "OUTCOME_SKIPPED_SHARED_UPSTREAM",
    "OUTCOME_NOT_REACHED",
    "OUTCOME_DENIED_RIGHTS",
    "OUTCOME_NO_COVERAGE",
    "OUTCOME_STALE",
    "REASON_NO_CANDIDATE",
    "REASON_NO_ENTITLED",
    "REASON_ALL_STALE",
    "REASON_INSUFFICIENT",
    "FieldRequirement",
    "ProviderCandidate",
    "FieldConsideration",
    "FieldRoute",
    "WaterfallResult",
    "ProviderWaterfallPlanner",
]
