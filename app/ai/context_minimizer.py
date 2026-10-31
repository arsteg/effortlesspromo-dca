"""DCA-131 — minimize the AI-service context before egress to the effortlesspromo-ai gateway.

Key: ``ai-minimize`` · source sections 4, 8, 9. Stage: existing AI-service integration.

**Load-bearing guarantee:** for every capability the DCA may request, ONLY the fields that capability
needs — the authorized evidence and provenance references — ever leave the DCA for the model gateway.
Everything else is dropped BEFORE egress: secrets/credentials, the internal send sentinel, other-tenant
evidence, and unnecessary personal fields. This minimization is DCA-owned and runs *in addition to* the
gateway's own privacy screens (``effortlesspromo-ai/app/gateway/privacy.py``): a recording fake AI HTTP
service placed in front of the gateway cannot observe a forbidden field, because the DCA never sends one.

The minimizer is a **pure, deterministic, allowlist-first** screen — the AI-context counterpart of the
DCA-125 licensed-provider ``PERMITTED_ORG_FIELDS`` allowlist and the DCA-058 field-minimization screen,
reused here at the gateway seam rather than re-invented:

1. **Per-capability allowlist (:data:`CAPABILITY_CONTEXT_ALLOWLIST`).** Only a key on the capability's
   allowlist is a candidate to keep; anything else is DROPPED, regardless of its value. A capability with
   no registered allowlist is refused (:data:`REASON_UNKNOWN_CAPABILITY`) — fail closed, never a
   silent passthrough. Future capabilities stay disabled: a new capability ships its reviewed allowlist in
   its own AI-capability story.
2. **Always-forbidden keys (:data:`FORBIDDEN_CONTEXT_KEYS`).** Secret/credential/sentinel key names are
   dropped first, before the allowlist is even consulted, so a smuggled ``authorization`` /
   ``service_auth_token`` / ``send_sentinel`` can never survive even if a future allowlist mistakenly
   named it.
3. **Belt-and-braces value scrub.** Even an allowlisted key's VALUE is walked and scrubbed: the internal
   send sentinel (:data:`INTERNAL_SEND_SENTINEL`) and any registered secret literal (via the DCA-012
   :class:`~app.core.logging.Redactor`) are replaced with ``[redacted]`` — so a secret hidden inside an
   otherwise-legitimate evidence string cannot leak. Legitimate contact evidence (a role email) is NOT
   masked, because the allowlist already decided it is in-scope.
4. **DCA-058 class screen.** Each allowlisted field is run through :func:`~app.compliance.field_minimization.classify_field`;
   a prohibited personal class (children/sensitive, private mobile, unnecessary identifier) is dropped —
   so a wrongly-allowlisted or mislabelled field cannot launder prohibited PII to a model.
5. **Bounded size.** The serialized minimized context is bounded (:data:`DEFAULT_MAX_CONTEXT_CHARS`); an
   oversize context is refused (:data:`REASON_CONTEXT_TOO_LARGE`) rather than shipped — the gateway never
   sees an unbounded payload.

The result is inspectable WITHOUT re-leaking: :class:`MinimizedContext` carries the kept mapping plus the
NAMES (never the values) of what was dropped or redacted, mirroring the DCA-058 log-safety rule.

Refusals are raised as :class:`ContextMinimizationError` — a subclass of
:class:`~app.ai.contract.AiContractViolation`, so the facade and the DCA-130 bound invoker treat an
un-minimizable or oversize context as a fail-closed, non-retryable contract violation (no egress, no
spend). The minimizer performs NO I/O and NO model/paid work.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.ai.contract import AiContractViolation
from app.compliance.field_minimization import PROHIBITED_CLASSES, classify_field
from app.core.logging import REDACTED, SENSITIVE_KEYS, Redactor, get_redactor

# The internal send-authorization sentinel. The platform send pipeline stamps a sentinel token to mark a
# message as cleared to send; it is an internal control value that must NEVER reach a model. The DCA
# strips it defensively from any context value so a recording fake in front of the gateway never sees it.
INTERNAL_SEND_SENTINEL = "__dca_send_cleared__"

# Always-forbidden context keys — dropped before the allowlist is consulted, so a secret/credential or the
# internal send sentinel can never egress even under a future (mistaken) allowlist. Reuses the DCA-012
# central sensitive-key vocabulary and adds the send-control key names.
FORBIDDEN_CONTEXT_KEYS: frozenset[str] = SENSITIVE_KEYS | frozenset(
    {
        "send_sentinel",
        "sentinel",
        "send_token",
        "send_authorization",
        "send_cleared",
        "bearer",
        "cookie",
        "set-cookie",
        "x_service_auth",
        "x-service-auth",
        "x_request_id",
        "credential",
        "credentials",
        "private_key",
        "signing_secret",
    }
)

# Per-capability allowlist of the context field names that capability is permitted to receive. ONLY these
# keys survive minimization; everything else is dropped. The single entry mirrors the one DCA-128-allowed
# capability (``contact_research``) and the DCA-125 ``PERMITTED_ORG_FIELDS`` organization-level allowlist:
# authorized company-identity EVIDENCE plus PROVENANCE references, and NO personal contact channel. A
# later AI-capability story adds its own capability's reviewed allowlist here.
CAPABILITY_CONTEXT_ALLOWLIST: dict[str, frozenset[str]] = {
    "contact_research": frozenset(
        {
            # authorized organization-identity evidence (company facts only)
            "org_name",
            "legal_name",
            "org_identifier",
            "industry",
            "employee_count",
            "annual_revenue_band",
            "hq_state",
            "region",
            "website",
            # provenance references (point AT the evidence; never the raw personal payload)
            "org_ref",
            "source_key",
            "source_ref",
            "source_version",
            "evidence_ref",
            "provenance",
        }
    ),
    # DCA-134 — the trusted context for business-profile drafting is ONLY the business's own identity +
    # the refs/field-hints that steer the draft. The authorised website / Brand-KB evidence BODIES never
    # ride here: they go to the untrusted channel (DCA-132), so no raw evidence text is ever egressed as
    # trusted instruction context.
    "business_profile_draft": frozenset(
        {
            "businessName",
            "domain",
            # refs the model may cite (bodies are untrusted) + which aspects to draft
            "evidenceRefs",
            "draftFields",
        }
    ),
    # DCA-135 — the trusted context for bounded discovery planning is ONLY the business's own confirmed
    # India ICP targeting, the sources it is ALREADY approved+entitled to (each with the exact permitted
    # fields) and the hard task/query caps. No prospect contact data and no external content ever rides
    # here; the model proposes a plan within these declared bounds (and the gateway's own rule_check +
    # the DCA re-enforce them).
    "discovery_plan": frozenset(
        {
            "icpVersion",
            "industries",
            "sizeBands",
            "roles",
            "geography",
            "allowedSources",
            "maxTasks",
            "maxQueriesPerTask",
        }
    ),
    # DCA-136 — the trusted context for permitted unstructured-observation extraction is ONLY the
    # subject kind, the organization-field allowlist this approved+entitled source may supply, and the
    # evidence refs the model may cite. The fetched content BODIES never ride here: they go to the
    # untrusted channel (DCA-132), so no raw source content is ever egressed as trusted instruction
    # context. The model may produce no field outside permittedFields (gateway rule_check + DCA-133).
    "unstructured_observation_extraction": frozenset(
        {
            "subjectType",
            "permittedFields",
            "evidenceRefs",
        }
    ),
    # DCA-137 — the trusted context for ambiguous identity-match suggestions is ONLY the subject kind and
    # the authorized candidate descriptors (an opaque id + company-fact name/state per candidate). No
    # personal contact data and no external content ever rides here; the model proposes which candidate
    # pairs are ambiguous matches worth review, and the DCA re-vets every pair deterministically (DCA-067)
    # before storing a review-only suggestion — a suggestion can never execute a merge.
    "ambiguous_identity_match_suggestion": frozenset(
        {
            "subjectType",
            "candidates",
        }
    ),
}

# The serialized-size bound for a minimized context (characters of its canonical JSON). Matches the AI
# service's ``untrusted_max_chars`` default and the DCA-130 bound invoker's payload bound, so a context
# that would be refused downstream is refused here first, before any egress.
DEFAULT_MAX_CONTEXT_CHARS = 20_000

# Stable refusal codes (fail-closed, test-asserted — change deliberately).
REASON_UNKNOWN_CAPABILITY = "context_capability_not_minimizable"
REASON_CONTEXT_TOO_LARGE = "context_too_large"

# Stable drop/redact reason codes for the inspectable summary (names only, never values).
REASON_FORBIDDEN_KEY = "dropped_forbidden_key"
REASON_NOT_ALLOWLISTED = "dropped_not_allowlisted"
REASON_PROHIBITED_CLASS = "dropped_prohibited_class"
REASON_VALUE_REDACTED = "redacted_secret_or_sentinel"


class ContextMinimizationError(AiContractViolation):
    """A context could not be minimized for egress — refused BEFORE any gateway call (fail closed).

    A subclass of :class:`~app.ai.contract.AiContractViolation` so the facade / DCA-130 invoker treat an
    un-minimizable or oversize context as a non-retryable contract violation: no egress, no spend, and (in
    the bound invoker) the reserved hold is released. ``code`` is one of :data:`REASON_UNKNOWN_CAPABILITY`
    / :data:`REASON_CONTEXT_TOO_LARGE`; ``detail`` names the offending size when useful. It NEVER carries a
    raw context value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FieldDrop:
    """A dropped/redacted context field: its NAME and a stable REASON — never its raw value."""

    name: str
    reason_code: str


@dataclass(frozen=True)
class MinimizedContext:
    """The minimized context to egress, plus an inspectable, value-free account of what was removed.

    ``context`` is the mapping safe to place on the gateway envelope's ``inputContext``. ``dropped`` and
    ``redacted`` explain what was removed/scrubbed using field NAMES and stable reason codes only — never a
    raw value — so an audit/log line can be honest about the minimization without re-leaking the data it
    removed (the DCA-058 log-safety rule). ``size_chars`` is the bounded serialized size of ``context``.
    """

    context: dict[str, Any]
    dropped: tuple[FieldDrop, ...] = ()
    redacted: tuple[str, ...] = ()
    size_chars: int = 0

    @property
    def kept_fields(self) -> list[str]:
        return sorted(self.context)

    def dropped_names(self) -> list[str]:
        return [d.name for d in self.dropped]

    def log_summary(self) -> dict[str, Any]:
        """A log-safe summary: kept/dropped/redacted field NAMES and reasons only — no raw values ever."""
        return {
            "kept_fields": self.kept_fields,
            "dropped": [{"field": d.name, "reason": d.reason_code} for d in self.dropped],
            "redacted_fields": sorted(self.redacted),
            "size_chars": self.size_chars,
        }


def _scrub_value(value: Any, redactor: Redactor) -> tuple[Any, bool]:
    """Recursively replace the send sentinel and any registered secret literal inside ``value``.

    Returns ``(scrubbed, changed)``. Emails/phones are deliberately NOT masked (the allowlist already
    decided an allowlisted field is in-scope evidence, e.g. a role email); only secret/sentinel literals
    are removed. Pure and deterministic."""
    if isinstance(value, str):
        new = value
        if INTERNAL_SEND_SENTINEL and INTERNAL_SEND_SENTINEL in new:
            new = new.replace(INTERNAL_SEND_SENTINEL, REDACTED)
        new = redactor.redact_secrets(new)
        return new, new != value
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            scrubbed, item_changed = _scrub_value(item, redactor)
            out[key] = scrubbed
            changed = changed or item_changed
        return out, changed
    if isinstance(value, (list, tuple)):
        items = [_scrub_value(item, redactor) for item in value]
        return [item for item, _ in items], any(c for _, c in items)
    return value, False


class ContextMinimizer:
    """Pure, deterministic, allowlist-first minimizer applied to a capability's context before egress.

    Construct with an optional allowlist registry (defaults to the module registry), a redactor (defaults
    to the process-wide DCA-012 redactor so configured service-token secrets are scrubbed), and a size
    bound. :meth:`minimize` returns a :class:`MinimizedContext` or raises :class:`ContextMinimizationError`
    (fail closed) for an un-minimizable capability or an oversize payload. It is injected into the DCA-128
    facade and runs on every invocation — the one point every DCA model request passes through."""

    def __init__(
        self,
        *,
        allowlist: Mapping[str, frozenset[str]] | None = None,
        redactor: Redactor | None = None,
        max_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    ) -> None:
        self._allowlist = dict(allowlist if allowlist is not None else CAPABILITY_CONTEXT_ALLOWLIST)
        self._redactor = redactor if redactor is not None else get_redactor()
        self._max_chars = max_chars

    def allowed_fields(self, capability: str) -> frozenset[str] | None:
        """The permitted context fields for a capability, or ``None`` when the capability is unmapped."""
        return self._allowlist.get(capability)

    def minimize(self, capability: str, context: Mapping[str, Any] | None) -> MinimizedContext:
        """Minimize ``context`` for ``capability``: keep only authorized fields, scrub/strip the rest.

        Order: look up the capability allowlist (unmapped → refuse, fail closed) → per field: drop a
        forbidden key, then a non-allowlisted key, then a prohibited personal class; keep an allowlisted
        field after scrubbing secret/sentinel literals from its value → bound the serialized size (oversize
        → refuse). Pure/deterministic: no I/O, no mutation of the input, no model/paid work.
        """
        allow = self._allowlist.get(capability)
        if allow is None:
            raise ContextMinimizationError(
                REASON_UNKNOWN_CAPABILITY,
                f"capability '{capability}' has no registered context allowlist — nothing may be sent "
                "to the model for it (fail closed; a future capability ships its own reviewed allowlist)",
            )

        kept: dict[str, Any] = {}
        dropped: list[FieldDrop] = []
        redacted: list[str] = []

        for name, value in (context or {}).items():
            if name.lower() in FORBIDDEN_CONTEXT_KEYS:
                dropped.append(FieldDrop(name, REASON_FORBIDDEN_KEY))
                continue
            if name not in allow:
                dropped.append(FieldDrop(name, REASON_NOT_ALLOWLISTED))
                continue
            if classify_field(name, value) in PROHIBITED_CLASSES:
                dropped.append(FieldDrop(name, REASON_PROHIBITED_CLASS))
                continue
            scrubbed, changed = _scrub_value(value, self._redactor)
            kept[name] = scrubbed
            if changed:
                redacted.append(name)

        size = len(json.dumps(kept, default=str, sort_keys=True))
        if size > self._max_chars:
            raise ContextMinimizationError(
                REASON_CONTEXT_TOO_LARGE,
                f"minimized context is {size} characters, over the {self._max_chars} egress bound "
                "(refused before any gateway call — the model never sees an unbounded payload)",
                detail=str(size),
            )

        return MinimizedContext(
            context=kept,
            dropped=tuple(dropped),
            redacted=tuple(redacted),
            size_chars=size,
        )


# The process-wide default minimizer, built lazily so importing this module does not snapshot the redactor
# before ``configure_logging`` has registered the deployment's secrets.
_default_minimizer: ContextMinimizer | None = None


def default_context_minimizer() -> ContextMinimizer:
    """The shared default minimizer (over the module allowlist + the process-wide redactor)."""
    global _default_minimizer
    if _default_minimizer is None:
        _default_minimizer = ContextMinimizer()
    return _default_minimizer


def minimize_context(
    capability: str,
    context: Mapping[str, Any] | None,
    *,
    minimizer: ContextMinimizer | None = None,
) -> MinimizedContext:
    """Convenience: minimize a context with the default (or a supplied) minimizer."""
    return (minimizer or default_context_minimizer()).minimize(capability, context)


__all__ = [
    "INTERNAL_SEND_SENTINEL",
    "FORBIDDEN_CONTEXT_KEYS",
    "CAPABILITY_CONTEXT_ALLOWLIST",
    "DEFAULT_MAX_CONTEXT_CHARS",
    "REASON_UNKNOWN_CAPABILITY",
    "REASON_CONTEXT_TOO_LARGE",
    "REASON_FORBIDDEN_KEY",
    "REASON_NOT_ALLOWLISTED",
    "REASON_PROHIBITED_CLASS",
    "REASON_VALUE_REDACTED",
    "ContextMinimizationError",
    "FieldDrop",
    "MinimizedContext",
    "ContextMinimizer",
    "default_context_minimizer",
    "minimize_context",
]
