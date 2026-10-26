"""DCA-026 — transactional audit.

A single sanctioned writer (:class:`~app.audit.sink.AuditSink`) that records tenant-scoped mutations
(actor, scope, action, reason, opaque references) INSIDE the caller's transaction, so an audit entry
can never be committed apart from the business change it describes — nor survive its rollback.
"""

from app.audit.sink import (
    ActorType,
    AuditActor,
    AuditPayloadError,
    AuditSink,
    assert_opaque_references,
    assert_opaque_text,
)

__all__ = [
    "ActorType",
    "AuditActor",
    "AuditPayloadError",
    "AuditSink",
    "assert_opaque_references",
    "assert_opaque_text",
]
