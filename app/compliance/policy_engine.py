"""DCA-055 — evaluate ONE deterministic policy request.

A pure, deterministic Python evaluator that answers a single policy question — *may this
acquisition/contact action proceed?* — against the in-force India compliance policy rulepack
(DCA-052 schema, promoted to ``active`` only by a DCA-053 approval). It returns one of four
decisions with the evidence, stable reason codes and the rulepack/approval versions that produced it.

This is the capability DCA-052/053 kept describing as "the policy ENGINE that would CONSULT an
in-force pack" — this story builds *only that one read-only evaluation function* so it can be reviewed
and accepted independently. It is deliberately **not wired** into any send, acquisition, worker or HTTP
path: nothing calls it except its own tests and the manual-check scenario. Expressing a decision
enforces nothing and enables nothing; future capabilities stay disabled.

The load-bearing guarantees (the DCA-055 acceptance criteria):

- **A missing pack or an unresolved/unsupported jurisdiction can NEVER yield ALLOW.** Personal-data
  processing needs an explicit, in-force permission; absent one the evaluator fails closed to ``DENY``
  (``no personal-data permission``). The India-only service supports exactly one jurisdiction (``IN``);
  anything else is unresolved/unsupported and denied.
- **The LLM is never consulted.** Evaluation is pure Python over the rulepack data — no AI client is
  imported or called, the result is reproducible, and evaluating changes no state.
- **Default unknown/unsupported scope to no personal-data permission.** An action/channel outside the
  controlled vocabulary, or an in-force rule that does not clearly permit the request, defaults to
  ``DENY`` (or ``REVIEW`` when a governing rule plausibly applies but a required fact is missing) —
  never a silent allow.

Decision semantics:

- ``NOT_APPLICABLE`` — the request does not involve personal data, so the personal-data policy regime
  does not govern it (there is no personal-data permission to grant or withhold). This is decided
  before anything else; it is the only safe non-deny for a request the policy does not reach.
- ``DENY`` — a prohibition applies (an in-force ``deny`` rule), OR the fail-closed default: no in-force
  pack, an unresolved/unsupported jurisdiction, an unknown action/channel, or no rule grants the
  personal-data permission the request needs.
- ``REVIEW`` — a governing rule plausibly applies but a required condition fact was not supplied, so
  the engine can neither confirm the permission nor a specific prohibition; it defers to a human rather
  than guess (and crucially never upgrades the uncertainty to ALLOW).
- ``ALLOW`` — an in-force rule explicitly permits the request and NO in-force rule forbids it. A
  prohibition from any authority dominates (law is the ceiling; provider/product defaults only ever
  narrow it — they can never widen past a deny), so a single applicable ``deny`` blocks the action.

Precedence (``law > provider > product``) is reported as the *decisive authority* and orders the
evidence; it never turns a deny into an allow, because the resolution is deny-dominant (strictest wins)
— the safe direction for personal data.

DCA-056 — select rules by effective date + immutable evaluation-time snapshot
----------------------------------------------------------------------------
Rule selection is **obligation-specific and effective-date-driven**: each rule carries its OWN explicit
validity window (``effective_from``/``expires_at``, from DCA-052), which is where a DPDP/TRAI
obligation's *per-provision* commencement date lands (DCA-054 §1). A boundary timestamp selects the
correct rule — a rule whose obligation has NOT yet commenced (``now < effective_from``) or has lapsed
(``now >= expires_at``) is simply not in the selected set at that instant. There is deliberately **no
universal ``dpdp_enabled`` flag**: applicability is resolved one obligation at a time from its own
window (:func:`obligation_key` / :func:`select_effective_rules`), so staged tranches switch on
independently and the exact same request flips from DENY to ALLOW only once its governing obligation's
effective date is reached.

Every pack-backed evaluation also carries an **immutable evaluation-time rule snapshot**
(:class:`EvaluationSnapshot`): a frozen, content-fingerprinted capture of the evaluation moment, the
rulepack version / approval, and the exact rules that were in force and governed the request at that
instant (each with its obligation key and window). Because it is frozen (no field can be reassigned;
rule content is a canonical JSON string, the rule list a tuple), it never drifts after capture, and
:func:`replay` reproduces the identical decision from the snapshot ALONE — even if the live rulepack has
since changed — with no DB access and no state change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy.orm import Session

from app.compliance.policy_rulepacks import INDIA_JURISDICTION, CompliancePolicyRulepackStore
from app.db.models import (
    PolicyRulepackStatus,
    RuleAction,
    RuleAuthority,
    RuleChannel,
    RuleEffect,
)
from app.services.policy_approval import PolicyApprovalStore

# The evaluator's own version, surfaced on every decision so a result can be reproduced against the
# exact engine logic that produced it (alongside the rulepack version + approval id).
POLICY_ENGINE_VERSION = 1

# The India-only service supports exactly one jurisdiction. Anything else is unresolved/unsupported
# and must fail closed — it can never yield a personal-data permission.
SUPPORTED_JURISDICTIONS: frozenset[str] = frozenset({INDIA_JURISDICTION})

# Controlled vocabularies (as value sets) — a request naming a scope outside these is unsupported.
_ACTIONS: frozenset[str] = frozenset(a.value for a in RuleAction)
_CHANNELS: frozenset[str] = frozenset(c.value for c in RuleChannel)

# Authority precedence: a higher number outranks (law is the ceiling). Used only to pick the decisive
# authority for reporting/evidence ordering — never to override the deny-dominant resolution.
_AUTHORITY_RANK: dict[str, int] = {
    RuleAuthority.LAW.value: 3,
    RuleAuthority.PROVIDER.value: 2,
    RuleAuthority.PRODUCT.value: 1,
}


class PolicyDecision(str, Enum):
    """The four outcomes a single policy request can resolve to (DCA-055)."""

    ALLOW = "allow"
    DENY = "deny"
    REVIEW = "review"
    NOT_APPLICABLE = "not_applicable"


# Stable, test-asserted reason codes (one per distinct path to a decision).
REASON_NOT_PERSONAL_DATA = "not_personal_data"  # NOT_APPLICABLE: policy regime does not reach it
REASON_UNRESOLVED_JURISDICTION = "unresolved_jurisdiction"  # DENY: empty/ambiguous country
REASON_UNSUPPORTED_JURISDICTION = "unsupported_jurisdiction"  # DENY: a resolved but non-IN country
REASON_UNSUPPORTED_ACTION = "unsupported_action"  # DENY: action outside the vocabulary
REASON_UNSUPPORTED_CHANNEL = "unsupported_channel"  # DENY: channel outside the vocabulary
REASON_NO_IN_FORCE_PACK = "no_in_force_pack"  # DENY: no approved+active+in-window pack
REASON_NO_MATCHING_RULE = "no_matching_rule"  # DENY: nothing grants the permission (default-deny)
REASON_DENIED_BY_RULE = "denied_by_rule"  # DENY: an in-force rule forbids it
REASON_ALLOWED_BY_RULE = "allowed_by_rule"  # ALLOW: an in-force rule permits it, none forbids it
REASON_INSUFFICIENT_FACTS = "insufficient_facts"  # REVIEW: a governing rule needs a missing fact


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a (possibly naive — SQLite) datetime to tz-aware UTC for comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse_dt(value: object) -> datetime | None:
    """A stored rule window bound is an ISO string (or ``None``); parse it to tz-aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    return _as_utc(datetime.fromisoformat(str(value)))


def _iso_or_none(value: object) -> str | None:
    """Render a stored window bound as a canonical ISO-UTC string (or ``None``) for the snapshot."""
    parsed = _parse_dt(value)
    return parsed.isoformat() if parsed is not None else None


def _canonical_json(value: object) -> str:
    """Deterministic JSON (sorted keys, no whitespace) — used for stable snapshot fingerprints and to
    store a rule's conditions as an immutable string the snapshot can round-trip in :func:`replay`."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def obligation_key(rule: dict) -> str:
    """Stable identity of the OBLIGATION a rule implements — its cited provision (evidence reference),
    falling back to its scope when no reference is present.

    An obligation is what carries an effective date (DCA-054 §1: store a commencement date *per
    obligation*, e.g. a specific DPDP provision, never a single ``dpdp_enabled`` flag). Keying selection
    by this makes "which rule is in force now" a per-obligation question resolved from that obligation's
    own window — so staged tranches commence independently.
    """
    reference = str((rule.get("evidence") or {}).get("reference", "")).strip()
    if reference:
        return reference
    action = (rule.get("action") or "").strip().lower()
    channel = (rule.get("channel") or RuleChannel.ANY.value).strip().lower()
    authority = (rule.get("authority") or "").strip().lower()
    return f"{authority}:{action}:{channel}"


def select_effective_rules(rules: Iterable[dict], *, now: datetime) -> list[dict]:
    """Select the rules in force at ``now`` purely by each rule's OWN effective window.

    This is the obligation-specific effective-date selection: a rule whose obligation has not yet
    commenced (``now < effective_from``) or has already lapsed (``now >= expires_at``) is excluded; an
    unset bound is unbounded on that side. There is no universal flag — each rule is selected (or not)
    independently from its own window, so a boundary timestamp selects exactly the obligations that have
    commenced and not lapsed at that instant.
    """
    return [rule for rule in rules if _rule_time_eligible(rule, now)]


@dataclass(frozen=True)
class PolicyRequest:
    """One deterministic policy question.

    ``action``/``channel`` name the acquisition/contact scope (controlled vocabularies — an unknown
    value is unsupported and denied). ``jurisdiction`` is the ALREADY-RESOLVED recipient jurisdiction
    (``""`` means it could not be resolved — e.g. an ambiguous country — and fails closed); only
    ``IN`` is supported. ``involves_personal_data`` defaults to ``True`` (the conservative assumption);
    when ``False`` the request is ``NOT_APPLICABLE``. ``facts`` are the asserted context values a
    rule's conditions are checked against (e.g. ``{"consent": "present", "trust_tier":
    "official_registry"}``); a missing fact that a governing rule needs yields ``REVIEW``, never a
    guess.
    """

    action: str
    channel: str = RuleChannel.ANY.value
    jurisdiction: str = INDIA_JURISDICTION
    involves_personal_data: bool = True
    facts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InForcePack:
    """The one in-force policy rulepack the evaluator consults (resolved from the DCA-052/053 stores).

    ``rules`` is the stored, normalized rule list (each a dict exactly as DCA-052 persisted it).
    ``approval_id`` ties the pack to the DCA-053 approval that put it in force. A ``None`` from the
    resolver means there is NO in-force pack — which the evaluator treats as the fail-closed default.
    """

    jurisdiction: str
    version: int
    approval_id: str | None
    rules: list[dict]
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuleSnapshot:
    """One rule exactly as it stood at evaluation time — an immutable record (DCA-056).

    Captures the rule's obligation identity and its EXPLICIT effective window (so a reader can see *why*
    a boundary timestamp did or did not select it), plus the effect/scope/authority/conditions that drove
    the decision. ``conditions_json`` is a canonical JSON string (not a mutable dict) so the whole
    snapshot is genuinely immutable and :func:`replay` can round-trip it. ``time_eligible`` records
    whether the rule's own window covered the evaluation moment.
    """

    obligation: str
    effect: str
    action: str
    channel: str
    authority: str
    conditions_json: str
    reference: str
    effective_from: str | None
    expires_at: str | None
    time_eligible: bool


@dataclass(frozen=True)
class EvaluationSnapshot:
    """An immutable, content-fingerprinted capture of a single evaluation (DCA-056).

    Pins the evaluation moment, the in-force rulepack version / approval, the engine version, and the
    exact rules that were IN FORCE and governed the request at that instant (:class:`RuleSnapshot`). It
    is frozen (no attribute can be reassigned) and holds only immutable members (a tuple of frozen
    rule snapshots, strings), so it can never drift after capture. ``fingerprint`` is a sha256 over the
    canonical content, so two identical evaluations produce the same fingerprint and a later change to
    the live pack cannot silently alter a recorded decision. :func:`replay` reproduces the identical
    decision from this snapshot alone — no DB, no live pack, no state change.
    """

    evaluated_at: str
    jurisdiction: str
    rulepack_version: int | None
    approval_id: str | None
    engine_version: int
    selected_rules: tuple[RuleSnapshot, ...]
    fingerprint: str


@dataclass(frozen=True)
class PolicyEvaluation:
    """The result of evaluating one policy request — a reproducible, auditable record.

    ``decision`` is one of the four :class:`PolicyDecision` outcomes. ``reasons`` are stable codes
    explaining it. ``evidence`` lists the rule(s)/pack/approval that drove it (never fabricated — only
    what was actually consulted). ``rulepack_version``/``approval_id`` pin the exact in-force content;
    ``engine_version`` pins the logic. ``decisive_authority`` names which authority's rule decided (for
    an allow/deny). ``llm_consulted`` is ALWAYS ``False`` — the invariant that no model was asked.
    """

    decision: PolicyDecision
    jurisdiction: str
    action: str
    channel: str
    reasons: tuple[str, ...]
    evidence: tuple[dict, ...] = ()
    rulepack_version: int | None = None
    approval_id: str | None = None
    decisive_authority: str | None = None
    engine_version: int = POLICY_ENGINE_VERSION
    llm_consulted: bool = False
    snapshot: EvaluationSnapshot | None = None

    @property
    def allowed(self) -> bool:
        """True only for an explicit ALLOW — the only decision that grants the permission."""
        return self.decision is PolicyDecision.ALLOW


# --------------------------------------------------------------------------------------------------
# Condition evaluation — a small, deterministic, fully-reproducible predicate engine. A rule's
# ``conditions`` are PRECONDITIONS that must hold in the request's ``facts`` for the rule to apply.
# Each condition resolves to MET / UNMET / INDETERMINATE (the fact is absent) — never to a guess.
# --------------------------------------------------------------------------------------------------

_MET = "met"
_UNMET = "unmet"
_INDETERMINATE = "indeterminate"

_CONSENT_PRESENT = {"present", "granted", "obtained", "verified", "yes", "true"}
_CONSENT_ABSENT = {"absent", "none", "missing", "denied", "no", "false"}


def _truthy(value: object) -> bool | None:
    """Interpret a fact as a boolean; ``None`` means the fact is absent/indeterminate."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"true", "yes", "present", "1"}:
        return True
    if token in {"false", "no", "absent", "0", ""}:
        return False
    return None


def _check_consent(required: str, facts: dict) -> str:
    if required == "not_required":
        return _MET  # no consent needed — the condition is satisfied unconditionally
    fact_key = "prior_relationship" if required == "prior_relationship" else "consent"
    raw = facts.get(fact_key)
    if required == "prior_relationship":
        flag = _truthy(raw)
        return _INDETERMINATE if flag is None else (_MET if flag else _UNMET)
    # required == "required": consent must be affirmatively present in the facts.
    if raw is None:
        return _INDETERMINATE
    token = str(raw).strip().lower()
    if token in _CONSENT_PRESENT:
        return _MET
    if token in _CONSENT_ABSENT:
        return _UNMET
    return _INDETERMINATE


def _check_bool(required: bool, fact_key: str, facts: dict) -> str:
    if not required:
        return _MET  # the rule does not require this flag
    flag = _truthy(facts.get(fact_key))
    return _INDETERMINATE if flag is None else (_MET if flag else _UNMET)


def _check_membership(allowed: Sequence[str], fact_key: str, facts: dict) -> str:
    raw = facts.get(fact_key)
    if raw is None:
        return _INDETERMINATE
    return _MET if str(raw).strip() in set(allowed) else _UNMET


def _check_time_window(window: dict, facts: dict) -> str:
    raw = facts.get("hour")
    if raw is None:
        return _INDETERMINATE
    try:
        hour = int(raw)
    except (TypeError, ValueError):
        return _UNMET
    return _MET if window["start_hour"] <= hour <= window["end_hour"] else _UNMET


def _check_max_frequency(limit: int, facts: dict) -> str:
    raw = facts.get("frequency_today")
    if raw is None:
        return _INDETERMINATE
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return _UNMET
    return _MET if count <= limit else _UNMET


def _evaluate_conditions(conditions: dict, facts: dict) -> str:
    """Resolve a rule's whole condition set to MET / UNMET / INDETERMINATE.

    Any explicitly contradicted condition makes the rule UNMET (it governs a different scenario, so it
    is excluded). Otherwise, any condition whose fact is absent makes the rule INDETERMINATE (it might
    govern — defer to review). Only a rule with every condition satisfied is MET.
    """
    overall = _MET
    for key, value in (conditions or {}).items():
        if key == "consent":
            status = _check_consent(str(value), facts)
        elif key == "registration_required":
            status = _check_bool(bool(value), "registration", facts)
        elif key == "trust_tier":
            status = _check_membership(value, "trust_tier", facts)
        elif key == "contact_kind":
            status = _check_membership(value, "contact_kind", facts)
        elif key == "time_window":
            status = _check_time_window(value, facts)
        elif key == "max_frequency_per_day":
            status = _check_max_frequency(int(value), facts)
        else:
            # An unrecognized condition key cannot be deterministically confirmed — fail safe to
            # review rather than silently ignore it (DCA-052 only stores known keys, so this is
            # defence-in-depth against future/unknown schema).
            status = _INDETERMINATE
        if status == _UNMET:
            return _UNMET
        if status == _INDETERMINATE:
            overall = _INDETERMINATE
    return overall


# --------------------------------------------------------------------------------------------------
# The pure decision function + a thin DB-backed resolver.
# --------------------------------------------------------------------------------------------------


def _rule_time_eligible(rule: dict, now: datetime) -> bool:
    """Is a rule inside its OWN explicit validity window at ``now``? An unset bound is unbounded."""
    effective_from = _parse_dt(rule.get("effective_from"))
    expires_at = _parse_dt(rule.get("expires_at"))
    if effective_from is not None and now < effective_from:
        return False
    if expires_at is not None and now >= expires_at:
        return False
    return True


def _rule_evidence(rule: dict) -> dict:
    return {
        "source": "rule",
        "obligation": obligation_key(rule),
        "effect": rule.get("effect"),
        "action": rule.get("action"),
        "channel": rule.get("channel"),
        "authority": rule.get("authority"),
        "conditions": rule.get("conditions", {}),
        "effective_from": rule.get("effective_from"),
        "expires_at": rule.get("expires_at"),
        "evidence": rule.get("evidence", {}),
    }


def _rule_snapshot(rule: dict, *, now: datetime) -> RuleSnapshot:
    """Freeze one rule as it stood at ``now`` — immutable, with its obligation key and window."""
    return RuleSnapshot(
        obligation=obligation_key(rule),
        effect=(rule.get("effect") or "").strip().lower(),
        action=(rule.get("action") or "").strip().lower(),
        channel=(rule.get("channel") or RuleChannel.ANY.value).strip().lower(),
        authority=(rule.get("authority") or "").strip().lower(),
        conditions_json=_canonical_json(rule.get("conditions", {}) or {}),
        reference=str((rule.get("evidence") or {}).get("reference", "")).strip(),
        effective_from=_iso_or_none(rule.get("effective_from")),
        expires_at=_iso_or_none(rule.get("expires_at")),
        time_eligible=_rule_time_eligible(rule, now),
    )


def _build_snapshot(
    *,
    moment: datetime,
    jurisdiction: str,
    pack: InForcePack,
    selected: Sequence[dict],
) -> EvaluationSnapshot:
    """Build the immutable evaluation-time snapshot from the rules that actually governed the request.

    ``selected`` are the in-force, governing rules (time-eligible, conditions not contradicted) that
    drove the decision — the ones a reproduction needs. The fingerprint is a sha256 over the canonical
    content (moment + versions + each selected rule), so an identical evaluation yields an identical
    fingerprint and a later change to the live pack cannot silently rewrite a recorded decision.
    """
    rule_snaps = tuple(_rule_snapshot(rule, now=moment) for rule in selected)
    content = _canonical_json(
        {
            "evaluated_at": moment.isoformat(),
            "jurisdiction": jurisdiction,
            "rulepack_version": pack.version,
            "approval_id": pack.approval_id,
            "engine_version": POLICY_ENGINE_VERSION,
            "selected_rules": [
                {
                    "obligation": s.obligation,
                    "effect": s.effect,
                    "action": s.action,
                    "channel": s.channel,
                    "authority": s.authority,
                    "conditions_json": s.conditions_json,
                    "reference": s.reference,
                    "effective_from": s.effective_from,
                    "expires_at": s.expires_at,
                    "time_eligible": s.time_eligible,
                }
                for s in rule_snaps
            ],
        }
    )
    fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return EvaluationSnapshot(
        evaluated_at=moment.isoformat(),
        jurisdiction=jurisdiction,
        rulepack_version=pack.version,
        approval_id=pack.approval_id,
        engine_version=POLICY_ENGINE_VERSION,
        selected_rules=rule_snaps,
        fingerprint=fingerprint,
    )


def _rule_from_snapshot(snap: RuleSnapshot) -> dict:
    """Rebuild a rule dict (DCA-052 shape) from a frozen :class:`RuleSnapshot`, for :func:`replay`."""
    return {
        "effect": snap.effect,
        "action": snap.action,
        "channel": snap.channel,
        "authority": snap.authority,
        "conditions": json.loads(snap.conditions_json),
        "evidence": {"reference": snap.reference},
        "effective_from": snap.effective_from,
        "expires_at": snap.expires_at,
    }


def replay(request: PolicyRequest, snapshot: EvaluationSnapshot) -> PolicyEvaluation:
    """Reproduce a decision from its immutable snapshot ALONE — no DB, no live pack, no state change.

    Rebuilds the exact rules that were in force at evaluation time and re-decides at the SAME recorded
    moment, so it yields the identical decision even if the live rulepack has since changed, been
    superseded, or expired. This is the DCA-056 reproducibility guarantee: a recorded snapshot is a
    complete, self-contained, re-runnable record of *why* an action was allowed or denied at that instant.
    """
    moment = _parse_dt(snapshot.evaluated_at)
    pack = InForcePack(
        jurisdiction=snapshot.jurisdiction,
        version=snapshot.rulepack_version if snapshot.rulepack_version is not None else 0,
        approval_id=snapshot.approval_id,
        rules=[_rule_from_snapshot(s) for s in snapshot.selected_rules],
    )
    return decide(request, pack, now=moment)


def _fail_closed(
    request: PolicyRequest,
    jurisdiction: str,
    reason: str,
    *,
    evidence: Iterable[dict] = (),
    snapshot: EvaluationSnapshot | None = None,
) -> PolicyEvaluation:
    """Build a DENY with no permission granted — the fail-closed default for personal data."""
    return PolicyEvaluation(
        decision=PolicyDecision.DENY,
        jurisdiction=jurisdiction,
        action=request.action,
        channel=request.channel,
        reasons=(reason,),
        evidence=tuple(evidence),
        snapshot=snapshot,
    )


def decide(
    request: PolicyRequest, pack: InForcePack | None, *, now: datetime | None = None
) -> PolicyEvaluation:
    """Evaluate ONE policy request against an already-resolved in-force pack — pure, no I/O, no AI.

    ``pack`` is ``None`` when there is no in-force rulepack (unapproved/expired/absent); the evaluator
    then fails closed to DENY. This function performs no database or network access and never consults
    a model, so a given ``(request, pack, now)`` always produces the identical result.
    """
    moment = now or datetime.now(UTC)
    action = (request.action or "").strip().lower()
    channel = (request.channel or RuleChannel.ANY.value).strip().lower()
    jurisdiction = (request.jurisdiction or "").strip().upper()

    # 0) Non-personal-data requests are outside the policy regime entirely — NOT_APPLICABLE. Decided
    #    first: there is no personal-data permission to grant, so this is the only safe non-deny.
    if not request.involves_personal_data:
        return PolicyEvaluation(
            decision=PolicyDecision.NOT_APPLICABLE,
            jurisdiction=jurisdiction,
            action=request.action,
            channel=request.channel,
            reasons=(REASON_NOT_PERSONAL_DATA,),
        )

    # 1) Jurisdiction must resolve to the single supported country. Empty => unresolved (e.g. an
    #    ambiguous country); a resolved-but-other country => unsupported. Both fail closed.
    if not jurisdiction:
        return _fail_closed(request, jurisdiction, REASON_UNRESOLVED_JURISDICTION)
    if jurisdiction not in SUPPORTED_JURISDICTIONS:
        return _fail_closed(request, jurisdiction, REASON_UNSUPPORTED_JURISDICTION)

    # 2) The scope vocabulary must be recognized; an unknown action/channel is unsupported => DENY.
    if action not in _ACTIONS:
        return _fail_closed(request, jurisdiction, REASON_UNSUPPORTED_ACTION)
    if channel not in _CHANNELS:
        return _fail_closed(request, jurisdiction, REASON_UNSUPPORTED_CHANNEL)

    # 3) No in-force pack => no permission exists => fail closed (a missing pack can NEVER allow).
    if pack is None:
        return _fail_closed(request, jurisdiction, REASON_NO_IN_FORCE_PACK)

    pack_evidence = {"source": "rulepack", "version": pack.version, "evidence": pack.evidence}
    approval_evidence = {"source": "approval", "approval_id": pack.approval_id}

    # 4) Collect the rules that GOVERN this request: same action, matching channel (or channel-any),
    #    time-eligible, and whose conditions are not explicitly contradicted by the facts.
    governing_allow: list[dict] = []
    governing_deny: list[dict] = []
    indeterminate: list[dict] = []
    for rule in pack.rules:
        if (rule.get("action") or "").lower() != action:
            continue
        rule_channel = (rule.get("channel") or RuleChannel.ANY.value).lower()
        if rule_channel != channel and rule_channel != RuleChannel.ANY.value:
            continue
        if not _rule_time_eligible(rule, moment):
            continue
        status = _evaluate_conditions(rule.get("conditions", {}), request.facts)
        if status == _UNMET:
            continue  # the rule governs a different scenario — exclude it
        if status == _INDETERMINATE:
            indeterminate.append(rule)
            continue
        if (rule.get("effect") or "").lower() == RuleEffect.DENY.value:
            governing_deny.append(rule)
        else:
            governing_allow.append(rule)

    # 4b) Freeze the rules that actually governed this request at ``moment`` into an immutable, content-
    #     fingerprinted snapshot (DCA-056). Built from the SELECTED set (time-eligible, conditions not
    #     contradicted) so a reproduction needs nothing but the snapshot; see :func:`replay`.
    selected = governing_deny + governing_allow + indeterminate
    snapshot = _build_snapshot(
        moment=moment, jurisdiction=jurisdiction, pack=pack, selected=selected
    )

    # 5) Deny-dominant resolution (strictest wins — the safe direction; a lower authority may only
    #    narrow a higher one, never widen past it). A single applicable DENY blocks the action.
    if governing_deny:
        decisive = max(governing_deny, key=lambda r: _AUTHORITY_RANK.get(r.get("authority", ""), 0))
        evidence = [_rule_evidence(r) for r in governing_deny] + [pack_evidence, approval_evidence]
        return PolicyEvaluation(
            decision=PolicyDecision.DENY,
            jurisdiction=jurisdiction,
            action=request.action,
            channel=request.channel,
            reasons=(REASON_DENIED_BY_RULE,),
            evidence=tuple(evidence),
            rulepack_version=pack.version,
            approval_id=pack.approval_id,
            decisive_authority=decisive.get("authority"),
            snapshot=snapshot,
        )

    if governing_allow:
        decisive = max(
            governing_allow, key=lambda r: _AUTHORITY_RANK.get(r.get("authority", ""), 0)
        )
        evidence = [_rule_evidence(r) for r in governing_allow] + [pack_evidence, approval_evidence]
        return PolicyEvaluation(
            decision=PolicyDecision.ALLOW,
            jurisdiction=jurisdiction,
            action=request.action,
            channel=request.channel,
            reasons=(REASON_ALLOWED_BY_RULE,),
            evidence=tuple(evidence),
            rulepack_version=pack.version,
            approval_id=pack.approval_id,
            decisive_authority=decisive.get("authority"),
            snapshot=snapshot,
        )

    # 6) No confidently-applicable rule. If a rule plausibly governs but a required fact was missing,
    #    defer to a human (REVIEW) — never upgrade uncertainty to ALLOW. Otherwise no rule grants the
    #    permission the request needs, so personal-data processing fails closed to DENY (default-deny).
    if indeterminate:
        evidence = [_rule_evidence(r) for r in indeterminate] + [pack_evidence, approval_evidence]
        return PolicyEvaluation(
            decision=PolicyDecision.REVIEW,
            jurisdiction=jurisdiction,
            action=request.action,
            channel=request.channel,
            reasons=(REASON_INSUFFICIENT_FACTS,),
            evidence=tuple(evidence),
            rulepack_version=pack.version,
            approval_id=pack.approval_id,
            snapshot=snapshot,
        )

    return _fail_closed(
        request,
        jurisdiction,
        REASON_NO_MATCHING_RULE,
        evidence=[pack_evidence, approval_evidence],
        snapshot=snapshot,
    )


class PolicyEvaluator:
    """DB-backed orchestrator: resolve the in-force pack (DCA-052/053) then :func:`decide` over it.

    Read-only. It opens no write, consults no model and is wired into no pipeline — it exists for
    review/manual-check only. Construct with a session; it never commits.
    """

    def __init__(self, session: Session) -> None:
        self._rulepacks = CompliancePolicyRulepackStore(session)
        self._approvals = PolicyApprovalStore(session)

    def resolve_in_force_pack(
        self, jurisdiction: str = INDIA_JURISDICTION, *, now: datetime | None = None
    ) -> InForcePack | None:
        """The highest-version pack that is ACTIVE and inside its approval window at ``now`` — or None.

        Mirrors the DCA-053 lifecycle: a version is in force only when approved, ACTIVE and within its
        approval's explicit window. Picking the highest such version makes a newer approved policy
        supersede an older one. Returns ``None`` when nothing is in force (the fail-closed default).
        """
        moment = now or datetime.now(UTC)
        juris = (jurisdiction or "").strip().upper()
        for pack in sorted(self._rulepacks.versions(juris), key=lambda p: p.version, reverse=True):
            if pack.status != PolicyRulepackStatus.ACTIVE.value:
                continue
            decision = self._approvals.evaluate(pack.version, now=moment, jurisdiction=juris)
            if decision.in_force:
                return InForcePack(
                    jurisdiction=juris,
                    version=pack.version,
                    approval_id=decision.approval_id,
                    rules=list(pack.rules or []),
                    evidence=dict(pack.evidence or {}),
                )
        return None

    def evaluate(self, request: PolicyRequest, *, now: datetime | None = None) -> PolicyEvaluation:
        """Resolve the in-force pack for the request's jurisdiction, then decide — never calling AI."""
        moment = now or datetime.now(UTC)
        juris = (request.jurisdiction or "").strip().upper()
        # Only attempt resolution for a supported jurisdiction; an unresolved/unsupported one has no
        # pack to look up and :func:`decide` fails it closed.
        pack = (
            self.resolve_in_force_pack(juris, now=moment)
            if juris in SUPPORTED_JURISDICTIONS
            else None
        )
        return decide(request, pack, now=moment)
