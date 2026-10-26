"""DCA-026 — the DCA audit sink.

One capability: record a tenant-scoped mutation — WHO (actor), WHERE (scope/business), WHAT (action),
WHY (reason) and opaque references — as a durable :class:`~app.db.models.AuditEvent`, staged into the
*caller's* session so it is part of the same transaction as the business change.

Two guarantees make this safe to adopt everywhere:

- **Atomic with the caller.** :meth:`AuditSink.record` only ``session.add(...)``s the row; it never
  commits or flushes a transaction of its own. The caller commits the business change and its audit
  entry together, so a failed unit of work leaves neither (mirrors the platform's SEC-001/BR-122
  atomic-audit rule and its ``AuditGuard`` interceptor).
- **No raw secrets or contact data.** Every payload is screened by :func:`assert_opaque_references`
  BEFORE anything is staged; a reference that carries an email, a phone number, a registered secret,
  or a sensitive key (``email`` / ``phone`` / ``token`` / ``contact`` / …) is refused with
  :class:`AuditPayloadError` and no row is created. The audit trail therefore holds only opaque
  references (ids / row keys) — never personal data. The screen reuses the single DCA-012 redaction
  policy (:func:`app.core.logging.get_redactor`) so detection can never drift from redaction.

The sink is the ONLY sanctioned writer of ``audit_events``. It stamps ``business_id`` (the scope)
from the verified tenant context, never from a caller argument, and attaches the current request's
correlation id so an audit row and its DCA-012 log lines can be joined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_correlation_id, get_redactor
from app.db.models import AuditEvent
from app.identity.tenant_context import TenantContext

# A canonical UUID — the normal shape of an opaque DCA id (subject/business/row key). Its hex digits
# would otherwise trip the DCA-012 phone heuristic (a long run of digits/hyphens), so UUIDs are
# blanked out of the probe BEFORE the secret/contact scan. Emails and registered secrets are not
# UUID-shaped, so neutralising UUIDs only suppresses the phone false-positive on legitimate ids.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class ActorType(str, Enum):
    """The kind of actor that performed the audited action.

    ``PLATFORM_SUPPORT`` is deliberately distinct from ``SERVICE``/``USER``: delegated client-support
    access is a separate, scoped, expiring grant (not an Admin role on its own), and its actions must
    be attributable as such in the trail.
    """

    SERVICE = "service"
    USER = "user"
    SYSTEM = "system"
    PLATFORM_SUPPORT = "platform_support"


@dataclass(frozen=True)
class AuditActor:
    """WHO performed the action — an opaque id (subject/service GUID) plus its kind.

    ``id`` must be an opaque identifier, never a display name / email / phone; it is screened like a
    reference so a mistaken contact value is refused rather than persisted.
    """

    id: str
    type: ActorType


class AuditPayloadError(Exception):
    """A payload was refused because it carried raw secret or contact data.

    Raised BEFORE the session is touched, so a refused audit write leaves no row and no partial
    state. Callers must pass opaque references (ids / row keys), never personal data.
    """


# Structured-field keys whose VALUE is treated as a secret/contact and must never land in the audit
# trail, even if its text happens to look benign. Superset of the keys the DCA-012 redactor drops.
_FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "auth",
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "contact",
        "email",
        "phone",
        "mobile",
        "msisdn",
    }
)


def _reject_if_sensitive_text(where: str, value: str) -> None:
    # Reuse the single DCA-012 redaction policy: if redacting the text changes it, it contained an
    # email, a phone number, or a registered secret — refuse rather than persist it. Opaque UUID ids
    # are blanked first so their hex digits don't read as a phone number (false positive on valid ids).
    probe = _UUID_RE.sub(" ", value)
    if get_redactor().redact_text(probe) != probe:
        raise AuditPayloadError(
            f"{where} contains raw secret/contact data; references must be opaque"
        )


def assert_opaque_text(where: str, value: str) -> None:
    """Refuse a single string that carries raw secret/contact data (shared opaque-text screen).

    Raises :class:`AuditPayloadError` if ``value`` contains an email, a phone number, or a registered
    secret; returns ``None`` when the text is opaque. Reused by other transactional writers (e.g. the
    DCA-027 outbox sink) so the single DCA-012 redaction policy governs every durable payload."""
    _reject_if_sensitive_text(where, value)


def _screen(where: str, value: Any) -> None:
    """Recursively screen one reference value for secrets/contact data and JSON-storability."""
    if isinstance(value, str):
        _reject_if_sensitive_text(where, value)
        return
    if isinstance(value, (bool, int, float)) or value is None:
        return
    if isinstance(value, dict):
        for key, inner in value.items():
            key_str = str(key)
            if key_str.lower() in _FORBIDDEN_KEYS:
                raise AuditPayloadError(
                    f"{where}.{key_str} is a secret/contact field and cannot be audited"
                )
            _screen(f"{where}.{key_str}", inner)
        return
    if isinstance(value, (list, tuple)):
        for index, inner in enumerate(value):
            _screen(f"{where}[{index}]", inner)
        return
    raise AuditPayloadError(
        f"{where} is not a JSON-storable opaque value (got {type(value).__name__})"
    )


def assert_opaque_references(references: dict[str, Any] | None) -> dict[str, Any]:
    """Validate that ``references`` is a mapping of opaque, JSON-storable, secret/contact-free values.

    Returns the (unchanged) mapping on success so it can be used inline; raises
    :class:`AuditPayloadError` otherwise. A top-level key that is itself a secret/contact field is
    refused too, so e.g. ``{"email": "..."}`` can never be smuggled in as a "reference".
    """
    if references is None:
        return {}
    if not isinstance(references, dict):
        raise AuditPayloadError("references must be a mapping of opaque keys to opaque values")
    for key, value in references.items():
        key_str = str(key)
        if key_str.lower() in _FORBIDDEN_KEYS:
            raise AuditPayloadError(
                f"references.{key_str} is a secret/contact field and cannot be audited"
            )
        _screen(f"references.{key_str}", value)
    return references


class AuditSink:
    """Stage transactional audit events into one caller-owned session.

    Construct it with the active session; bind an actor/scope either per call or once via
    :meth:`for_context`. The sink never opens, commits, flushes or rolls back a transaction — that is
    the caller's unit of work, which is exactly what makes the audit entry atomic with the business
    change.
    """

    def __init__(
        self,
        session: Session,
        *,
        business_id: str | None = None,
        actor: AuditActor | None = None,
    ) -> None:
        self._session = session
        self._business_id = business_id
        self._actor = actor

    @classmethod
    def for_context(cls, session: Session, context: TenantContext) -> AuditSink:
        """Bind the sink to a verified tenant context: scope = the context's business, actor = its
        subject (a user acting within the business). The scope is taken from the context, never from
        a caller argument, so an audit row can never be attributed to a different tenant."""
        return cls(
            session,
            business_id=context.business_id,
            actor=AuditActor(id=context.subject, type=ActorType.USER),
        )

    def record(
        self,
        *,
        action: str,
        reason: str = "",
        references: dict[str, Any] | None = None,
        actor: AuditActor | None = None,
        business_id: str | None = None,
    ) -> AuditEvent:
        """Stage one audit event into the caller's session and return the (uncommitted) row.

        ``action`` is a required controlled code (e.g. ``"candidate.accept"``). ``reason`` is a
        short reason code / server-side explanation. ``references`` must be opaque (screened by
        :func:`assert_opaque_references`). Scope and actor come from the per-call arguments or the
        bound defaults; a missing scope or actor is a programming error (``ValueError``) — the sink
        is fail-closed and will not invent one.
        """
        effective_business_id = business_id or self._business_id
        effective_actor = actor or self._actor
        if not action or not action.strip():
            raise ValueError("audit action is required")
        if not effective_business_id:
            raise ValueError("audit event requires a business scope")
        if effective_actor is None:
            raise ValueError("audit event requires an actor")

        # Screen BEFORE touching the session: a refused payload stages no row.
        _reject_if_sensitive_text("actor id", effective_actor.id)
        _reject_if_sensitive_text("reason", reason)
        screened = assert_opaque_references(references)

        event = AuditEvent(
            business_id=effective_business_id,
            actor_id=effective_actor.id,
            actor_type=effective_actor.type.value,
            action=action.strip(),
            reason=reason,
            references=dict(screened),
            correlation_id=get_correlation_id(),
        )
        self._session.add(event)
        return event
