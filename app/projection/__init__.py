"""DCA-031 — revision-aware projection of integration events.

One capability: apply events that *project* an aggregate's state in revision order, so a delayed or
retried delivery carrying an OLDER revision can never overwrite a newer projection, and a revision
that arrives ahead of a missing predecessor is held in a visible, reconcilable buffer rather than
applied over a gap.

The single sanctioned writer is :class:`~app.projection.revision.RevisionProjector`; it stages its
changes into the *caller's* session (atomic with the caller's unit of work, like the DCA-026/027/028
sinks) and performs no I/O. It consumes the ``aggregate_version`` the DCA-029 envelope already
carries; the aggregate identity (``aggregate_type`` / ``aggregate_id``) is supplied by the caller,
so the envelope contract is untouched.
"""

from app.projection.revision import (
    RevisionDecision,
    RevisionError,
    RevisionOutcome,
    RevisionPayloadError,
    RevisionProjector,
)

__all__ = [
    "RevisionDecision",
    "RevisionError",
    "RevisionOutcome",
    "RevisionPayloadError",
    "RevisionProjector",
]
