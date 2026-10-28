"""Projection: turn stored state into the view a consumer is permitted to see.

Two capabilities:

- **DCA-031 — revision-aware projection of integration events.** Apply events that *project* an
  aggregate's state in revision order, so a delayed or retried delivery carrying an OLDER revision can
  never overwrite a newer projection, and a revision that arrives ahead of a missing predecessor is
  held in a visible, reconcilable buffer rather than applied over a gap. The single sanctioned writer
  is :class:`~app.projection.revision.RevisionProjector`; it stages its changes into the *caller's*
  session (atomic with the caller's unit of work, like the DCA-026/027/028 sinks) and performs no I/O.

- **DCA-063 — filter fields by display entitlement (rights-aware projection).** A pure, read-only
  projection (:mod:`app.projection.field_visibility`) that decides, per field / evidence object, what
  a business is permitted to SEE: a licensed shared fact is served back only when the business holds a
  DISPLAY entitlement for it, a private overlay (the business's own data) is always visible, and a
  restricted value is dropped so it is absent from the payload rather than merely hidden.
"""

from app.projection.field_visibility import (
    DISPLAY_OPERATION,
    REASON_DISPLAY_NOT_ENTITLED,
    DisplayDecider,
    ProjectedEvidence,
    ProjectedField,
    display_decider_from_permits,
    is_private_overlay,
    project_evidence,
    project_field,
    project_fields,
)
from app.projection.revision import (
    RevisionDecision,
    RevisionError,
    RevisionOutcome,
    RevisionPayloadError,
    RevisionProjector,
)

__all__ = [
    "DISPLAY_OPERATION",
    "REASON_DISPLAY_NOT_ENTITLED",
    "DisplayDecider",
    "ProjectedEvidence",
    "ProjectedField",
    "RevisionDecision",
    "RevisionError",
    "RevisionOutcome",
    "RevisionPayloadError",
    "RevisionProjector",
    "display_decider_from_permits",
    "is_private_overlay",
    "project_evidence",
    "project_field",
    "project_fields",
]
