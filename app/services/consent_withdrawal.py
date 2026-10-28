"""DCA-076 — the withdraw-one-permission-scope command.

:class:`ConsentWithdrawalService` is the one write path for WITHDRAWING a single permission scope —
``(brand_key, contact_point_id, channel, purpose)`` — for one business. It is the sibling of the
DCA-074 grant writer (:mod:`app.services.consent_receipt`) and the DCA-075 state projector
(:mod:`app.consent.permission_state`): recording a grant, deriving the current state, and withdrawing a
scope are three deliberately independent capabilities, so each can be reviewed and accepted on its own.

What the command does, atomically, in the caller's unit of work:

1. **Authorize** the withdrawal one of two ways and NO other:
   - :class:`AuthenticatedWithdrawal` — a business user whose verified membership roles grant
     :attr:`~app.identity.permissions.Scope.CONSENT_WITHDRAW`. Authority is checked FIRST, so an
     unauthorized caller (a read-only viewer, a non-member, a platform operator holding only global
     rights) is refused BEFORE anything is read or written.
   - :class:`PublicTokenWithdrawal` — the subject's own one-click unsubscribe: a valid signed
     :mod:`app.consent.withdrawal_token`. The token BINDS the exact scope + business, so it can only
     ever restrict the scope it was minted for; its business id is taken from the token, never from a
     caller argument. When no signing secret is configured the public path is disabled (fail-closed).
2. **Append a WITHDRAW receipt** via :meth:`~app.services.consent_receipt.ConsentReceiptStore.record_withdrawal`
   — append-only, so the prior grant is preserved and the scope immediately projects WITHDRAWN.
3. **Emit a transactional outbox event** (:data:`OUTBOX_EVENT_SCOPE_WITHDRAWN`) carrying only opaque
   references (ids / brand / channel / purpose), so a downstream relay can propagate the restriction
   later. The event and the receipt commit together — a failed unit of work leaves neither.

The load-bearing acceptance guarantee: a withdrawal is confined to ONE business (the authenticated
context's, or the token's) and acts only on a contact point that exists in THAT business. So an email
withdrawal in Business A never touches Business B's independent permission for the same email value
(B owns its own contact point + receipts), and the restriction takes effect immediately — the next
:func:`~app.consent.derive_permission_state` over the timeline reports WITHDRAWN. Acting on that
restriction (suppressing a send) stays a disabled future capability behind the one send pipeline; this
command only records the opt-out and the intent to propagate it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor
from app.consent.permission_state import (
    PermissionProjection,
    derive_permission_state,
    events_from_receipts,
)
from app.consent.withdrawal_token import (
    WithdrawalTokenClaims,
    WithdrawalTokenError,
    verify_withdrawal_token,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.outbox.sink import OutboxSink
from app.services.consent_receipt import ConsentReceiptError, ConsentReceiptStore

# The outbox event emitted when a scope is withdrawn — a propagation INTENT, relayed by a later story.
OUTBOX_EVENT_SCOPE_WITHDRAWN = "consent.scope.withdrawn"

# How the withdrawal was authorized, recorded opaquely on the receipt audit + outbox payload.
VIA_AUTHENTICATED = "authenticated"
VIA_PUBLIC_TOKEN = "public_token"

# Stable refusal codes for the authorization gate (the scope/receipt codes come from the store).
REASON_NOT_AUTHORIZED = "consent_withdraw_not_authorized"
REASON_PUBLIC_WITHDRAWAL_DISABLED = "public_withdrawal_disabled"
REASON_TOKEN_SCOPE_MISMATCH = "withdrawal_token_scope_mismatch"


class ConsentWithdrawalError(Exception):
    """A withdrawal was refused with a stable ``code``. Raised before the session is mutated, so a
    refusal stages no receipt and no outbox event. ``detail`` names the offending value when useful
    (never a raw contact value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ConsentWithdrawalAuthorizationError(ConsentWithdrawalError):
    """The caller is not authorized to withdraw the scope (bad roles, invalid/forged token, disabled
    public path, or a token for a different scope). A distinct type so an HTTP boundary (a later story)
    can map it to a 401/403 while scope/validation errors map to 404/422."""


@dataclass(frozen=True)
class ScopeRef:
    """The one permission scope to withdraw — the DCA-074 scoping dimensions."""

    brand_key: str
    contact_point_id: str
    channel: str
    purpose: str


@dataclass(frozen=True)
class AuthenticatedWithdrawal:
    """A withdrawal exercised by a business user. ``business_id`` is the VERIFIED tenant scope (from the
    membership-checked context, never a bare header), ``roles`` the caller's business-plane membership
    roles, ``actor_id`` an opaque subject id recorded on the receipt + audit. The scope to withdraw is
    supplied separately (:class:`ScopeRef`) since an authenticated user names it explicitly."""

    business_id: str
    actor_id: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"


@dataclass(frozen=True)
class PublicTokenWithdrawal:
    """A withdrawal exercised by the subject via a one-click unsubscribe token. The token both
    AUTHORIZES and NAMES the scope — the service never accepts a caller-supplied scope here, so the
    token can only restrict exactly what it was minted for. ``actor_id`` defaults to an opaque
    self-service marker (the subject is not an authenticated principal)."""

    token: str
    actor_id: str = "public-withdrawal-self-service"


class ConsentWithdrawalService:
    """Withdraw one permission scope, authorizing via an authenticated actor or a public token.

    Construct with the active session and the configured public-token signing secret (empty / ``None``
    ⇒ the public path is disabled, fail-closed; the authenticated path is unaffected). The service
    never opens, commits or rolls back — the caller owns the unit of work, which is what makes the
    receipt and its outbox intent atomic.
    """

    def __init__(self, session: Session, *, token_secret: str | None = None) -> None:
        self._session = session
        self._token_secret = (token_secret or "").strip()

    def withdraw_authenticated(
        self,
        auth: AuthenticatedWithdrawal,
        scope: ScopeRef,
        *,
        now: datetime | None = None,
    ) -> PermissionProjection:
        """Withdraw ``scope`` for an authenticated business user. Refuses with
        :class:`ConsentWithdrawalAuthorizationError` (``REASON_NOT_AUTHORIZED``) BEFORE touching state
        when the caller's roles lack :attr:`~app.identity.permissions.Scope.CONSENT_WITHDRAW`."""
        if Scope.CONSENT_WITHDRAW not in scopes_for_business_roles(auth.roles):
            raise ConsentWithdrawalAuthorizationError(
                REASON_NOT_AUTHORIZED,
                f"withdrawing a permission requires a business user holding "
                f"'{Scope.CONSENT_WITHDRAW.value}'; a read-only viewer, a non-member or a platform "
                "operator cannot perform it",
            )
        actor = AuditActor(id=auth.actor_id, type=ActorType(auth.actor_type))
        return self._apply(
            business_id=auth.business_id,
            scope=scope,
            actor=actor,
            withdrawn_via=VIA_AUTHENTICATED,
            now=now,
        )

    def withdraw_with_public_token(
        self,
        request: PublicTokenWithdrawal,
        *,
        now: datetime | None = None,
    ) -> PermissionProjection:
        """Withdraw the scope a valid public token authorizes (one-click unsubscribe).

        The token is verified against the configured secret; when none is set the public path is
        disabled (``REASON_PUBLIC_WITHDRAWAL_DISABLED``). An invalid / expired / forged token is
        refused with :class:`ConsentWithdrawalAuthorizationError`. The business id and scope come from
        the token's signed claims — never from a caller argument — so the token can only restrict the
        exact scope it was minted for.
        """
        if not self._token_secret:
            raise ConsentWithdrawalAuthorizationError(
                REASON_PUBLIC_WITHDRAWAL_DISABLED,
                "public (token) withdrawal is disabled: no signing secret is configured",
            )
        try:
            claims: WithdrawalTokenClaims = verify_withdrawal_token(
                self._token_secret, request.token, now=now
            )
        except WithdrawalTokenError as exc:
            raise ConsentWithdrawalAuthorizationError(exc.code, str(exc)) from exc

        scope = ScopeRef(
            brand_key=claims.brand_key,
            contact_point_id=claims.contact_point_id,
            channel=claims.channel,
            purpose=claims.purpose,
        )
        actor = AuditActor(id=request.actor_id, type=ActorType.USER)
        return self._apply(
            business_id=claims.business_id,
            scope=scope,
            actor=actor,
            withdrawn_via=VIA_PUBLIC_TOKEN,
            now=now,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _apply(
        self,
        *,
        business_id: str,
        scope: ScopeRef,
        actor: AuditActor,
        withdrawn_via: str,
        now: datetime | None,
    ) -> PermissionProjection:
        """Stage the WITHDRAW receipt + its outbox intent for ONE business and return the resulting
        in-force projection (which is now WITHDRAWN). All scope validation is the store's — a contact
        point that does not exist in this business, or a channel mismatch, is refused and stages
        nothing (the relevant denial). Everything here is bound to ``business_id``, so a withdrawal can
        never reach another tenant's permission."""
        moment = now or datetime.now(UTC)
        store = ConsentReceiptStore(self._session, business_id)

        # Append-only WITHDRAW receipt (scope validated against THIS business's contact point).
        try:
            receipt = store.record_withdrawal(
                brand_key=scope.brand_key,
                contact_point_id=scope.contact_point_id,
                channel=scope.channel,
                purpose=scope.purpose,
                actor=actor,
                collected_at=moment,
                withdrawn_via=withdrawn_via,
                now=moment,
            )
        except ConsentReceiptError as exc:
            # Re-raise as the command's error type, preserving the stable store code (the denial).
            raise ConsentWithdrawalError(exc.code, str(exc), detail=exc.detail) from exc

        # Transactional outbox intent — opaque references only (no raw contact value). Staged into the
        # same session so it commits atomically with the receipt; a later relay propagates it.
        OutboxSink(self._session, business_id=business_id).enqueue(
            event_type=OUTBOX_EVENT_SCOPE_WITHDRAWN,
            dedup_key=f"withdraw:{receipt.id}",
            payload={
                "receiptId": receipt.id,
                "brandKey": receipt.brand_key,
                "subjectType": receipt.subject_type,
                "subjectId": receipt.subject_id,
                "contactPointId": receipt.contact_point_id,
                "channel": receipt.channel,
                "purpose": receipt.purpose,
                "withdrawnVia": withdrawn_via,
            },
        )

        # The resulting in-force state for this scope — proof the restriction is immediate. The store
        # hasn't flushed yet, so include the just-staged receipt alongside the committed timeline.
        timeline = list(store.for_contact_point(scope.contact_point_id))
        if receipt not in timeline:
            timeline.append(receipt)
        same_scope = [
            r
            for r in timeline
            if r.channel == receipt.channel
            and r.purpose == receipt.purpose
            and r.brand_key == receipt.brand_key
        ]
        return derive_permission_state(events_from_receipts(same_scope), now=moment)
