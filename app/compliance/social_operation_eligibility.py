"""DCA-092 — gate social use by operation (one authorization per social operation, kept separate).

Key: ``social-policy`` · source sections 5, 10. Stage: India channel eligibility.

A pure, deterministic Python evaluator that answers ONE question for the social surface — *may this
specific social OPERATION, on this platform, proceed?* — while keeping the four social operations a
business might want **separately authorized**, so authority for one can never leak into another:

**The four operations, kept apart.**

- ``CONTENT_RESEARCH`` — drafting **company-fact** content (researching a business's OWN public
  company information so it can be summarized/drafted). This is the ONE operation this story supports:
  it reads/organizes facts; it reaches no audience and messages no one.
- ``CONTENT_PUBLISH`` — posting content to a social audience surface. A disabled future capability.
- ``DIRECT_MESSAGE`` — sending a direct message to a user on a platform. A disabled future capability.
- ``CUSTOMER_LIST_UPLOAD`` — uploading a customer list / audience to a platform (the social-ads
  "ad upload"). A disabled future capability (its own story, with source-rights + rulepack review).

**Two independent gate families compose into one decision:**

A) **operation** — is the requested operation a *supported* one at all? Only ``CONTENT_RESEARCH`` is.
   ``CONTENT_PUBLISH`` / ``DIRECT_MESSAGE`` / ``CUSTOMER_LIST_UPLOAD`` are deliberately disabled future
   capabilities and ALWAYS block here, regardless of platform.

B) **platform** — is the target an *supported* surface? The only supported research surface is the
   business's own ``COMPANY_FACTS``. Every real audience platform (LinkedIn, Facebook, Instagram, X,
   YouTube, …) is an **unsupported audience platform** and stays disabled — so publishing/DMing/uploading
   is withheld twice over (operation disabled AND platform unsupported).

Load-bearing guarantee (the DCA-092 acceptance criterion):

- **Content research grants NO direct-message or ad-upload permission.** An ``ELIGIBLE`` content-research
  decision authorizes content research ONLY: :meth:`SocialOperationDecision.permits_operation` returns
  ``True`` for ``content_research`` and ``False`` for every other operation, and the convenience
  properties :attr:`grants_direct_message` / :attr:`grants_customer_list_upload` / :attr:`grants_publish`
  are hard ``False``. Separately requesting a DM or an upload is independently BLOCKED as a disabled
  future capability — no content-research outcome ever changes that.
- **Reproducible, with the relevant denial, changing no state.** The decision is a pure function of its
  structured inputs at a given ``now`` — it reads nothing and writes nothing, so both the eligible
  result and every denial are perfectly reproducible with no state change.

This evaluator is deliberately **not wired** into any worker, acquisition or HTTP path — nothing calls
it except its own tests and the manual-check scenario. Concluding ELIGIBLE for content research
publishes nothing, messages no one and uploads nothing: the publish / DM / upload operations stay
disabled future capabilities (each its own story). ELIGIBLE is NOT recipient consent and NOT a send,
publish or upload authorization.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

# The policy key this evaluator speaks for (source sections 5, 10). Social is intentionally NOT a member
# of ``RuleChannel`` (which enumerates outbound *message* channels) — social use is gated per OPERATION.
SOCIAL_POLICY_KEY = "social-policy"


class SocialOperation(str, Enum):
    """A distinct social operation — each independently authorized, authority for one never implying another.

    - ``CONTENT_RESEARCH`` — draft company-fact content from a business's own public information. The ONLY
      operation this story supports. Reaches no audience; sends/uploads nothing.
    - ``CONTENT_PUBLISH`` — publish content to an audience surface. Disabled future capability.
    - ``DIRECT_MESSAGE`` — send a direct message to a user. Disabled future capability.
    - ``CUSTOMER_LIST_UPLOAD`` — upload a customer list / audience (the social-ads "ad upload"). Disabled
      future capability.
    """

    CONTENT_RESEARCH = "content_research"
    CONTENT_PUBLISH = "content_publish"
    DIRECT_MESSAGE = "direct_message"
    CUSTOMER_LIST_UPLOAD = "customer_list_upload"


# The ONLY supported operation. Everything else is a deliberately disabled future capability — kept in a
# named set so the separation is explicit and a future story flips exactly one membership to enable it.
SUPPORTED_OPERATIONS = frozenset({SocialOperation.CONTENT_RESEARCH.value})

# The social operations that exist as concepts but are withheld (disabled future capabilities). This set
# is exactly the complement of SUPPORTED_OPERATIONS over the known operations — named for readability.
DISABLED_FUTURE_OPERATIONS = frozenset(
    {
        SocialOperation.CONTENT_PUBLISH.value,
        SocialOperation.DIRECT_MESSAGE.value,
        SocialOperation.CUSTOMER_LIST_UPLOAD.value,
    }
)


class SocialPlatform(str, Enum):
    """Where a social operation would act.

    - ``COMPANY_FACTS`` — the business's OWN public company information: the one supported *research*
      surface. Not an audience platform; nothing is sent to or uploaded onto it.
    - every other member is a real **audience platform** where publishing / DMs / uploads would occur —
      all **unsupported** (disabled) until a dedicated integration story lands with its rights review.
    """

    COMPANY_FACTS = "company_facts"
    LINKEDIN = "linkedin"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    X = "x"
    YOUTUBE = "youtube"


# The ONLY supported surface — the business's own company facts (the research substrate). Every audience
# platform is unsupported and stays disabled.
SUPPORTED_PLATFORMS = frozenset({SocialPlatform.COMPANY_FACTS.value})


def operation_is_supported(operation: str) -> bool:
    """Whether a social operation is a supported one (only content research is)."""
    return operation.strip().casefold() in SUPPORTED_OPERATIONS


def platform_is_supported(platform: str) -> bool:
    """Whether a target platform is a supported surface (only the business's own company facts is)."""
    return platform.strip().casefold() in SUPPORTED_PLATFORMS


class SocialOperationEligibility(str, Enum):
    """The two outcomes of a per-operation social decision.

    - ``ELIGIBLE`` — both families passed: the operation is supported AND the platform is supported. The
      operation MAY be offered to a (future) path that still re-gates everything. Scoped to exactly the
      evaluated operation; never recipient consent and never a publish/DM/upload authorization.
    - ``BLOCKED`` — at least one gate withheld. Fail-closed is the default (unknown op/platform block).
    """

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"


# The two gate families, carried on every blocker so the separation is explicit and testable.
FAMILY_OPERATION = "operation"
FAMILY_PLATFORM = "platform"

# Stable, test-asserted reason codes — one per distinct gate that can withhold eligibility.
REASON_OK = "ok"  # ELIGIBLE: every gate passed
REASON_OPERATION_NOT_SUPPORTED = (
    "operation_not_supported"  # a disabled future capability (publish / DM / upload)
)
REASON_UNKNOWN_OPERATION = "unknown_operation"  # unrecognized operation — fail-closed
REASON_PLATFORM_NOT_SUPPORTED = (
    "platform_not_supported"  # an unsupported audience platform (publish/DM/upload target)
)
REASON_UNKNOWN_PLATFORM = "unknown_platform"  # unrecognized platform — fail-closed


_KNOWN_OPERATIONS = frozenset(op.value for op in SocialOperation)
_KNOWN_PLATFORMS = frozenset(p.value for p in SocialPlatform)


@dataclass(frozen=True)
class EligibilityBlocker:
    """One gate that withheld eligibility — its family, gate name, a stable reason code, and detail."""

    family: str
    gate: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class SocialOperationRequest:
    """The structured inputs to a per-operation social decision.

    ``operation`` is the social operation being requested (content research / publish / DM / upload).
    ``platform`` is the target surface. ``purpose`` and ``requested_by`` are provenance echoed back into
    the reported decision; they are RECORDED context only and never relax any gate.
    """

    operation: str
    platform: str
    purpose: str = ""
    requested_by: str = ""


@dataclass(frozen=True)
class SocialOperationDecision:
    """The result of a per-operation social decision — reproducible and auditable, changing no state.

    ``blockers`` lists EVERY gate that withheld eligibility, in a FIXED order (operation family first,
    then platform), each tagged with its :attr:`EligibilityBlocker.family`. ``decisive`` is the first
    blocker. ``eligible`` is true only when there are no blockers at all.

    The decision authorizes **exactly** the evaluated ``operation`` — never another. That is the DCA-092
    invariant: an ELIGIBLE content-research decision :meth:`permits_operation` only content research, and
    the ``grants_*`` properties for direct message / upload / publish are hard ``False``.
    """

    decision: SocialOperationEligibility
    operation: str
    platform: str
    purpose: str
    requested_by: str
    blockers: tuple[EligibilityBlocker, ...]
    evaluated_at: str

    @property
    def eligible(self) -> bool:
        return self.decision is SocialOperationEligibility.ELIGIBLE

    @property
    def reasons(self) -> tuple[str, ...]:
        """Stable reason codes: ``(ok,)`` when eligible, else one per blocker in gate order."""
        if self.eligible:
            return (REASON_OK,)
        return tuple(b.reason for b in self.blockers)

    @property
    def decisive(self) -> EligibilityBlocker | None:
        return self.blockers[0] if self.blockers else None

    @property
    def operation_blockers(self) -> tuple[EligibilityBlocker, ...]:
        """The withheld gates in the OPERATION family (unsupported / unknown operation)."""
        return tuple(b for b in self.blockers if b.family == FAMILY_OPERATION)

    @property
    def platform_blockers(self) -> tuple[EligibilityBlocker, ...]:
        """The withheld gates in the PLATFORM family (unsupported audience platform / unknown platform)."""
        return tuple(b for b in self.blockers if b.family == FAMILY_PLATFORM)

    def permits_operation(self, operation: str) -> bool:
        """This decision authorizes EXACTLY the operation it evaluated — never a different one.

        ``True`` only when ELIGIBLE AND ``operation`` is the evaluated operation. So a content-research
        decision returns ``False`` for ``direct_message`` and ``customer_list_upload`` — authority for one
        social operation never implies another. Always ``False`` when BLOCKED.
        """
        return self.eligible and operation.strip().casefold() == self.operation.strip().casefold()

    @property
    def grants_direct_message(self) -> bool:
        """Whether this decision authorizes sending a direct message. Only a DM decision ever could, and
        even that is a disabled future capability — a content-research decision is always ``False``."""
        return self.permits_operation(SocialOperation.DIRECT_MESSAGE.value)

    @property
    def grants_customer_list_upload(self) -> bool:
        """Whether this decision authorizes a customer-list / audience upload ("ad upload"). A
        content-research decision is always ``False`` — research never grants upload permission."""
        return self.permits_operation(SocialOperation.CUSTOMER_LIST_UPLOAD.value)

    @property
    def grants_publish(self) -> bool:
        """Whether this decision authorizes publishing to an audience surface. A content-research
        decision is always ``False``."""
        return self.permits_operation(SocialOperation.CONTENT_PUBLISH.value)

    @property
    def grants_consent(self) -> bool:
        """ELIGIBLE is never recipient consent and never a publish/DM/upload authorization. Kept explicit
        so no caller mistakes a research grant for an outbound one."""
        return False


def decide_social_operation(
    request: SocialOperationRequest, *, now: datetime | None = None
) -> SocialOperationDecision:
    """Combine the operation family and the platform family into one per-operation social decision —
    pure, no I/O, no AI.

    Every gate is evaluated (not short-circuited) and each failure appends a blocker in a FIXED order,
    operation family first. ELIGIBLE requires ALL gates to pass, so only a supported operation
    (content research) on a supported surface (the business's own company facts) is eligible. The
    function reads nothing and changes nothing, so a given ``(request, now)`` always yields the identical
    result.
    """
    moment = now or datetime.now(UTC)
    blockers: list[EligibilityBlocker] = []

    op = request.operation.strip().casefold()
    platform = request.platform.strip().casefold()

    # -- A) OPERATION family (is this operation supported at all?) ----------------------------------
    #
    # Only content research is supported; publish / DM / upload are deliberately disabled future
    # capabilities and block here independent of the platform. Unknown operations fail-closed.
    if op not in _KNOWN_OPERATIONS:
        blockers.append(
            EligibilityBlocker(
                FAMILY_OPERATION,
                "operation",
                REASON_UNKNOWN_OPERATION,
                detail=f"unrecognized operation {request.operation!r}",
            )
        )
    elif op not in SUPPORTED_OPERATIONS:
        blockers.append(
            EligibilityBlocker(
                FAMILY_OPERATION,
                "operation",
                REASON_OPERATION_NOT_SUPPORTED,
                detail=f"{op} is a disabled future capability",
            )
        )

    # -- B) PLATFORM family (is the target surface supported?) --------------------------------------
    #
    # The only supported surface is the business's own company facts; every real audience platform is an
    # unsupported audience platform and stays disabled. Unknown platforms fail-closed.
    if platform not in _KNOWN_PLATFORMS:
        blockers.append(
            EligibilityBlocker(
                FAMILY_PLATFORM,
                "platform",
                REASON_UNKNOWN_PLATFORM,
                detail=f"unrecognized platform {request.platform!r}",
            )
        )
    elif platform not in SUPPORTED_PLATFORMS:
        blockers.append(
            EligibilityBlocker(
                FAMILY_PLATFORM,
                "platform",
                REASON_PLATFORM_NOT_SUPPORTED,
                detail=f"{platform} is an unsupported audience platform",
            )
        )

    decision = (
        SocialOperationEligibility.BLOCKED if blockers else SocialOperationEligibility.ELIGIBLE
    )
    return SocialOperationDecision(
        decision=decision,
        operation=op,
        platform=platform,
        purpose=request.purpose,
        requested_by=request.requested_by,
        blockers=tuple(blockers),
        evaluated_at=moment.isoformat(),
    )


def evaluate_social_operation(
    *,
    operation: str,
    platform: str,
    purpose: str = "",
    requested_by: str = "",
    now: datetime | None = None,
) -> SocialOperationDecision:
    """Assemble a :class:`SocialOperationRequest` then :func:`decide_social_operation`.

    A convenience that takes the operation/platform directly. Still pure: it opens no DB, calls no adapter
    and consults no model.
    """
    request = SocialOperationRequest(
        operation=operation,
        platform=platform,
        purpose=purpose,
        requested_by=requested_by,
    )
    return decide_social_operation(request, now=now)


def reason_codes(
    decisions: Iterable[SocialOperationDecision],
) -> list[tuple[str, ...]]:
    """Small helper for scenarios/tests: the reason tuples of a sequence of decisions, in order."""
    return [d.reasons for d in decisions]
