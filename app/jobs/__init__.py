"""DCA job-request persistence (DCA-099) + the typed handler registry (DCA-100) + safe claim (DCA-101).

- The durable job *store* (DCA-099) persists the intent to do background work so it survives a restart
  and is deduplicated by logical intent.
- The typed handler *registry* (DCA-100) binds a job's ``(job_type, schema_version)`` to a typed Python
  handler and validates its payload BEFORE anything could run; unknown job types fail safely.
- The *claim store* (DCA-101) lets workers lease a queued job in a short ``FOR UPDATE SKIP LOCKED``
  transaction with a lease + fencing token, so two workers never hold the same current claim and a
  slow handler never pins a database lock.

None of them executes a job: there is no job runner here (a disabled future capability) — claiming
leases work, it does not run it. Public surface is the store + its enqueue contract, the registry + its
handler/validation contract, and the claim store + its lease/fencing contract.
"""

from __future__ import annotations

from app.jobs.handlers import (
    DuplicateHandlerError,
    HandlerDescriptor,
    JobHandler,
    JobHandlerError,
    JobHandlerRegistry,
    JobPayloadValidationError,
    PayloadFieldError,
    UnknownJobTypeError,
    UnsupportedSchemaVersionError,
    ValidatedJob,
    build_default_registry,
)
from app.jobs.claim import (
    DEFAULT_LEASE_SECONDS,
    MAX_LEASE_LIFETIME_SECONDS,
    MAX_LEASE_SECONDS,
    JobClaim,
    JobClaimStore,
)
from app.jobs.store import (
    JobEnqueueRequest,
    JobRequestConflict,
    JobRequestStore,
    PayloadContainsPiiError,
    job_payload_digest,
)

__all__ = [
    # DCA-099 — durable job-request store
    "JobEnqueueRequest",
    "JobRequestConflict",
    "JobRequestStore",
    "PayloadContainsPiiError",
    "job_payload_digest",
    # DCA-101 — safe cross-worker claim (lease + fencing)
    "DEFAULT_LEASE_SECONDS",
    "JobClaim",
    "JobClaimStore",
    # DCA-102 — bounded lease renewal
    "MAX_LEASE_SECONDS",
    "MAX_LEASE_LIFETIME_SECONDS",
    # DCA-100 — typed job-handler registry
    "DuplicateHandlerError",
    "HandlerDescriptor",
    "JobHandler",
    "JobHandlerError",
    "JobHandlerRegistry",
    "JobPayloadValidationError",
    "PayloadFieldError",
    "UnknownJobTypeError",
    "UnsupportedSchemaVersionError",
    "ValidatedJob",
    "build_default_registry",
]
