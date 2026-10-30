"""DCA-100 — the typed Python job-handler registry.

Key: ``job-registry`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: an **explicit** registry that binds a durable job's ``(job_type, schema_version)`` to a
*typed* Python handler whose payload is a Pydantic model, so that — before any work could ever run — a
job's payload is validated against its handler's schema and an unknown job type (or unknown version of a
known type) fails safely. This is the gate that sits between a persisted :class:`~app.db.models.DurableJobRequest`
(DCA-099 — a pointer + digest of intent) and the actual execution of that intent.

Two load-bearing rules, both fail-closed:

- **Unknown job types fail safely.** Resolving a ``job_type`` nothing registered raises
  :class:`UnknownJobTypeError`; resolving a *known* type at a ``schema_version`` nothing registered raises
  :class:`UnsupportedSchemaVersionError`. Neither returns a handler, so an unrecognised job can never be
  run — it is rejected, and rejection changes nothing (the registry is pure in-memory; it touches no DB).
- **Invalid payloads never reach execution.** The ONLY way to obtain a runnable
  (:class:`ValidatedJob` = handler + validated payload) is through :meth:`JobHandlerRegistry.prepare`,
  which resolves the handler and validates the raw payload against its Pydantic model FIRST. A payload
  that fails validation raises :class:`JobPayloadValidationError` (carrying a PII-safe field list — a
  location + issue, never the submitted value) before any :class:`ValidatedJob` is produced, so a future
  worker is handed only payloads that already conform to the handler's typed contract.

**Execution stays a disabled future capability.** Registering a handler and validating a payload runs
nothing: this story resolves + validates only. A handler's :meth:`JobHandler.run` is the typed contract a
later (currently-disabled) worker/claim capability will drive; no path in this story ever invokes it, and
there is no worker loop here. Registering work grants nothing and sends nothing — the DCA has no send
path, and acceptance of a prospect is not recipient consent.

The production registry (:func:`build_default_registry`) is deliberately **empty**: real job-type
handlers are wired by their own future stories (each with its payload schema + rights/rulepack review).
Callers register the handlers they own; the registry is the shared mechanism.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ValidationError


class JobHandlerError(Exception):
    """Base for every registry rejection. A registry rejection is always fail-closed: it resolves /
    validates nothing and changes no state (the registry holds only in-memory handler bindings)."""


class UnknownJobTypeError(JobHandlerError):
    """No handler is registered for this ``job_type`` at all — the job is unrecognised and is refused.

    Raised before any handler is returned, so an unknown job type can never reach execution."""

    def __init__(self, job_type: str) -> None:
        self.job_type = job_type
        super().__init__(f"no handler registered for job type {job_type!r}")


class UnsupportedSchemaVersionError(JobHandlerError):
    """The ``job_type`` is known, but no handler is registered for the requested ``schema_version``.

    Distinct from :class:`UnknownJobTypeError` so a caller can tell "unrecognised work" from "a known job
    at a payload-contract version this build does not understand" (e.g. a row enqueued under a newer
    schema than the running code). Either way the job is refused and nothing runs."""

    def __init__(self, job_type: str, schema_version: int, known_versions: tuple[int, ...]) -> None:
        self.job_type = job_type
        self.schema_version = schema_version
        self.known_versions = known_versions
        super().__init__(
            f"job type {job_type!r} has no handler for schema version {schema_version} "
            f"(known versions: {list(known_versions)})"
        )


class DuplicateHandlerError(JobHandlerError):
    """Two handlers were registered for the SAME ``(job_type, schema_version)``.

    Registration is explicit and a binding is one-to-one; a collision is a wiring bug, refused at
    registration time rather than silently letting the second handler shadow the first."""

    def __init__(self, job_type: str, schema_version: int) -> None:
        self.job_type = job_type
        self.schema_version = schema_version
        super().__init__(
            f"a handler is already registered for {job_type!r} schema version {schema_version}"
        )


@dataclass(frozen=True)
class PayloadFieldError:
    """One PII-safe validation issue: WHERE it is and WHAT kind of issue — never the submitted value.

    ``loc`` is the dotted field location (e.g. ``"members.0.key_type"``); ``issue`` is Pydantic's stable
    error ``type`` (e.g. ``"missing"``, ``"int_parsing"``, ``"extra_forbidden"``). The submitted value is
    deliberately excluded — a job payload may reference tenant data, and an error must never echo it (same
    discipline as the DCA-011 error envelope)."""

    loc: str
    issue: str


class JobPayloadValidationError(JobHandlerError):
    """A raw payload did not conform to its handler's typed Pydantic schema.

    Raised by :meth:`JobHandlerRegistry.validate_payload` / :meth:`~JobHandlerRegistry.prepare` BEFORE any
    :class:`ValidatedJob` is produced, so an invalid payload never reaches execution. Carries a PII-safe
    :attr:`errors` list (location + issue only); the offending values are never retained."""

    def __init__(
        self, job_type: str, schema_version: int, errors: tuple[PayloadFieldError, ...]
    ) -> None:
        self.job_type = job_type
        self.schema_version = schema_version
        self.errors = errors
        locations = ", ".join(f"{e.loc}:{e.issue}" for e in errors) or "(none)"
        super().__init__(
            f"payload for job type {job_type!r} schema version {schema_version} failed validation "
            f"[{locations}]"
        )


class JobHandler:
    """A typed handler for ONE ``(job_type, schema_version)`` — its payload schema plus the (future) body.

    Subclasses set three class attributes and implement :meth:`run`:

    - ``job_type`` — the logical kind of work (matches :attr:`DurableJobRequest.job_type`).
    - ``schema_version`` — the payload-contract version this handler understands (matches the row's
      ``schema_version``); a new contract is a new handler at a new version, never an in-place edit.
    - ``payload_model`` — a Pydantic :class:`BaseModel` subclass; the registry validates the raw payload
      against it before the handler could ever run. Models should forbid extra fields so an unexpected
      key is a validation error, not silently dropped.

    :meth:`run` is the typed execution contract a future (currently-disabled) worker will call with an
    already-validated payload. **No path in this story invokes it** — registering a handler and validating
    a payload runs nothing."""

    job_type: ClassVar[str]
    schema_version: ClassVar[int] = 1
    payload_model: ClassVar[type[BaseModel]]

    def run(
        self, payload: BaseModel
    ) -> None:  # pragma: no cover - execution is a disabled future story
        """Execute the job against an already-validated payload. A DISABLED future capability — the
        registry never calls this in DCA-100; it exists only as the typed contract a later worker drives."""
        raise NotImplementedError(
            "job execution is a disabled future capability; DCA-100 only registers + validates handlers"
        )


@dataclass(frozen=True)
class HandlerDescriptor:
    """A discoverable, inspectable description of one registered binding — no behaviour, safe to list."""

    job_type: str
    schema_version: int
    handler: str
    payload_model: str


@dataclass(frozen=True)
class ValidatedJob:
    """A resolved handler bound to a payload that has ALREADY passed the handler's typed validation.

    The only thing that can produce one is :meth:`JobHandlerRegistry.prepare`, and only after validation
    succeeds — so holding a :class:`ValidatedJob` is the proof an invalid payload never reached this far.
    It is inert data: a future worker would call ``job.handler.run(job.payload)``; this story never does.
    """

    handler: JobHandler
    payload: BaseModel

    @property
    def job_type(self) -> str:
        return self.handler.job_type

    @property
    def schema_version(self) -> int:
        return self.handler.schema_version


class JobHandlerRegistry:
    """An explicit, in-memory registry of typed job handlers keyed by ``(job_type, schema_version)``.

    Explicit: a handler is bound only by an outright :meth:`register` call (no import-time autodiscovery),
    and a second handler for the same ``(job_type, schema_version)`` is refused. The registry is the single
    gate between a persisted job request and its execution: :meth:`prepare` resolves the handler and
    validates the payload against its schema, and unknown types / unknown versions / invalid payloads all
    fail closed before anything could run. It touches no database and holds no tenant state, so a rejection
    changes nothing."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, int], JobHandler] = {}

    # ------------------------------------------------------------------ registration
    def register(self, handler: JobHandler) -> JobHandler:
        """Bind ``handler`` to its ``(job_type, schema_version)``. Explicit and one-to-one.

        Refuses a handler missing a ``job_type`` / ``payload_model`` (a mis-declared handler is a wiring
        bug) and a second handler for an already-registered ``(job_type, schema_version)``
        (:class:`DuplicateHandlerError`). Returns the handler so it can be used as a decorator."""
        job_type = getattr(handler, "job_type", None)
        if not job_type or not isinstance(job_type, str):
            raise JobHandlerError(f"{type(handler).__name__} must declare a non-empty str job_type")
        payload_model = getattr(handler, "payload_model", None)
        if not (isinstance(payload_model, type) and issubclass(payload_model, BaseModel)):
            raise JobHandlerError(
                f"{type(handler).__name__} must declare a Pydantic BaseModel payload_model"
            )
        schema_version = int(handler.schema_version)
        key = (job_type, schema_version)
        if key in self._handlers:
            raise DuplicateHandlerError(job_type, schema_version)
        self._handlers[key] = handler
        return handler

    # ------------------------------------------------------------------ discovery
    def is_registered(self, job_type: str, schema_version: int) -> bool:
        """Whether a handler is bound for this exact ``(job_type, schema_version)``."""
        return (job_type, int(schema_version)) in self._handlers

    def known_versions(self, job_type: str) -> tuple[int, ...]:
        """Every registered ``schema_version`` for ``job_type`` (ascending); empty if the type is unknown."""
        return tuple(sorted(v for (t, v) in self._handlers if t == job_type))

    def descriptors(self) -> tuple[HandlerDescriptor, ...]:
        """Every registered binding as inspectable descriptors (stable order). All handlers are
        discoverable — this is the enumeration the manual check and any future admin view read."""
        return tuple(
            HandlerDescriptor(
                job_type=job_type,
                schema_version=version,
                handler=type(handler).__name__,
                payload_model=handler.payload_model.__name__,
            )
            for (job_type, version), handler in sorted(self._handlers.items())
        )

    # ------------------------------------------------------------------ resolution + validation (the gate)
    def resolve(self, job_type: str, schema_version: int) -> JobHandler:
        """The handler for ``(job_type, schema_version)``, or fail safely.

        :class:`UnknownJobTypeError` if nothing is registered for ``job_type`` at all;
        :class:`UnsupportedSchemaVersionError` if the type is known but not at this version. Never returns
        ``None`` and never guesses a nearby version — an unrecognised job is refused, not run."""
        version = int(schema_version)
        handler = self._handlers.get((job_type, version))
        if handler is not None:
            return handler
        versions = self.known_versions(job_type)
        if not versions:
            raise UnknownJobTypeError(job_type)
        raise UnsupportedSchemaVersionError(job_type, version, versions)

    def validate_payload(
        self, job_type: str, schema_version: int, payload: Mapping[str, object]
    ) -> BaseModel:
        """Resolve the handler then validate ``payload`` against its Pydantic model; return the typed model.

        Fail-closed in order: an unknown type/version raises before validation; an invalid payload raises
        :class:`JobPayloadValidationError` (PII-safe — location + issue only) rather than returning a model.
        This is where "invalid payloads never reach execution" is enforced."""
        handler = self.resolve(job_type, schema_version)
        try:
            return handler.payload_model.model_validate(dict(payload))
        except ValidationError as exc:
            # Carry only location + stable error type — never the submitted value (it may be tenant data).
            errors = tuple(
                PayloadFieldError(
                    loc=".".join(str(part) for part in err["loc"]) or "(root)",
                    issue=str(err["type"]),
                )
                for err in exc.errors()
            )
            raise JobPayloadValidationError(job_type, int(schema_version), errors) from None

    def prepare(
        self, job_type: str, schema_version: int, payload: Mapping[str, object]
    ) -> ValidatedJob:
        """Resolve + validate, returning a runnable :class:`ValidatedJob` ONLY when both succeed.

        The single sanctioned way to turn a persisted job request into something a (future) worker could
        run. Because the payload is validated first, a :class:`ValidatedJob` is never produced for an
        unknown type/version or an invalid payload — execution is unreachable for anything that fails the
        gate. This story returns the validated job; it does not run it."""
        validated = self.validate_payload(job_type, schema_version, payload)
        handler = self.resolve(job_type, schema_version)
        return ValidatedJob(handler=handler, payload=validated)


def build_default_registry() -> JobHandlerRegistry:
    """The production job-handler registry — deliberately **empty** in DCA-100.

    The registry MECHANISM ships here; real job-type handlers are each their own future story (payload
    schema + rights/rulepack review), and future execution stays disabled. An empty registry still fails
    closed correctly: every ``job_type`` resolves to :class:`UnknownJobTypeError`, so nothing can run.
    This is the single wiring point a later story appends its ``register(...)`` calls to."""
    return JobHandlerRegistry()
