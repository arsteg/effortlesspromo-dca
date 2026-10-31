"""DCA-137 — suggest ambiguous identity matches (the AI-capability integration).

Pins the load-bearing behaviours of :mod:`app.services.identity_match_suggester` — the service that
proposes ambiguous same-entity ORGANIZATION candidate pairs through the one DCA AI chokepoint, to FEED
(never replace) the deterministic DCA-067/068 matchers and the human-authorized, review-only DCA-071
merge:

1. **A weak/ambiguous overlap becomes a REVIEW suggestion.** Two same-named orgs with no conflicting
   identifier re-match as a deterministic ``REVIEW`` candidate, so the AI proposal is kept as a
   review-only :class:`IdentityMatchSuggestion` (status ``proposed``) carrying the deterministic
   evidence/outcome + the model's advisory confidence.
2. **A conflicting typed identifier NO_MATCH is NEVER suggested.** Two same-named orgs with DIFFERENT
   legal-entity CINs re-match as a deterministic ``NO_MATCH``; the AI proposal is DROPPED (recorded),
   nothing is stored — an AI suggestion can never override the deterministic refutation.
3. **A suggestion is review-only and can never execute a merge.** Suggesting creates NO
   :class:`IdentityMerge` and touches no consent/restriction state; and the separate DCA-071 merge
   refuses an AI/automation actor outright, so a suggestion can never become an automatic collapse.
4. **The model cannot invent / reach an un-authorized id.** A proposed pair referencing an id outside
   the authorized candidate set (incl. another tenant's) is dropped DCA-side, nothing stored.
5. **Tenant isolation.** Suggestions are business-private; a candidate id from another business reads as
   not-found.
6. **Idempotent re-run.** Re-suggesting the same pair returns the existing row — no duplicate.
7. **Fail-closed denials.** A disabled capability, a rejected gateway output and fewer than two
   authorized candidates each stage NO suggestion (and never call the model where applicable).

No AI, no network, no model, no spend — the deterministic fixture transport stands in for the gateway
(the real gateway capability schema + its rule_check bound are pinned by effortlesspromo-ai's own
contract test). Unit-tested on in-memory SQLite; the RLS/migration paths for the table are covered by
``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ai import AiCapabilityFacade, FakeAiGatewayTransport
from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    ConsentReceipt,
    IdentityMatchSuggestion,
    IdentityMerge,
    ProcessingRestriction,
)
from app.services.identity_match_suggester import (
    CAPABILITY,
    EXCL_DETERMINISTIC_REFUTED,
    EXCL_NOT_A_CANDIDATE,
    EXCL_SUBJECT_NOT_FOUND,
    REASON_CAPABILITY_DISABLED,
    REASON_NO_CANDIDATES,
    REASON_OUTPUT_REJECTED,
    IdentityMatchSuggestionStore,
    suggest_identity_matches,
)
from app.services.identity_merge import (
    REASON_AI_ACTOR_CANNOT_MERGE,
    IdentityMergeAuthorizationError,
    IdentityMergeInput,
    IdentityMergeStore,
    IdentityReviewer,
)
from app.services.org_observation import ExternalIdInput, ObservationInput, OrgObservationStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0137", type=ActorType.USER)

# Two valid, DIFFERENT legal-entity CINs (same shape, different registration number) — a conflicting
# typed identifier that the DCA-067 matcher treats as a NO_MATCH refutation.
CIN_ONE = "U17120MH2026PTC900099"
CIN_TWO = "U17120MH2026PTC900100"


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _org(session, business_id: str, name: str, *, cin: str | None = None, state: str = "") -> str:
    external = [ExternalIdInput(scheme="cin", value=cin)] if cin else []
    obs = OrgObservationStore(session, business_id).create(
        ObservationInput(
            entity_type="legal_entity", display_name=name, state_code=state, external_ids=external
        ),
        actor=ACTOR,
        now=NOW,
    )
    session.flush()
    return obs.id


def _facade(suggestions, *, enabled=True, fail=False) -> AiCapabilityFacade:
    """A fixture transport returning a crafted model output (the proposed pairs). No network/model/spend."""
    output = {"suggestions": suggestions, "confidence": 0.7}
    return AiCapabilityFacade(
        FakeAiGatewayTransport(enabled=enabled, fail=fail, structured_output=output)
    )


def _pair(a: str, b: str, confidence: float = 0.75, rationale: str = "similar names") -> dict:
    return {"subjectAId": a, "subjectBId": b, "confidence": confidence, "rationale": rationale}


# 1) A weak/ambiguous overlap → a REVIEW suggestion -------------------------------------------


def test_ambiguous_overlap_becomes_a_review_suggestion(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited", state="MH")
        b = _org(session, BIZ_A, "Acme Textiles Private Limited", state="KA")
        facade = _facade([_pair(a, b)])

        result = suggest_identity_matches(
            session, facade, BIZ_A, candidate_ids=[a, b], invocation_key="sugg:1"
        )
        session.commit()

    assert result.produced
    assert result.suggestion_count == 1
    sug = result.suggestions[0]
    assert sug.status == "proposed"
    assert sug.match_outcome == "review"  # deterministic DCA-067 conclusion (weak name overlap)
    assert {sug.subject_a_id, sug.subject_b_id} == {a, b}
    assert sug.ai_confidence == 0.75
    assert sug.synthetic is True
    # Evidence snapshot carries the deterministic name signal (company facts only).
    assert any(s["kind"] == "name" for s in sug.evidence)
    # No merge was ever recorded — proposing is not merging.
    with session_factory() as session:
        assert session.query(IdentityMerge).count() == 0
        assert session.query(IdentityMatchSuggestion).count() == 1


# 2) A conflicting typed identifier NO_MATCH is NEVER suggested -------------------------------


def test_conflicting_identifier_pair_is_not_suggested(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited", cin=CIN_ONE, state="MH")
        b = _org(session, BIZ_A, "Acme Textiles Private Limited", cin=CIN_TWO, state="MH")
        facade = _facade([_pair(a, b, confidence=0.95)])

        result = suggest_identity_matches(
            session, facade, BIZ_A, candidate_ids=[a, b], invocation_key="sugg:2"
        )
        session.commit()

    # A confident-looking AI proposal is overridden by the deterministic NO_MATCH: nothing stored.
    assert result.produced
    assert result.suggestion_count == 0
    dropped = {(e["reason"], e.get("detail")) for e in result.excluded}
    assert (EXCL_DETERMINISTIC_REFUTED, "no_match") in dropped
    with session_factory() as session:
        assert session.query(IdentityMatchSuggestion).count() == 0
        assert session.query(IdentityMerge).count() == 0


# 3) A suggestion is review-only and can never execute a merge --------------------------------


def test_suggestion_is_review_only_and_an_ai_actor_cannot_merge(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited")
        b = _org(session, BIZ_A, "Acme Textiles Private Limited")
        result = suggest_identity_matches(
            session, _facade([_pair(a, b)]), BIZ_A, candidate_ids=[a, b], invocation_key="sugg:3"
        )
        session.commit()
        assert result.suggestion_count == 1

        # The review-only suggestion changed NO permission state and recorded NO merge.
        assert session.query(IdentityMerge).count() == 0
        assert session.query(ConsentReceipt).count() == 0
        assert session.query(ProcessingRestriction).count() == 0

        # The separate DCA-071 merge refuses an AI/automation actor outright — a suggestion can never
        # be auto-executed into a merge.
        ai_reviewer = IdentityReviewer(
            subject="ai-bot", roles=("admin",), actor_type=ActorType.SERVICE.value
        )
        with pytest.raises(IdentityMergeAuthorizationError) as exc:
            IdentityMergeStore(session, BIZ_A).merge(
                IdentityMergeInput(
                    subject_type="organization", primary_subject_id=a, duplicate_subject_id=b
                ),
                ai_reviewer,
            )
        assert exc.value.code == REASON_AI_ACTOR_CANNOT_MERGE
        session.rollback()

    with session_factory() as session:
        assert session.query(IdentityMerge).count() == 0


# 4) The model cannot reach an un-authorized / cross-tenant id --------------------------------


def test_unauthorized_id_in_a_proposal_is_dropped(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited")
        b = _org(session, BIZ_A, "Acme Textiles Private Limited")
        # A third org exists but is NOT passed as an authorized candidate.
        c = _org(session, BIZ_A, "Acme Textiles Private Limited")
        facade = _facade([_pair(a, c), _pair(a, "intruder-999")])

        result = suggest_identity_matches(
            session, facade, BIZ_A, candidate_ids=[a, b], invocation_key="sugg:4"
        )
        session.commit()

    assert result.suggestion_count == 0
    reasons = {e["reason"] for e in result.excluded}
    assert EXCL_NOT_A_CANDIDATE in reasons
    with session_factory() as session:
        assert session.query(IdentityMatchSuggestion).count() == 0


# 5) Tenant isolation -------------------------------------------------------------------------


def test_tenant_scoped(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited")
        b = _org(session, BIZ_A, "Acme Textiles Private Limited")
        b_other = _org(session, BIZ_B, "Acme Textiles Private Limited")
        # Business A suggests among its own candidates; a candidate id from B is not-found in A.
        facade = _facade([_pair(a, b)])
        result = suggest_identity_matches(
            session, facade, BIZ_A, candidate_ids=[a, b, b_other], invocation_key="sugg:5"
        )
        session.commit()

    assert result.suggestion_count == 1
    assert any(
        e["reason"] == EXCL_SUBJECT_NOT_FOUND and e.get("subjectId") == b_other
        for e in result.excluded
    )
    with session_factory() as session:
        assert len(IdentityMatchSuggestionStore(session, BIZ_A).suggestions()) == 1
        assert IdentityMatchSuggestionStore(session, BIZ_B).suggestions() == []


# 6) Idempotent re-run ------------------------------------------------------------------------


def test_idempotent_rerun(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited")
        b = _org(session, BIZ_A, "Acme Textiles Private Limited")
        suggest_identity_matches(
            session, _facade([_pair(a, b)]), BIZ_A, candidate_ids=[a, b], invocation_key="sugg:6"
        )
        session.commit()

    with session_factory() as session:
        # Re-run with the pair in the OPPOSITE order — still the same canonical pair, no duplicate.
        again = suggest_identity_matches(
            session, _facade([_pair(b, a)]), BIZ_A, candidate_ids=[a, b], invocation_key="sugg:6"
        )
        session.commit()
        assert again.suggestion_count == 1

    with session_factory() as session:
        assert session.query(IdentityMatchSuggestion).count() == 1


# 7) Fail-closed denials ----------------------------------------------------------------------


def test_disabled_capability_stages_nothing_and_never_calls_the_model(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited")
        b = _org(session, BIZ_A, "Acme Textiles Private Limited")
        transport = FakeAiGatewayTransport(
            enabled=False, structured_output={"suggestions": [_pair(a, b)], "confidence": 0.7}
        )
        result = suggest_identity_matches(
            session, AiCapabilityFacade(transport), BIZ_A, candidate_ids=[a, b], invocation_key="x"
        )
        session.commit()

    assert result.refused_reason == REASON_CAPABILITY_DISABLED
    assert transport.calls == []  # never called the model
    with session_factory() as session:
        assert session.query(IdentityMatchSuggestion).count() == 0


def test_rejected_gateway_output_stages_nothing(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited")
        b = _org(session, BIZ_A, "Acme Textiles Private Limited")
        result = suggest_identity_matches(
            session,
            _facade([_pair(a, b)], fail=True),
            BIZ_A,
            candidate_ids=[a, b],
            invocation_key="x",
        )
        session.commit()

    assert result.refused_reason == REASON_OUTPUT_REJECTED
    with session_factory() as session:
        assert session.query(IdentityMatchSuggestion).count() == 0


def test_fewer_than_two_candidates_refused_without_a_model_call(session_factory):
    with session_factory() as session:
        a = _org(session, BIZ_A, "Acme Textiles Private Limited")
        transport = FakeAiGatewayTransport(structured_output={"suggestions": [], "confidence": 0.3})
        result = suggest_identity_matches(
            session, AiCapabilityFacade(transport), BIZ_A, candidate_ids=[a], invocation_key="x"
        )
        session.commit()

    assert result.refused_reason == REASON_NO_CANDIDATES
    assert transport.calls == []
    with session_factory() as session:
        assert session.query(IdentityMatchSuggestion).count() == 0


def test_capability_key_is_registered_in_the_dca_allowlist():
    from app.ai import KNOWN_CAPABILITIES

    assert CAPABILITY in KNOWN_CAPABILITIES
