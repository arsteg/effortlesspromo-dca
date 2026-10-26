"""DCA-030 — inbound webhook signature verification.

Two layers, mirroring the DCA-029 envelope suite:

- **Always-on unit tests** pin the transport-auth contract: a validly-signed body is accepted; a
  single flipped byte, a stale/future timestamp, a missing/garbled header, and an unconfigured
  keyring are each rejected; a rotation OVERLAP accepts both the old and the new key; and no
  rejection message ever echoes the body, timestamp, signature or signing secret. Verification is a
  pure function (no session, no mutation), so these need no database.
- **"Rejected before mutation" on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) exercises the
  acceptance criterion end to end: a guarded receiver verifies the signature FIRST, then (only on
  success) parses the DCA-029 envelope and records a DCA-028 inbox receipt — so a tampered or stale
  event leaves no row. Transactions are not exercised on SQLite; this layer skips without a Postgres
  URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55475:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55475/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import InboxReceipt
from app.identity.tenant_context import TenantContext
from app.inbox import InboxSink
from app.integration import (
    CURRENT_ENVELOPE_VERSION,
    DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    MalformedSignatureError,
    SignatureMismatchError,
    StaleWebhookError,
    VerifiedWebhook,
    WebhookAuthError,
    WebhookKey,
    WebhookKeyring,
    WebhookNotConfiguredError,
    parse_envelope,
    sign_body,
    verify_webhook,
)

A = "00000000-0000-4000-8000-00000000000a"
SUBJECT = "00000000-0000-4000-9000-0000000a11ce"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# Synthetic, local-only signing secrets — a current key and a rotated-in replacement.
CURRENT_SECRET = "whsec_current_SENTINEL_0123456789"
NEW_SECRET = "whsec_new_SENTINEL_abcdefabcdef"
BODY = b'{"schema_version":1,"event_id":"evt-1"}'


def _keyring(*keys: WebhookKey) -> WebhookKeyring:
    return WebhookKeyring(list(keys) or [WebhookKey("current", CURRENT_SECRET)])


# --------------------------------------------------------------------------------------------------
# Always-on unit tests — the transport-auth contract
# --------------------------------------------------------------------------------------------------


def test_validly_signed_body_is_accepted():
    ring = _keyring()
    header = sign_body(ring, BODY, timestamp=NOW)
    result = verify_webhook(keyring=ring, header=header, body=BODY, now=NOW)
    assert isinstance(result, VerifiedWebhook)
    assert result.key_id == "current"
    assert result.timestamp == NOW


def test_tampered_body_fails_verification():
    ring = _keyring()
    header = sign_body(ring, BODY, timestamp=NOW)
    tampered = BODY.replace(b"evt-1", b"evt-2")  # one field changed in transit
    assert tampered != BODY
    with pytest.raises(SignatureMismatchError):
        verify_webhook(keyring=ring, header=header, body=tampered, now=NOW)


def test_single_flipped_byte_fails_verification():
    ring = _keyring()
    header = sign_body(ring, BODY, timestamp=NOW)
    flipped = bytearray(BODY)
    flipped[0] ^= 0x01  # flip one bit of one byte
    with pytest.raises(SignatureMismatchError):
        verify_webhook(keyring=ring, header=header, body=bytes(flipped), now=NOW)


def test_wrong_key_fails_verification():
    signed_with = _keyring(WebhookKey("current", CURRENT_SECRET))
    header = sign_body(signed_with, BODY, timestamp=NOW)
    verified_with = _keyring(WebhookKey("current", "a-totally-different-secret"))
    with pytest.raises(SignatureMismatchError):
        verify_webhook(keyring=verified_with, header=header, body=BODY, now=NOW)


def test_stale_timestamp_fails_even_when_validly_signed():
    ring = _keyring()
    old = NOW - timedelta(seconds=DEFAULT_TIMESTAMP_TOLERANCE_SECONDS + 1)
    header = sign_body(ring, BODY, timestamp=old)  # correctly signed, but for an old timestamp
    with pytest.raises(StaleWebhookError):
        verify_webhook(keyring=ring, header=header, body=BODY, now=NOW)


def test_far_future_timestamp_fails():
    ring = _keyring()
    future = NOW + timedelta(seconds=DEFAULT_TIMESTAMP_TOLERANCE_SECONDS + 5)
    header = sign_body(ring, BODY, timestamp=future)
    with pytest.raises(StaleWebhookError):
        verify_webhook(keyring=ring, header=header, body=BODY, now=NOW)


def test_timestamp_at_tolerance_boundary_is_accepted():
    ring = _keyring()
    edge = NOW - timedelta(seconds=DEFAULT_TIMESTAMP_TOLERANCE_SECONDS)
    header = sign_body(ring, BODY, timestamp=edge)
    result = verify_webhook(keyring=ring, header=header, body=BODY, now=NOW)
    assert result.timestamp == edge


def test_staleness_is_checked_before_signature():
    # A stale timestamp AND a bad signature => the staleness guard fires first (replay guard precedes
    # the signature check), so an expired event is turned away regardless of how it was signed.
    ring = _keyring()
    old = NOW - timedelta(hours=1)
    header = f"t={int(old.timestamp())},v1=deadbeef"  # stale and a bogus signature
    with pytest.raises(StaleWebhookError):
        verify_webhook(keyring=ring, header=header, body=BODY, now=NOW)


@pytest.mark.parametrize(
    "header",
    [
        "",  # empty
        "   ",  # whitespace only
        "v1=abc",  # no timestamp
        "t=not-an-int,v1=abc",  # non-integer timestamp
        f"t={int(NOW.timestamp())}",  # no signature
        f"t={int(NOW.timestamp())},k=current",  # only a key hint, no signature
    ],
)
def test_malformed_header_is_rejected(header):
    ring = _keyring()
    with pytest.raises(MalformedSignatureError):
        verify_webhook(keyring=ring, header=header, body=BODY, now=NOW)


def test_missing_header_is_rejected():
    ring = _keyring()
    with pytest.raises(MalformedSignatureError):
        verify_webhook(keyring=ring, header=None, body=BODY, now=NOW)


def test_oversized_header_is_rejected_without_echo():
    ring = _keyring()
    junk = "x" * 5000
    with pytest.raises(MalformedSignatureError) as exc:
        verify_webhook(keyring=ring, header=f"t=1,v1={junk}", body=BODY, now=NOW)
    assert junk not in str(exc.value)


def test_too_many_signatures_is_rejected():
    ring = _keyring()
    many = ",".join(f"v1=sig{i}" for i in range(32))
    with pytest.raises(MalformedSignatureError):
        verify_webhook(keyring=ring, header=f"t={int(NOW.timestamp())},{many}", body=BODY, now=NOW)


def test_unconfigured_keyring_fails_closed():
    # No keyring (verification disabled) => every webhook is refused, never served open.
    header = sign_body(_keyring(), BODY, timestamp=NOW)
    with pytest.raises(WebhookNotConfiguredError):
        verify_webhook(keyring=None, header=header, body=BODY, now=NOW)


# --- key rotation: both keys verify during the overlap -------------------------------------------


def test_rotation_overlap_accepts_both_old_and_new_keys():
    old_key = WebhookKey("key-2025", CURRENT_SECRET)
    new_key = WebhookKey("key-2026", NEW_SECRET)

    # During the overlap the ring holds BOTH keys; the new key is current (signs new traffic).
    overlap = WebhookKeyring([new_key, old_key])

    # A producer still on the OLD key signs with it...
    old_header = sign_body(WebhookKeyring([old_key]), BODY, timestamp=NOW)
    verified_old = verify_webhook(keyring=overlap, header=old_header, body=BODY, now=NOW)
    assert verified_old.key_id == "key-2025"  # matched the old key

    # ...and a producer on the NEW key signs with it. Both verify against the overlap ring.
    new_header = sign_body(WebhookKeyring([new_key]), BODY, timestamp=NOW)
    verified_new = verify_webhook(keyring=overlap, header=new_header, body=BODY, now=NOW)
    assert verified_new.key_id == "key-2026"


def test_after_overlap_closes_the_retired_key_is_rejected():
    old_key = WebhookKey("key-2025", CURRENT_SECRET)
    new_key = WebhookKey("key-2026", NEW_SECRET)
    old_header = sign_body(WebhookKeyring([old_key]), BODY, timestamp=NOW)

    # Overlap removed: the ring holds only the new key, so the old key's signature no longer verifies.
    only_new = WebhookKeyring([new_key])
    with pytest.raises(SignatureMismatchError):
        verify_webhook(keyring=only_new, header=old_header, body=BODY, now=NOW)


def test_current_key_is_the_first_key():
    old_key = WebhookKey("key-2025", CURRENT_SECRET)
    new_key = WebhookKey("key-2026", NEW_SECRET)
    ring = WebhookKeyring([new_key, old_key])
    assert ring.current == new_key


# --- no-echo discipline ---------------------------------------------------------------------------


def test_rejection_never_echoes_body_timestamp_signature_or_secret():
    ring = _keyring()
    header = sign_body(ring, BODY, timestamp=NOW - timedelta(hours=1))  # stale
    cases: list[WebhookAuthError] = []
    for call in (
        lambda: verify_webhook(keyring=ring, header=header, body=BODY, now=NOW),  # stale
        lambda: verify_webhook(  # tampered
            keyring=ring, header=sign_body(ring, BODY, timestamp=NOW), body=b"tampered", now=NOW
        ),
        lambda: verify_webhook(keyring=ring, header="garbage", body=BODY, now=NOW),  # malformed
    ):
        with pytest.raises(WebhookAuthError) as exc:
            call()
        cases.append(exc.value)

    for err in cases:
        message = str(err)
        assert CURRENT_SECRET not in message
        assert "SENTINEL" not in message
        assert header.split("v1=")[-1] not in message  # the hex signature is never echoed
        assert BODY.decode() not in message


# --- config builder (from_settings) ---------------------------------------------------------------


def _settings(**overrides) -> Settings:
    base = {
        "environment": "testing",
        "database_url": "sqlite+pysqlite:///:memory:",
        "service_auth_token": "test-token",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_from_settings_disabled_by_default_returns_none():
    assert WebhookKeyring.from_settings(_settings()) is None


def test_from_settings_enabled_but_no_secret_returns_none():
    # Fail-closed: enabled without a secret does not build an (empty) keyring.
    ring = WebhookKeyring.from_settings(_settings(webhook_verification_enabled=True))
    assert ring is None


def test_from_settings_builds_single_key_ring():
    ring = WebhookKeyring.from_settings(
        _settings(
            webhook_verification_enabled=True,
            webhook_signing_secret=CURRENT_SECRET,
            webhook_current_key_id="key-a",
        )
    )
    assert ring is not None
    assert [k.key_id for k in ring.keys] == ["key-a"]
    # round-trips: a body signed by the built ring verifies against it.
    header = sign_body(ring, BODY, timestamp=NOW)
    assert verify_webhook(keyring=ring, header=header, body=BODY, now=NOW).key_id == "key-a"


def test_from_settings_builds_rotation_overlap_ring():
    ring = WebhookKeyring.from_settings(
        _settings(
            webhook_verification_enabled=True,
            webhook_signing_secret=NEW_SECRET,
            webhook_current_key_id="key-new",
            webhook_previous_signing_secret=CURRENT_SECRET,
            webhook_previous_key_id="key-old",
        )
    )
    assert ring is not None
    assert [k.key_id for k in ring.keys] == ["key-new", "key-old"]
    # Both an old-key and a new-key signature verify during the overlap.
    old_header = sign_body(
        WebhookKeyring([WebhookKey("key-old", CURRENT_SECRET)]), BODY, timestamp=NOW
    )
    assert verify_webhook(keyring=ring, header=old_header, body=BODY, now=NOW).key_id == "key-old"


def test_keyring_rejects_empty_and_duplicate_ids():
    with pytest.raises(ValueError):
        WebhookKeyring([])
    with pytest.raises(ValueError):
        WebhookKeyring([WebhookKey("dup", "s1"), WebhookKey("dup", "s2")])


def test_webhook_key_requires_id_and_secret():
    with pytest.raises(ValueError):
        WebhookKey("", "secret")
    with pytest.raises(ValueError):
        WebhookKey("id", "")


# --------------------------------------------------------------------------------------------------
# "Rejected before mutation" on a disposable PostgreSQL — the acceptance criterion
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_webhook_test"


def _ctx(business_id: str) -> TenantContext:
    return TenantContext(
        subject=SUBJECT, business_id=business_id, roles=("owner",), membership_version="v1"
    )


@pytest.fixture
def pg_factory():
    """A sync session factory over a fresh schema on the disposable Postgres (superuser connection)."""
    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
    admin.dispose()

    engine = create_engine(PG_URL)

    @event.listens_for(engine, "connect")
    def _pin_search_path(dbapi_conn, _record):  # pragma: no cover - trivial connect hook
        cur = dbapi_conn.cursor()
        cur.execute(f'SET search_path TO "{SCHEMA}"')
        cur.close()

    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()
        admin = create_engine(PG_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        admin.dispose()


def _event_body(event_id: str) -> bytes:
    import json

    return json.dumps(
        {
            "schema_version": CURRENT_ENVELOPE_VERSION,
            "event_id": event_id,
            "business_id": A,
            "event_type": "candidate.accepted",
            "aggregate_version": 1,
            "occurred_at": NOW.isoformat(),
            "payload": {"candidateId": "cand-9"},
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _guarded_receive(factory, ring, header: str | None, body: bytes, *, now: datetime) -> bool:
    """A receiver that AUTHENTICATES the transport before it parses or mutates: verify the webhook
    signature first, then parse the DCA-029 envelope, then record a DCA-028 inbox receipt. A failed
    signature/timestamp raises before any session is opened, so nothing is written."""
    verify_webhook(keyring=ring, header=header, body=body, now=now)  # raises BEFORE any session
    import json

    env = parse_envelope(json.loads(body))
    with factory() as session:
        InboxSink.for_context(session, _ctx(env.business_id)).register(
            event_source="effortlesspromo-api",
            event_id=env.event_id,
            event_type=env.event_type,
            payload=env.payload,
        )
        session.commit()
    return True


@requires_pg
def test_valid_webhook_is_recorded_but_rejected_ones_leave_no_row(pg_factory):
    factory = pg_factory
    ring = _keyring()
    body = _event_body("evt-ok")
    header = sign_body(ring, body, timestamp=NOW)

    # A validly-signed, fresh event is authenticated then recorded.
    assert _guarded_receive(factory, ring, header, body, now=NOW) is True

    # A tampered body is rejected before the receiver mutates (signature over the original body).
    tampered = _event_body("evt-tampered")
    with pytest.raises(SignatureMismatchError):
        _guarded_receive(factory, ring, header, tampered, now=NOW)

    # A stale (replayed) event is rejected before mutation, even though it is correctly signed.
    stale_body = _event_body("evt-stale")
    stale_header = sign_body(ring, stale_body, timestamp=NOW - timedelta(hours=1))
    with pytest.raises(StaleWebhookError):
        _guarded_receive(factory, ring, stale_header, stale_body, now=NOW)

    # Exactly one receipt exists — only the valid event.
    with factory() as check:
        rows = check.scalars(select(InboxReceipt)).all()
        assert len(rows) == 1
        assert rows[0].event_id == "evt-ok"


@requires_pg
def test_rotation_overlap_records_old_and_new_key_events(pg_factory):
    factory = pg_factory
    old_key = WebhookKey("key-old", CURRENT_SECRET)
    new_key = WebhookKey("key-new", NEW_SECRET)
    overlap = WebhookKeyring([new_key, old_key])

    old_body = _event_body("evt-old-key")
    old_header = sign_body(WebhookKeyring([old_key]), old_body, timestamp=NOW)
    new_body = _event_body("evt-new-key")
    new_header = sign_body(WebhookKeyring([new_key]), new_body, timestamp=NOW)

    assert _guarded_receive(factory, overlap, old_header, old_body, now=NOW) is True
    assert _guarded_receive(factory, overlap, new_header, new_body, now=NOW) is True

    with factory() as check:
        ids = {r.event_id for r in check.scalars(select(InboxReceipt)).all()}
        assert ids == {"evt-old-key", "evt-new-key"}
