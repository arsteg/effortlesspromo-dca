"""DCA-092 — gate social use by operation.

Pins the per-operation social decision's acceptance behaviour:

- **Content research grants no direct-message or ad-upload permission.** An ELIGIBLE content-research
  decision authorizes content research ONLY — ``permits_operation`` is ``False`` for DM/upload/publish and
  the ``grants_*`` properties are hard ``False``. Separately requesting a DM or an upload is independently
  BLOCKED as a disabled future capability.
- **Unsupported audience platforms stay disabled.** Content research on an audience platform (LinkedIn,
  Facebook, …) is BLOCKED on the platform family; only the business's own company facts is supported.
- **Reproducible, changing no state.** The decision is a pure function of its structured inputs at a given
  ``now`` — nothing is read or written.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.compliance.social_operation_eligibility import (
    DISABLED_FUTURE_OPERATIONS,
    FAMILY_OPERATION,
    FAMILY_PLATFORM,
    REASON_OK,
    REASON_OPERATION_NOT_SUPPORTED,
    REASON_PLATFORM_NOT_SUPPORTED,
    REASON_UNKNOWN_OPERATION,
    REASON_UNKNOWN_PLATFORM,
    SUPPORTED_OPERATIONS,
    SUPPORTED_PLATFORMS,
    SocialOperation,
    SocialOperationDecision,
    SocialOperationEligibility,
    SocialOperationRequest,
    SocialPlatform,
    decide_social_operation,
    evaluate_social_operation,
    operation_is_supported,
    platform_is_supported,
    reason_codes,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
PURPOSE = "draft_company_fact_post"


def _research(platform: str = SocialPlatform.COMPANY_FACTS.value) -> SocialOperationDecision:
    return evaluate_social_operation(
        operation=SocialOperation.CONTENT_RESEARCH.value,
        platform=platform,
        purpose=PURPOSE,
        requested_by="synthetic-operator",
        now=NOW,
    )


# -- the reproducible allow --------------------------------------------------------------------------


def test_content_research_on_company_facts_is_eligible():
    result = _research()
    assert result.decision is SocialOperationEligibility.ELIGIBLE
    assert result.eligible is True
    assert result.reasons == (REASON_OK,)
    assert result.blockers == ()
    assert result.operation == SocialOperation.CONTENT_RESEARCH.value
    assert result.platform == SocialPlatform.COMPANY_FACTS.value
    assert result.purpose == PURPOSE
    assert result.grants_consent is False


# -- acceptance: content research grants NO DM / upload / publish permission -------------------------


def test_content_research_grant_does_not_permit_dm_or_upload_or_publish():
    """The headline DCA-092 case: an ELIGIBLE content-research decision authorizes content research ONLY."""
    result = _research()
    assert result.eligible is True
    # authorizes exactly what it evaluated...
    assert result.permits_operation(SocialOperation.CONTENT_RESEARCH.value) is True
    # ...and nothing else — authority for one social operation never implies another.
    assert result.permits_operation(SocialOperation.DIRECT_MESSAGE.value) is False
    assert result.permits_operation(SocialOperation.CUSTOMER_LIST_UPLOAD.value) is False
    assert result.permits_operation(SocialOperation.CONTENT_PUBLISH.value) is False
    assert result.grants_direct_message is False
    assert result.grants_customer_list_upload is False
    assert result.grants_publish is False


# -- acceptance: the failure cases — DM and upload are independently blocked --------------------------


def test_direct_message_is_blocked_as_disabled_future_capability():
    result = evaluate_social_operation(
        operation=SocialOperation.DIRECT_MESSAGE.value,
        platform=SocialPlatform.LINKEDIN.value,
        now=NOW,
    )
    assert result.decision is SocialOperationEligibility.BLOCKED
    assert result.eligible is False
    assert result.permits_operation(SocialOperation.DIRECT_MESSAGE.value) is False
    # blocked on BOTH families: the operation is disabled AND the platform is an unsupported audience one.
    assert REASON_OPERATION_NOT_SUPPORTED in result.reasons
    assert REASON_PLATFORM_NOT_SUPPORTED in result.reasons
    # operation family comes first (fixed ordering), so it is the decisive blocker.
    assert result.decisive is not None
    assert result.decisive.family == FAMILY_OPERATION
    assert result.decisive.reason == REASON_OPERATION_NOT_SUPPORTED
    assert {b.family for b in result.operation_blockers} == {FAMILY_OPERATION}
    assert {b.family for b in result.platform_blockers} == {FAMILY_PLATFORM}


def test_customer_list_upload_is_blocked_as_disabled_future_capability():
    result = evaluate_social_operation(
        operation=SocialOperation.CUSTOMER_LIST_UPLOAD.value,
        platform=SocialPlatform.FACEBOOK.value,
        now=NOW,
    )
    assert result.decision is SocialOperationEligibility.BLOCKED
    assert result.grants_customer_list_upload is False
    assert result.decisive is not None
    assert result.decisive.reason == REASON_OPERATION_NOT_SUPPORTED


def test_content_publish_is_blocked_as_disabled_future_capability():
    result = evaluate_social_operation(
        operation=SocialOperation.CONTENT_PUBLISH.value,
        platform=SocialPlatform.INSTAGRAM.value,
        now=NOW,
    )
    assert result.decision is SocialOperationEligibility.BLOCKED
    assert result.grants_publish is False
    assert result.decisive is not None
    assert result.decisive.reason == REASON_OPERATION_NOT_SUPPORTED


# -- acceptance: unsupported audience platforms stay disabled ----------------------------------------


@pytest.mark.parametrize(
    "platform",
    [
        SocialPlatform.LINKEDIN.value,
        SocialPlatform.FACEBOOK.value,
        SocialPlatform.INSTAGRAM.value,
        SocialPlatform.X.value,
        SocialPlatform.YOUTUBE.value,
    ],
)
def test_content_research_on_audience_platform_is_blocked(platform: str):
    """Even the supported operation (research) is withheld on an unsupported audience platform."""
    result = _research(platform=platform)
    assert result.decision is SocialOperationEligibility.BLOCKED
    assert result.reasons == (REASON_PLATFORM_NOT_SUPPORTED,)
    assert result.decisive is not None
    assert result.decisive.family == FAMILY_PLATFORM
    # the operation family is clear — only the platform withheld it.
    assert result.operation_blockers == ()


# -- fail-closed on unknown inputs -------------------------------------------------------------------


def test_unknown_operation_fails_closed():
    result = evaluate_social_operation(operation="mint_nft", platform="company_facts", now=NOW)
    assert result.decision is SocialOperationEligibility.BLOCKED
    assert result.reasons == (REASON_UNKNOWN_OPERATION,)


def test_unknown_platform_fails_closed():
    result = evaluate_social_operation(
        operation=SocialOperation.CONTENT_RESEARCH.value, platform="mastodon", now=NOW
    )
    assert result.decision is SocialOperationEligibility.BLOCKED
    assert result.reasons == (REASON_UNKNOWN_PLATFORM,)


def test_unknown_operation_and_platform_reports_both_blockers_in_order():
    result = evaluate_social_operation(operation="mint_nft", platform="mastodon", now=NOW)
    assert result.reasons == (REASON_UNKNOWN_OPERATION, REASON_UNKNOWN_PLATFORM)
    assert result.decisive is not None
    assert result.decisive.family == FAMILY_OPERATION


# -- input hygiene: case / whitespace are normalized -------------------------------------------------


def test_operation_and_platform_are_normalized():
    result = evaluate_social_operation(
        operation="  Content_Research  ", platform=" COMPANY_FACTS ", now=NOW
    )
    assert result.eligible is True
    assert result.operation == SocialOperation.CONTENT_RESEARCH.value
    assert result.platform == SocialPlatform.COMPANY_FACTS.value
    # permits_operation also normalizes the queried operation.
    assert result.permits_operation("  CONTENT_RESEARCH ") is True


# -- purity / reproducibility ------------------------------------------------------------------------


def test_decision_is_reproducible_and_changes_nothing():
    req = SocialOperationRequest(
        operation=SocialOperation.CONTENT_RESEARCH.value,
        platform=SocialPlatform.COMPANY_FACTS.value,
        purpose=PURPOSE,
    )
    a = decide_social_operation(req, now=NOW)
    b = decide_social_operation(req, now=NOW)
    assert a == b


def test_blocked_decision_is_reproducible():
    req = SocialOperationRequest(
        operation=SocialOperation.DIRECT_MESSAGE.value,
        platform=SocialPlatform.X.value,
    )
    a = decide_social_operation(req, now=NOW)
    b = decide_social_operation(req, now=NOW)
    assert a == b
    assert a.decision is SocialOperationEligibility.BLOCKED


# -- the operation/platform support sets are exactly as the story requires ---------------------------


def test_only_content_research_is_supported():
    assert SUPPORTED_OPERATIONS == frozenset({SocialOperation.CONTENT_RESEARCH.value})
    assert operation_is_supported(SocialOperation.CONTENT_RESEARCH.value) is True
    assert operation_is_supported(SocialOperation.DIRECT_MESSAGE.value) is False
    assert DISABLED_FUTURE_OPERATIONS == frozenset(
        {
            SocialOperation.CONTENT_PUBLISH.value,
            SocialOperation.DIRECT_MESSAGE.value,
            SocialOperation.CUSTOMER_LIST_UPLOAD.value,
        }
    )
    # the supported + disabled sets partition every known operation (nothing silently enabled).
    assert SUPPORTED_OPERATIONS | DISABLED_FUTURE_OPERATIONS == frozenset(
        op.value for op in SocialOperation
    )


def test_only_company_facts_platform_is_supported():
    assert SUPPORTED_PLATFORMS == frozenset({SocialPlatform.COMPANY_FACTS.value})
    assert platform_is_supported(SocialPlatform.COMPANY_FACTS.value) is True
    assert platform_is_supported(SocialPlatform.LINKEDIN.value) is False


def test_reason_codes_helper():
    decisions = [_research(), _research(platform=SocialPlatform.LINKEDIN.value)]
    assert reason_codes(decisions) == [(REASON_OK,), (REASON_PLATFORM_NOT_SUPPORTED,)]
