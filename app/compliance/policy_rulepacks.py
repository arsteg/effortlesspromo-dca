"""DCA-052 — versioned India compliance policy rulepack schemas (validation + the one writer).

A *policy* rulepack is the deterministic, auditable data structure a FUTURE (disabled) policy engine
would consult to decide whether an acquisition/contact **action** over a **channel** is permitted in a
jurisdiction, under explicit **conditions**, backed by **evidence**, within an **effective-date**
window, and labelled by its **authority** — the distinction between a *law*, a *provider* and a
*product* default. This is distinct from the lightweight :mod:`app.compliance.rulepacks`
acquisition-screening pack: that one answers "may this candidate be acquired at all"; this one is the
richer, versioned, reviewable policy document.

This story DEFINES and VALIDATES the schema. It enforces nothing and activates nothing.

:class:`CompliancePolicyRulepackStore` is the single sanctioned writer of
:class:`~app.db.models.CompliancePolicyRulepack`. The load-bearing guarantees (the DCA-052 acceptance
criteria):

- **Malformed or contradictory rules cannot be saved.** Validation runs FIRST over the whole pack, so
  a refusal raises :class:`PolicyRulepackValidationError` with a stable code and leaves no row and no
  partial state: a non-India jurisdiction, an empty pack, an unknown action/channel/effect/authority,
  a rule missing evidence, a malformed condition, an incoherent validity window (pack or rule level),
  or two rules that CONTRADICT each other each have a distinct code.
- **A contradiction is caught, not stored.** Two rules that scope the identical
  ``(action, channel, authority, conditions)`` with OPPOSITE effects over an OVERLAPPING validity
  window cannot both be true, so the pack is refused. Opposite effects across DIFFERENT authorities
  are NOT a contradiction — that is precedence (LAW > PROVIDER > PRODUCT), resolved by a reader.
- **No rule becomes live just by existing.** Every stored pack is ``status='draft'``
  (:meth:`CompliancePolicyRulepackStore.is_active` is always ``False`` here). Promotion to ``ACTIVE``
  — and the policy-engine capability it would pair with — stay DISABLED future stories.

Like the source catalog/terms stores, this is platform CONTROL-PLANE data (a shared policy registry,
not tenant-scoped), so the store is constructed with just the session — no business scope, no tenant
audit entry. It stages into the caller's session and never commits; the caller owns the unit of work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    CompliancePolicyRulepack,
    PolicyRulepackStatus,
    RuleAction,
    RuleAuthority,
    RuleChannel,
    RuleEffect,
)

# The service is India-only: a policy rulepack can only be for India.
INDIA_JURISDICTION = "IN"

# Controlled vocabularies (the enum values), surfaced for error messages and callers.
ACTIONS: tuple[str, ...] = tuple(a.value for a in RuleAction)
CHANNELS: tuple[str, ...] = tuple(c.value for c in RuleChannel)
EFFECTS: tuple[str, ...] = tuple(e.value for e in RuleEffect)
AUTHORITIES: tuple[str, ...] = tuple(a.value for a in RuleAuthority)

# Stable validation codes (each raised BEFORE the session is mutated, so a refusal stages nothing).
REASON_INVALID_JURISDICTION = "invalid_jurisdiction"
REASON_NO_RULES = "no_rules"
REASON_UNKNOWN_ACTION = "unknown_action"
REASON_UNKNOWN_CHANNEL = "unknown_channel"
REASON_UNKNOWN_EFFECT = "unknown_effect"
REASON_UNKNOWN_AUTHORITY = "unknown_authority"
REASON_MISSING_RULE_EVIDENCE = "missing_rule_evidence"
REASON_INVALID_CONDITION = "invalid_condition"
REASON_INVALID_WINDOW = "invalid_window"
REASON_CONTRADICTORY_RULES = "contradictory_rules"


class PolicyRulepackValidationError(Exception):
    """A rulepack was refused with a stable code. Raised before the session is mutated, so a refusal
    leaves no row and no partial state. ``detail`` names the offending value where useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class PolicyRuleInput:
    """One policy rule as supplied by the caller (before validation/normalization).

    ``effect`` (allow/deny), ``action`` and ``channel`` are controlled vocabularies; ``authority``
    distinguishes a law / provider / product default. ``conditions`` is a structured object whose keys
    must be recognized (see :data:`CONDITION_VALIDATORS`). ``evidence`` must carry a non-empty
    ``reference`` — a rule cannot exist without citing why. ``effective_from``/``expires_at`` are the
    rule's EXPLICIT validity window (optional, but coherent if both are given).
    """

    effect: str
    action: str
    authority: str
    channel: str = RuleChannel.ANY.value
    conditions: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    effective_from: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class RulepackInput:
    """One policy rulepack version as supplied by the caller.

    ``jurisdiction``/``status``/``version`` are intentionally constrained: the jurisdiction must be
    India, the store assigns the next version, and a pack is always a draft. ``rules`` must contain at
    least one rule. ``evidence`` is the optional pack-level evidence; the pack-level validity window is
    explicit and optional.
    """

    jurisdiction: str = INDIA_JURISDICTION
    title: str = ""
    rules: list[PolicyRuleInput] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    effective_from: datetime | None = None
    expires_at: datetime | None = None


# --------------------------------------------------------------------------------------------------
# Condition schema — a small, recognized vocabulary. An unknown key or a bad value is malformed.
# Each validator returns the normalized value (so stored conditions are canonical) or raises.
# --------------------------------------------------------------------------------------------------


def _cond_consent(value: object) -> str:
    allowed = {"required", "not_required", "prior_relationship"}
    token = str(value).strip().lower()
    if token not in allowed:
        raise PolicyRulepackValidationError(
            REASON_INVALID_CONDITION,
            f"condition 'consent' must be one of {sorted(allowed)}, got {value!r}",
            detail=str(value),
        )
    return token


def _cond_string_list(key: str, value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise PolicyRulepackValidationError(
            REASON_INVALID_CONDITION,
            f"condition {key!r} must be a list of strings, got {type(value).__name__}",
            detail=key,
        )
    out: list[str] = []
    for item in value:
        token = str(item).strip()
        if token and token not in out:
            out.append(token)
    if not out:
        raise PolicyRulepackValidationError(
            REASON_INVALID_CONDITION,
            f"condition {key!r} must name at least one value",
            detail=key,
        )
    return sorted(out)


def _cond_time_window(value: object) -> dict:
    if not isinstance(value, dict):
        raise PolicyRulepackValidationError(
            REASON_INVALID_CONDITION,
            "condition 'time_window' must be an object with integer 'start_hour'/'end_hour'",
            detail="time_window",
        )
    out: dict[str, int] = {}
    for bound in ("start_hour", "end_hour"):
        raw = value.get(bound)
        try:
            hour = int(raw)
        except (TypeError, ValueError) as exc:
            raise PolicyRulepackValidationError(
                REASON_INVALID_CONDITION,
                f"condition 'time_window.{bound}' must be an integer hour 0..23, got {raw!r}",
                detail=bound,
            ) from exc
        if not 0 <= hour <= 23:
            raise PolicyRulepackValidationError(
                REASON_INVALID_CONDITION,
                f"condition 'time_window.{bound}' must be in 0..23, got {hour}",
                detail=bound,
            )
        out[bound] = hour
    return out


def _cond_non_negative_int(key: str, value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyRulepackValidationError(
            REASON_INVALID_CONDITION,
            f"condition {key!r} must be a non-negative integer, got {value!r}",
            detail=key,
        ) from exc
    if number < 0:
        raise PolicyRulepackValidationError(
            REASON_INVALID_CONDITION,
            f"condition {key!r} cannot be negative, got {number}",
            detail=key,
        )
    return number


def _cond_bool(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise PolicyRulepackValidationError(
            REASON_INVALID_CONDITION,
            f"condition {key!r} must be a boolean, got {type(value).__name__}",
            detail=key,
        )
    return value


# key -> validator(value) -> normalized value. An unknown key is itself a malformed condition.
CONDITION_VALIDATORS: dict = {
    "consent": _cond_consent,
    "trust_tier": lambda v: _cond_string_list("trust_tier", v),
    "contact_kind": lambda v: _cond_string_list("contact_kind", v),
    "time_window": _cond_time_window,
    "max_frequency_per_day": lambda v: _cond_non_negative_int("max_frequency_per_day", v),
    "registration_required": lambda v: _cond_bool("registration_required", v),
}


def _normalize_conditions(conditions: object) -> dict:
    """Validate & normalize a rule's conditions; an unknown key or a bad value is malformed."""
    if conditions is None:
        return {}
    if not isinstance(conditions, dict):
        raise PolicyRulepackValidationError(
            REASON_INVALID_CONDITION,
            f"conditions must be an object, got {type(conditions).__name__}",
        )
    out: dict = {}
    for raw_key, raw_value in conditions.items():
        key = str(raw_key).strip()
        validator = CONDITION_VALIDATORS.get(key)
        if validator is None:
            raise PolicyRulepackValidationError(
                REASON_INVALID_CONDITION,
                f"unknown condition {key!r}; expected one of {sorted(CONDITION_VALIDATORS)}",
                detail=key,
            )
        out[key] = validator(raw_value)
    return out


def _normalize_enum(value: str, enum: type, code: str) -> str:
    token = (value or "").strip().lower()
    try:
        return enum(token).value
    except ValueError as exc:
        allowed = ", ".join(m.value for m in enum)
        raise PolicyRulepackValidationError(
            code,
            f"unknown {enum.__name__} '{value}'; expected one of: {allowed}",
            detail=value,
        ) from exc


def _validate_evidence(evidence: object, *, code: str, what: str, required: bool) -> dict:
    """Normalize evidence to ``{reference, kind, uri}``. ``reference`` required when ``required``."""
    evidence = evidence or {}
    if not isinstance(evidence, dict):
        raise PolicyRulepackValidationError(code, f"{what} evidence must be an object", detail=what)
    reference = str(evidence.get("reference", "")).strip()
    if required and not reference:
        raise PolicyRulepackValidationError(
            code, f"{what} evidence with a non-empty 'reference' is required"
        )
    return {
        "reference": reference,
        "kind": str(evidence.get("kind", "")).strip(),
        "uri": str(evidence.get("uri", "")).strip(),
    }


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a (possibly naive — SQLite) datetime to tz-aware UTC for comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _validate_window(
    effective_from: datetime | None, expires_at: datetime | None, *, what: str
) -> tuple[datetime | None, datetime | None]:
    """A validity window must be coherent: an expiry cannot be at/before its effective date."""
    ef = _as_utc(effective_from)
    ex = _as_utc(expires_at)
    if ef is not None and ex is not None and ex <= ef:
        raise PolicyRulepackValidationError(
            REASON_INVALID_WINDOW,
            f"{what} expires_at must be after effective_from",
            detail=f"{ef.isoformat()}..{ex.isoformat()}",
        )
    return ef, ex


def _windows_overlap(
    a: tuple[datetime | None, datetime | None], b: tuple[datetime | None, datetime | None]
) -> bool:
    """Do two half-open ``[from, to)`` windows overlap? ``None`` means unbounded (−inf / +inf)."""
    a_from, a_to = a
    b_from, b_to = b
    # a starts before b ends  AND  b starts before a ends.
    if a_to is not None and b_from is not None and b_from >= a_to:
        return False
    if b_to is not None and a_from is not None and a_from >= b_to:
        return False
    return True


@dataclass(frozen=True)
class _NormalizedRule:
    """A rule after validation/normalization — ready to store and to compare for contradictions."""

    effect: str
    action: str
    channel: str
    authority: str
    conditions: dict
    evidence: dict
    effective_from: datetime | None
    expires_at: datetime | None

    def scope_key(self) -> tuple:
        """The identity a contradiction is detected on: same action/channel/authority/conditions.

        A canonical, hashable signature of the conditions makes two rules that say the SAME thing
        comparable regardless of key order.
        """
        canonical = tuple(sorted((k, _freeze(v)) for k, v in self.conditions.items()))
        return (self.action, self.channel, self.authority, canonical)

    def as_json(self) -> dict:
        return {
            "effect": self.effect,
            "action": self.action,
            "channel": self.channel,
            "authority": self.authority,
            "conditions": self.conditions,
            "evidence": self.evidence,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def _freeze(value: object):
    """A hashable canonical form of a normalized condition value (list/dict/scalar)."""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _normalize_rule(rule: PolicyRuleInput) -> _NormalizedRule:
    effect = _normalize_enum(rule.effect, RuleEffect, REASON_UNKNOWN_EFFECT)
    action = _normalize_enum(rule.action, RuleAction, REASON_UNKNOWN_ACTION)
    channel = _normalize_enum(rule.channel, RuleChannel, REASON_UNKNOWN_CHANNEL)
    authority = _normalize_enum(rule.authority, RuleAuthority, REASON_UNKNOWN_AUTHORITY)
    conditions = _normalize_conditions(rule.conditions)
    evidence = _validate_evidence(
        rule.evidence, code=REASON_MISSING_RULE_EVIDENCE, what="rule", required=True
    )
    ef, ex = _validate_window(rule.effective_from, rule.expires_at, what="rule")
    return _NormalizedRule(
        effect=effect,
        action=action,
        channel=channel,
        authority=authority,
        conditions=conditions,
        evidence=evidence,
        effective_from=ef,
        expires_at=ex,
    )


def _assert_no_contradictions(rules: list[_NormalizedRule]) -> None:
    """Refuse a pack where two rules on the identical scope have opposite effects over overlapping
    windows. Same authority + same (action, channel, conditions) can't both allow and deny at once."""
    by_scope: dict[tuple, list[_NormalizedRule]] = {}
    for rule in rules:
        by_scope.setdefault(rule.scope_key(), []).append(rule)
    for scope, group in by_scope.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.effect == b.effect:
                    continue
                if _windows_overlap(
                    (a.effective_from, a.expires_at), (b.effective_from, b.expires_at)
                ):
                    action, channel, authority, _ = scope
                    raise PolicyRulepackValidationError(
                        REASON_CONTRADICTORY_RULES,
                        f"contradictory rules: '{authority}' authority both '{a.effect}' and "
                        f"'{b.effect}' for action '{action}' on channel '{channel}' over an "
                        "overlapping effective window",
                        detail=f"{action}/{channel}/{authority}",
                    )


class CompliancePolicyRulepackStore:
    """Reader/writer for the India policy rulepack registry. Writes DRAFT versions only (activation
    is a later, disabled story)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- reads --------------------------------------------------------------------------------------

    def versions(self, jurisdiction: str = INDIA_JURISDICTION) -> list[CompliancePolicyRulepack]:
        """All rulepack versions for a jurisdiction, oldest first — every prior version is retained."""
        stmt = (
            select(CompliancePolicyRulepack)
            .where(CompliancePolicyRulepack.jurisdiction == (jurisdiction or "").strip().upper())
            .order_by(CompliancePolicyRulepack.version)
        )
        return list(self._session.scalars(stmt).all())

    def get_version(
        self, version: int, jurisdiction: str = INDIA_JURISDICTION
    ) -> CompliancePolicyRulepack | None:
        """One rulepack version for a jurisdiction, or ``None``."""
        stmt = (
            select(CompliancePolicyRulepack)
            .where(CompliancePolicyRulepack.jurisdiction == (jurisdiction or "").strip().upper())
            .where(CompliancePolicyRulepack.version == version)
        )
        return self._session.scalars(stmt).one_or_none()

    def latest(self, jurisdiction: str = INDIA_JURISDICTION) -> CompliancePolicyRulepack | None:
        """The highest-version rulepack for a jurisdiction, or ``None`` if none recorded."""
        stmt = (
            select(CompliancePolicyRulepack)
            .where(CompliancePolicyRulepack.jurisdiction == (jurisdiction or "").strip().upper())
            .order_by(CompliancePolicyRulepack.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    @staticmethod
    def is_active(pack: CompliancePolicyRulepack) -> bool:
        """True only for an ACTIVE pack. Every version this story writes is a draft, so this is always
        ``False`` — recording rules never puts them in force (activation is a disabled capability)."""
        return pack.status == PolicyRulepackStatus.ACTIVE.value

    # -- the one write path -------------------------------------------------------------------------

    def _validate_jurisdiction(self, jurisdiction: str) -> str:
        token = (jurisdiction or "").strip().upper()
        if token != INDIA_JURISDICTION:
            raise PolicyRulepackValidationError(
                REASON_INVALID_JURISDICTION,
                f"the service is India-only; jurisdiction must be '{INDIA_JURISDICTION}', "
                f"got '{jurisdiction}'",
                detail=jurisdiction,
            )
        return token

    def _next_version(self, jurisdiction: str) -> int:
        current = self._session.scalar(
            select(func.max(CompliancePolicyRulepack.version)).where(
                CompliancePolicyRulepack.jurisdiction == jurisdiction
            )
        )
        return int(current or 0) + 1

    def add_version(self, data: RulepackInput) -> CompliancePolicyRulepack:
        """Validate and stage a NEW rulepack version; return it (uncommitted — caller owns UoW).

        Validation runs FIRST over the WHOLE pack (jurisdiction, non-empty rules, each rule's enums /
        evidence / conditions / window, the pack window, and cross-rule contradictions), so a malformed
        OR contradictory pack raises :class:`PolicyRulepackValidationError` before any row is staged —
        a refusal leaves no row and no partial state. The stored row is ALWAYS ``status='draft'`` at
        ``max(version) + 1`` — insert-only, so a version never overwrites a prior one, and recording
        rules never puts them in force.
        """
        jurisdiction = self._validate_jurisdiction(data.jurisdiction)
        if not data.rules:
            raise PolicyRulepackValidationError(
                REASON_NO_RULES, "a rulepack version must contain at least one rule"
            )
        normalized = [_normalize_rule(rule) for rule in data.rules]
        _assert_no_contradictions(normalized)
        pack_evidence = _validate_evidence(
            data.evidence, code=REASON_MISSING_RULE_EVIDENCE, what="rulepack", required=False
        )
        ef, ex = _validate_window(data.effective_from, data.expires_at, what="rulepack")

        pack = CompliancePolicyRulepack(
            jurisdiction=jurisdiction,
            version=self._next_version(jurisdiction),
            title=(data.title or "").strip(),
            rules=[rule.as_json() for rule in normalized],
            evidence=pack_evidence,
            effective_from=ef,
            expires_at=ex,
            # Draft only — promotion to ACTIVE is a disabled future capability.
            status=PolicyRulepackStatus.DRAFT.value,
        )
        self._session.add(pack)
        return pack
