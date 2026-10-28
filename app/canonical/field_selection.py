"""DCA-069 — choose the canonical value of a field from its observation history, EXPLAINABLY.

This is the selection sibling of :mod:`app.matching` (DCA-067/068): a pure, deterministic,
network-free *conclusion* layer that sits on top of the append-only DCA-061
:class:`~app.db.models.FieldObservation` history. For ONE subject + ONE ``field_key`` it reads the
observations recorded about that field and decides which value is CANONICAL — and, crucially, WHY —
using **evidence reliability, verified identity, freshness and rights**, NOT a rigid source-tier rule
(official-beats-everything). It is a conclusion only: it reads nothing it is not handed, it NEVER
writes, mutates, dedups or deletes an observation, and the losing values stay untouched in history.

The load-bearing guarantee (the DCA-069 acceptance criteria):

- **Stale registry data cannot blindly replace a newer verified fact; losing values remain in
  history.** A rigid source-tier rule would let an official registry value win on provenance alone,
  even when it is stale and a newer, independently *verified* fact exists. Here reliability is ranked
  by *evidence quality* — a VERIFIED fact outranks a merely OBSERVED (e.g. registry) one, which
  outranks an INFERRED one — and freshness only decides WITHIN a reliability tier. So a newer verified
  fact is chosen over a stale registry value, and the explanation records exactly that
  (:data:`REASON_VERIFIED_OVER_STALE_REGISTRY`). Nothing is deleted: every non-winning observation is
  listed in :attr:`CanonicalSelection.preserved_observation_ids` and remains in the store.

- **Rights gate, not a source tier.** A value whose rights do not permit relying on it — no rights
  basis, a lapsed retention window, or source terms no longer in force — is INELIGIBLE to be canonical
  (a DENIAL with a stable reason code) even if it is the freshest or most reliable. It stays in
  history; it simply cannot win. This is the "relevant denial" the acceptance asks to reproduce.

- **Reproducible, no state to change.** The selection is a pure function of its inputs at a given
  ``now``, so both the winner and the denial are perfectly reproducible with nothing to mutate.
  Acting on the conclusion — promoting a canonical value into any shared/host record — stays a
  disabled future capability; this layer only computes and explains the choice.

Deliberately NOT a ranking input: the source's *kind* / *trust tier* (official vs commercial vs web).
The whole point of the story is to replace that rigid tier rule with an evidence-reliability model, so
provenance is carried for explanation but never ranks one value above another on tier alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models import FieldObservation


class Reliability(str, Enum):
    """How much the EVIDENCE behind a value can be relied on — the primary ranking dimension.

    This is evidence quality, NOT source tier. Highest first:

    - ``VERIFIED`` — the value was independently verified/confirmed (e.g. a channel confirmed by a
      reply/OTP, or a fact corroborated by independent evidence). The strongest class.
    - ``OBSERVED`` — the value was directly observed/fetched from a catalogued source (a registry
      record, a public listing). Reliable, but a stale observed value must not outrank a newer verified
      one.
    - ``INFERRED`` — the value was derived by the agent from other evidence; the weakest class.
    """

    VERIFIED = "verified"
    OBSERVED = "observed"
    INFERRED = "inferred"


# Reliability rank — higher wins. The gap is what makes a VERIFIED fact beat a stale OBSERVED
# (registry) one regardless of the registry's provenance tier.
_RELIABILITY_RANK: dict[Reliability, int] = {
    Reliability.VERIFIED: 3,
    Reliability.OBSERVED: 2,
    Reliability.INFERRED: 1,
}


# Denial reason codes — why a candidate is INELIGIBLE to be canonical (it stays in history regardless).
DENIAL_NO_RIGHTS_BASIS = "no_rights_basis"
DENIAL_RETENTION_LAPSED = "retention_lapsed"
DENIAL_SOURCE_RIGHTS_NOT_IN_FORCE = "source_rights_not_in_force"

# Winner reason codes — why the chosen value won over the alternatives (explainability).
REASON_ONLY_ELIGIBLE = "only_eligible_candidate"
REASON_HIGHER_RELIABILITY = "higher_reliability"
REASON_FRESHER = "fresher"
REASON_RIGHTS_IN_FORCE_TIEBREAK = "rights_in_force_tiebreak"
REASON_RECORDED_LATER_TIEBREAK = "recorded_later_tiebreak"
REASON_STABLE_ID_TIEBREAK = "stable_id_tiebreak"
# The load-bearing acceptance reason: the winner is a VERIFIED fact that is NEWER than an eligible
# OBSERVED (registry) value it beat — i.e. stale registry data did NOT blindly replace it.
REASON_VERIFIED_OVER_STALE_REGISTRY = "verified_over_stale_registry"


def _as_utc(moment: datetime) -> datetime:
    """Normalize to a tz-aware UTC datetime. SQLite returns naive datetimes, so a naive value is
    assumed to already be UTC (matching the store's convention) rather than compared tz-naively."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class CanonicalCandidate:
    """One field-value observation as the selection sees it — a value object, never tenant state.

    Build these from stored observations with :func:`candidates_from_observations` (which maps the
    DCA-061 provenance onto the reliability/rights dimensions this layer ranks on). Nothing here is a
    source *tier*: provenance (``source_key`` / ``source_version``) is carried for the explanation only.

    - ``observation_id`` — the id of the backing :class:`~app.db.models.FieldObservation` (the losing
      ones are preserved by id, never deleted).
    - ``value`` — the observed field value.
    - ``reliability`` — the evidence-quality class (the primary ranking dimension).
    - ``observed_at`` — when the value was observed/inferred at the source (freshness; decides WITHIN a
      reliability tier).
    - ``rights_basis`` — the recorded rights basis; an empty basis is a rights denial.
    - ``source_rights_in_force`` — whether the cited source terms are still in force (caller-derived);
      ``False`` is a rights denial. Defaults to ``True`` (decoupled from the source-approval wiring).
    - ``retention_until`` — the retention deadline; a value past its deadline at ``now`` is a rights
      denial. ``None`` means no stated limit.
    - ``recorded_at`` — when the observation row was recorded locally (a stable, late tiebreak).
    """

    observation_id: str
    value: str
    reliability: Reliability
    observed_at: datetime
    field_key: str = ""
    rights_basis: str = ""
    source_key: str = ""
    source_version: int | None = None
    source_rights_in_force: bool = True
    retention_until: datetime | None = None
    recorded_at: datetime | None = None


@dataclass(frozen=True)
class CandidateVerdict:
    """The per-candidate outcome: whether it was eligible to be canonical, and if not, why.

    ``denial_reason`` is ``""`` for an eligible candidate, otherwise one of the ``DENIAL_*`` codes.
    """

    candidate: CanonicalCandidate
    eligible: bool
    denial_reason: str = ""


@dataclass(frozen=True)
class CanonicalSelection:
    """The explainable canonical choice for ONE field_key over its observation history.

    - ``winner`` — the chosen canonical candidate, or ``None`` when no candidate is rights-eligible.
    - ``winner_reasons`` — stable reason codes explaining WHY the winner was chosen over the
      alternatives (empty when there is no winner).
    - ``verdicts`` — every candidate with its eligibility/denial, eligible ones first in ranked order
      (best first), then the denied ones — a full, reviewable picture.
    - ``preserved_observation_ids`` — every candidate's observation id EXCEPT the winner's: the losing
      values, which remain untouched in history (this layer deletes nothing).
    - ``mutated_state`` — always ``False``; a selection is a pure conclusion.
    """

    field_key: str
    winner: CanonicalCandidate | None
    winner_reasons: tuple[str, ...]
    verdicts: tuple[CandidateVerdict, ...]
    preserved_observation_ids: tuple[str, ...]
    mutated_state: bool = False

    @property
    def eligible(self) -> tuple[CanonicalCandidate, ...]:
        """The rights-eligible candidates, best-ranked first."""
        return tuple(v.candidate for v in self.verdicts if v.eligible)

    @property
    def denied(self) -> tuple[CandidateVerdict, ...]:
        """The rights-INELIGIBLE candidates and their denial reasons (preserved in history)."""
        return tuple(v for v in self.verdicts if not v.eligible)


def _evaluate_rights(candidate: CanonicalCandidate, now: datetime) -> str:
    """Return the DENIAL reason if the candidate's rights forbid relying on it, else ``""``.

    A value cannot be canonical when it has no recorded rights basis, its retention window has lapsed,
    or the cited source terms are no longer in force — regardless of how reliable or fresh it is. It
    still remains in history; it just cannot win.
    """
    if not candidate.rights_basis.strip():
        return DENIAL_NO_RIGHTS_BASIS
    if not candidate.source_rights_in_force:
        return DENIAL_SOURCE_RIGHTS_NOT_IN_FORCE
    if candidate.retention_until is not None and _as_utc(candidate.retention_until) < _as_utc(now):
        return DENIAL_RETENTION_LAPSED
    return ""


def _rank_key(candidate: CanonicalCandidate) -> tuple[int, float, int, float, str]:
    """A total, deterministic ordering key — sorted ASCENDING selects the best candidate first.

    Priority, strongest first: evidence reliability, then freshness (``observed_at``), then source
    rights in force, then the locally recorded time, then the observation id (a stable final tiebreak
    so the choice is fully reproducible). Numeric "higher is better" fields are negated so a plain
    ascending sort puts the winner first and the id tiebreak stays ascending (earliest-created-wins).
    """
    recorded = candidate.recorded_at
    recorded_ts = _as_utc(recorded).timestamp() if recorded is not None else float("-inf")
    return (
        -_RELIABILITY_RANK[candidate.reliability],
        -_as_utc(candidate.observed_at).timestamp(),
        -(1 if candidate.source_rights_in_force else 0),
        -recorded_ts,
        candidate.observation_id,
    )


def _winner_reasons(
    winner: CanonicalCandidate, others: list[CanonicalCandidate]
) -> tuple[str, ...]:
    """Explain why ``winner`` beat the other eligible candidates (``others``, already ranked)."""
    if not others:
        return (REASON_ONLY_ELIGIBLE,)

    reasons: list[str] = []
    runner = others[0]
    win_rank = _RELIABILITY_RANK[winner.reliability]
    run_rank = _RELIABILITY_RANK[runner.reliability]
    win_observed = _as_utc(winner.observed_at)
    if win_rank > run_rank:
        reasons.append(REASON_HIGHER_RELIABILITY)
    elif win_observed > _as_utc(runner.observed_at):
        reasons.append(REASON_FRESHER)
    elif winner.source_rights_in_force and not runner.source_rights_in_force:
        reasons.append(REASON_RIGHTS_IN_FORCE_TIEBREAK)
    elif _recorded_after(winner, runner):
        reasons.append(REASON_RECORDED_LATER_TIEBREAK)
    else:
        reasons.append(REASON_STABLE_ID_TIEBREAK)

    # The load-bearing acceptance guard: a newer VERIFIED fact chosen over a stale OBSERVED (registry)
    # value. Recorded whenever the winner is verified and it beat an eligible observed value that is
    # strictly older — i.e. stale registry data did not blindly replace it.
    if winner.reliability is Reliability.VERIFIED and any(
        o.reliability is Reliability.OBSERVED and _as_utc(o.observed_at) < win_observed
        for o in others
    ):
        reasons.append(REASON_VERIFIED_OVER_STALE_REGISTRY)
    return tuple(reasons)


def _recorded_after(a: CanonicalCandidate, b: CanonicalCandidate) -> bool:
    a_ts = _as_utc(a.recorded_at).timestamp() if a.recorded_at is not None else float("-inf")
    b_ts = _as_utc(b.recorded_at).timestamp() if b.recorded_at is not None else float("-inf")
    return a_ts > b_ts


def select_canonical(
    candidates: Iterable[CanonicalCandidate], *, now: datetime | None = None
) -> CanonicalSelection:
    """Choose the canonical value for ONE field_key from its observation history (pure, explainable).

    All candidates must concern the same ``field_key`` (a mix is a programming error and raises
    ``ValueError``). Each candidate is first screened by the rights gate (:func:`_evaluate_rights`);
    eligible candidates are ranked by :func:`_rank_key` and the top one wins, with
    :func:`_winner_reasons` recording why. If no candidate is rights-eligible there is no winner (every
    candidate is preserved in the verdicts, with its denial reason). Mutates nothing; deletes nothing.
    """
    moment = now or datetime.now(UTC)
    items = list(candidates)

    field_keys = {c.field_key for c in items if c.field_key}
    if len(field_keys) > 1:
        raise ValueError(
            "select_canonical expects candidates for a single field_key; got: "
            + ", ".join(sorted(field_keys))
        )
    field_key = next(iter(field_keys), "")

    eligible: list[CanonicalCandidate] = []
    denied: list[CandidateVerdict] = []
    for candidate in items:
        reason = _evaluate_rights(candidate, moment)
        if reason:
            denied.append(CandidateVerdict(candidate, eligible=False, denial_reason=reason))
        else:
            eligible.append(candidate)

    eligible.sort(key=_rank_key)

    winner: CanonicalCandidate | None = None
    winner_reasons: tuple[str, ...] = ()
    if eligible:
        winner = eligible[0]
        winner_reasons = _winner_reasons(winner, eligible[1:])

    verdicts = tuple(
        [CandidateVerdict(c, eligible=True) for c in eligible]
        # denied candidates ordered deterministically too, so the picture is reproducible.
        + sorted(denied, key=lambda v: _rank_key(v.candidate))
    )
    preserved = tuple(
        c.observation_id
        for c in items
        if winner is None or c.observation_id != winner.observation_id
    )
    return CanonicalSelection(
        field_key=field_key,
        winner=winner,
        winner_reasons=winner_reasons,
        verdicts=verdicts,
        preserved_observation_ids=preserved,
    )


def select_canonical_by_field(
    candidates: Iterable[CanonicalCandidate], *, now: datetime | None = None
) -> dict[str, CanonicalSelection]:
    """Group candidates by ``field_key`` and choose the canonical value for each (pure).

    A convenience over :func:`select_canonical` for a subject's whole observation set. Returns a dict
    keyed by field_key; each value is that field's explainable selection. Deletes/mutates nothing.
    """
    moment = now or datetime.now(UTC)
    by_field: dict[str, list[CanonicalCandidate]] = {}
    for candidate in candidates:
        by_field.setdefault(candidate.field_key, []).append(candidate)
    return {field_key: select_canonical(group, now=moment) for field_key, group in by_field.items()}


# -- read-only adapter over stored field observations -----------------------------------------------


def candidates_from_observations(
    observations: Iterable["FieldObservation"],
    *,
    verified_observation_ids: Iterable[str] = (),
    source_rights_in_force: Callable[[str, int | None], bool] | None = None,
) -> list[CanonicalCandidate]:
    """Build :class:`CanonicalCandidate` bundles from stored DCA-061 field observations (read-only).

    The caller supplies the observations already read within its tenant scope (e.g.
    ``FieldObservationStore.for_subject(...)``), so this never widens scope and reads only what it is
    handed. It maps each observation's provenance onto the reliability/rights dimensions:

    - ``reliability`` — an observation whose id is in ``verified_observation_ids`` is ``VERIFIED``;
      otherwise an ``observed`` field is ``OBSERVED`` and an ``inferred`` field is ``INFERRED``.
      Verification is caller-derived (e.g. a confirmed channel / corroboration) so this layer takes no
      dependency on a later, disabled verification capability.
    - ``source_rights_in_force`` — ``source_rights_in_force(source_key, source_version)`` when a
      predicate is given (so a caller can mark expired/revoked source terms as no longer in force),
      else ``True`` (decoupled from the source-approval wiring by default).

    Builds value objects only — it stages, mutates and deletes nothing.
    """
    verified = set(verified_observation_ids)
    in_force = source_rights_in_force or (lambda _key, _ver: True)
    out: list[CanonicalCandidate] = []
    for obs in observations:
        if obs.id in verified:
            reliability = Reliability.VERIFIED
        elif obs.inference_label == Reliability.OBSERVED.value:
            reliability = Reliability.OBSERVED
        else:
            reliability = Reliability.INFERRED
        out.append(
            CanonicalCandidate(
                observation_id=obs.id,
                value=obs.field_value,
                reliability=reliability,
                observed_at=obs.observed_at,
                field_key=obs.field_key,
                rights_basis=obs.rights_basis,
                source_key=obs.source_key,
                source_version=obs.source_version,
                source_rights_in_force=bool(in_force(obs.source_key, obs.source_version)),
                retention_until=obs.retention_until,
                recorded_at=obs.created_at,
            )
        )
    return out
