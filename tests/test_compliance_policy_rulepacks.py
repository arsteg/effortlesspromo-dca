"""DCA-052 — versioned India compliance policy rulepack schemas.

Service unit tests (in-memory SQLite) pin the store logic and the acceptance behaviour:

- a complete, valid pack validates and stages ONE draft row carrying the normalized rules (effect,
  action, channel, authority = law/provider/product, conditions, evidence, effective dates);
- malformed rules are refused before anything is staged, each with a stable code (non-India
  jurisdiction, empty pack, unknown action/channel/effect/authority, a rule missing evidence, an
  unknown/bad condition, an incoherent validity window) — a refusal leaves NO row;
- CONTRADICTORY rules are refused: same action/channel/authority/conditions with opposite effects over
  overlapping windows cannot be saved, while the SAME opposition across different authorities (law vs
  product) is allowed (that is precedence, not a contradiction), and opposite effects on
  non-overlapping windows are allowed;
- no rule becomes live just by existing: every stored pack is a draft (``is_active`` is False), and
  versions are insert-only (a prior version is preserved, never overwritten).

``compliance_policy_rulepacks`` is platform control-plane data (not tenant-scoped), so the behaviour is
app-level; the transaction/migration paths for the table are covered by ``tests/test_migrations.py``
(disposable Postgres) and its exclusion from RLS by ``tests/test_rls.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.compliance.policy_rulepacks import (
    REASON_CONTRADICTORY_RULES,
    REASON_INVALID_CONDITION,
    REASON_INVALID_JURISDICTION,
    REASON_INVALID_WINDOW,
    REASON_MISSING_RULE_EVIDENCE,
    REASON_NO_RULES,
    REASON_UNKNOWN_ACTION,
    REASON_UNKNOWN_AUTHORITY,
    REASON_UNKNOWN_CHANNEL,
    REASON_UNKNOWN_EFFECT,
    CompliancePolicyRulepackStore,
    PolicyRuleInput,
    PolicyRulepackValidationError,
    RulepackInput,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import CompliancePolicyRulepack, PolicyRulepackStatus

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _evidence(ref: str = "DPDP-2023-s7") -> dict:
    return {"reference": ref, "kind": "statute", "uri": "https://example.test/law"}


def _complete_pack(**overrides) -> RulepackInput:
    """A complete, valid India pack: a statutory contact rule and a conservative product default."""
    data = {
        "jurisdiction": "IN",
        "title": "India baseline policy",
        "evidence": _evidence("POLICY-REVIEW-2026-11"),
        "rules": [
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required", "trust_tier": ["official_registry"]},
                evidence=_evidence(),
                effective_from=NOW,
                expires_at=NOW + timedelta(days=365),
            ),
            PolicyRuleInput(
                effect="deny",
                action="contact",
                channel="sms",
                authority="product",
                conditions={"registration_required": True},
                evidence=_evidence("PRODUCT-POLICY-1"),
            ),
        ],
    }
    data.update(overrides)
    return RulepackInput(**data)


# --- the happy path: a complete fixture validates, stages one draft, is never live ----------------


def test_complete_pack_stages_one_draft(session_factory):
    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        pack = store.add_version(_complete_pack())
        session.commit()
        pack_id = pack.id

    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        rows = store.versions("IN")
        assert len(rows) == 1
        stored = session.get(CompliancePolicyRulepack, pack_id)
        assert stored is not None
        assert stored.jurisdiction == "IN"
        assert stored.version == 1
        assert stored.status == PolicyRulepackStatus.DRAFT.value
        # no rule becomes live just by existing.
        assert store.is_active(stored) is False
        assert len(stored.rules) == 2
        first = stored.rules[0]
        assert first["effect"] == "allow"
        assert first["action"] == "contact"
        assert first["channel"] == "email"
        assert first["authority"] == "law"
        assert first["conditions"] == {
            "consent": "required",
            "trust_tier": ["official_registry"],
        }
        assert first["evidence"]["reference"] == "DPDP-2023-s7"
        assert first["effective_from"] is not None


def test_versions_are_insert_only_and_preserved(session_factory):
    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        store.add_version(_complete_pack())
        store.add_version(_complete_pack(title="v2"))
        session.commit()

    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        rows = store.versions("IN")
        assert [r.version for r in rows] == [1, 2]
        assert store.latest("IN").version == 2
        assert rows[0].title == "India baseline policy"  # prior version preserved, not overwritten


# --- malformed rules cannot be saved; a refusal stages nothing ------------------------------------


def _assert_nothing_staged(session_factory):
    with session_factory() as session:
        assert CompliancePolicyRulepackStore(session).versions("IN") == []


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda p: setattr(p, "jurisdiction", "US"), REASON_INVALID_JURISDICTION),
        (lambda p: setattr(p, "rules", []), REASON_NO_RULES),
        (lambda p: setattr(p.rules[0], "action", "teleport"), REASON_UNKNOWN_ACTION),
        (lambda p: setattr(p.rules[0], "channel", "carrier_pigeon"), REASON_UNKNOWN_CHANNEL),
        (lambda p: setattr(p.rules[0], "effect", "maybe"), REASON_UNKNOWN_EFFECT),
        (lambda p: setattr(p.rules[0], "authority", "vibes"), REASON_UNKNOWN_AUTHORITY),
        (lambda p: setattr(p.rules[0], "evidence", {}), REASON_MISSING_RULE_EVIDENCE),
        (lambda p: setattr(p.rules[0], "conditions", {"nonsense": 1}), REASON_INVALID_CONDITION),
        (
            lambda p: setattr(p.rules[0], "conditions", {"time_window": {"start_hour": 99}}),
            REASON_INVALID_CONDITION,
        ),
        (
            lambda p: setattr(p.rules[0], "conditions", {"consent": "whenever"}),
            REASON_INVALID_CONDITION,
        ),
    ],
)
def test_malformed_pack_is_refused_and_stages_nothing(session_factory, mutate, code):
    pack = _complete_pack()
    mutate(pack)
    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        with pytest.raises(PolicyRulepackValidationError) as exc:
            store.add_version(pack)
        assert exc.value.code == code
        session.rollback()
    _assert_nothing_staged(session_factory)


def test_incoherent_rule_window_is_refused(session_factory):
    pack = _complete_pack(
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="acquire",
                authority="law",
                evidence=_evidence(),
                effective_from=NOW,
                expires_at=NOW - timedelta(days=1),  # expiry before effective date
            )
        ]
    )
    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        with pytest.raises(PolicyRulepackValidationError) as exc:
            store.add_version(pack)
        assert exc.value.code == REASON_INVALID_WINDOW
        session.rollback()
    _assert_nothing_staged(session_factory)


def test_missing_effective_dates_are_allowed(session_factory):
    """A rule with no effective dates is a valid (unbounded) rule — not every rule needs a window."""
    pack = _complete_pack(
        rules=[
            PolicyRuleInput(
                effect="deny",
                action="acquire",
                authority="product",
                evidence=_evidence("PRODUCT-DEFAULT"),
            )
        ]
    )
    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        stored = store.add_version(pack)
        session.commit()
        assert stored.rules[0]["effective_from"] is None
        assert stored.rules[0]["expires_at"] is None
        assert stored.status == PolicyRulepackStatus.DRAFT.value


# --- contradictory rules cannot be saved ----------------------------------------------------------


def test_contradictory_rules_same_authority_are_refused(session_factory):
    pack = _complete_pack(
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=_evidence(),
            ),
            PolicyRuleInput(
                effect="deny",  # same scope, opposite effect, overlapping (unbounded) window
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=_evidence(),
            ),
        ]
    )
    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        with pytest.raises(PolicyRulepackValidationError) as exc:
            store.add_version(pack)
        assert exc.value.code == REASON_CONTRADICTORY_RULES
        session.rollback()
    _assert_nothing_staged(session_factory)


def test_opposite_effects_across_authorities_is_not_a_contradiction(session_factory):
    """law ALLOW vs product DENY on the same scope is precedence (resolvable), not a contradiction."""
    pack = _complete_pack(
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=_evidence(),
            ),
            PolicyRuleInput(
                effect="deny",
                action="contact",
                channel="email",
                authority="product",
                conditions={"consent": "required"},
                evidence=_evidence(),
            ),
        ]
    )
    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        stored = store.add_version(pack)
        session.commit()
        assert len(stored.rules) == 2


def test_opposite_effects_on_non_overlapping_windows_is_allowed(session_factory):
    pack = _complete_pack(
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="sms",
                authority="provider",
                evidence=_evidence(),
                effective_from=NOW,
                expires_at=NOW + timedelta(days=30),
            ),
            PolicyRuleInput(
                effect="deny",
                action="contact",
                channel="sms",
                authority="provider",
                evidence=_evidence(),
                effective_from=NOW + timedelta(days=30),  # starts exactly when the first ends
                expires_at=NOW + timedelta(days=60),
            ),
        ]
    )
    with session_factory() as session:
        store = CompliancePolicyRulepackStore(session)
        stored = store.add_version(pack)
        session.commit()
        assert len(stored.rules) == 2
