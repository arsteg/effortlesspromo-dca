"""DCA-120 — bound retrieval resources (the centralized fetch client's resource guard).

Key: ``fetch-bounds`` · source sections 6, 8, 9. Stage: Approved source access.

Builds on DCA-118 (:mod:`app.sources.destination_guard`) and DCA-119
(:mod:`app.sources.redirect_guard`). Those two decide *where* a fetch may connect; this story bounds
*how much* a fetch may consume once a connection is (one day) permitted. The single load-bearing
guarantee: **an oversized, slow, decompression-bomb, or non-approved-type response is stopped mid-read
and is NEVER returned as valid evidence.** A retrieval that breaches any bound yields an inert
``ABORTED`` outcome carrying no body at all — the partial bytes are discarded — so a caller can only
ever store the result of a retrieval that stayed inside every bound.

Like DCA-118/119 this is a **pure, deterministic, network-free guard**. It opens **no socket, resolves
no name, performs no HTTP, and — crucially — executes NOTHING it reads**: every byte is treated as
opaque data that is only *counted*, never parsed, decoded-as-code, rendered, or evaluated (no HTML/JS
is ever run). The real network stream is a disabled future capability; here the caller supplies a
:class:`RetrievalStream` — the response metadata it already observed plus an *iterator of byte chunks*
standing in for the body it would read off the wire. The guard consumes that iterator under the caps
and **stops pulling from it the instant a bound is crossed**, so at most one chunk beyond the cap is
ever held in memory (no unbounded buffering — this is what defuses a decompression bomb / an endless
body). Time is read from an injected monotonic ``clock`` (default :func:`time.monotonic`) and
cancellation from an injected ``cancelled`` predicate, so the decision is reproducible from its inputs
with no wall-clock or thread dependence.

What it enforces, in a fixed order, STOP-AT-FIRST-FAILURE so the abort reason is deterministic:

1. **Content-type allowlist** — checked from the declared response metadata BEFORE a single body byte
   is read. A missing/blank type is refused (``MISSING_CONTENT_TYPE``, fail-closed); a present type
   outside :attr:`RetrievalLimits.allowed_content_types` is refused (``CONTENT_TYPE_NOT_ALLOWED``).
   Parameters (``; charset=…``) are stripped and matching is case-insensitive.
2. **Declared length** — if the response advertised a ``Content-Length`` already larger than the body
   cap, the retrieval is refused before reading anything (``DECLARED_LENGTH_EXCEEDS_LIMIT``) — the
   cheapest possible rejection of an oversized body.
3. **Per-chunk streaming bounds**, re-checked on every chunk, in this order:
   - **Cancellation** — the injected predicate returned true (an operator stop / shutdown) →
     ``CANCELLED``.
   - **Total timeout** — elapsed monotonic time exceeded :attr:`RetrievalLimits.total_timeout_seconds`
     → ``TIMEOUT_EXCEEDED`` (this is what stops a *slow* response — a trickle that never finishes).
   - **On-the-wire body cap** — accumulated wire bytes exceeded
     :attr:`RetrievalLimits.max_response_bytes` → ``BODY_TOO_LARGE``.
   - **Decoded-output cap** — accumulated decoded bytes exceeded
     :attr:`RetrievalLimits.max_decompressed_bytes` → ``DECOMPRESSION_LIMIT_EXCEEDED``.
   - **Decompression ratio** — for a compressed response, once decoded output has grown past the body
     cap, a decoded/wire ratio above :attr:`RetrievalLimits.max_decompression_ratio` →
     ``DECOMPRESSION_LIMIT_EXCEEDED`` (a body tiny on the wire that balloons when decoded — the
     classic zip/gzip bomb — is caught here long before it exhausts memory).

Only a stream that exhausts cleanly within every bound yields ``OK`` with the bounded, validated body.
A breach yields ``ABORTED`` with the single decisive reason and an **empty** body. The guard adds no
migration, schema, socket, HTTP, DNS, or network path; it only VALIDATES + bounds, and acting on an
``OK`` stays the caller's still-disabled fetch capability.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import only for typing — no runtime dependency on Settings
    from app.core.config import Settings


class RetrievalVerdict(str, Enum):
    """Whether a retrieval stayed within every resource bound. ``OK`` carries the bounded, validated
    body; ``ABORTED`` carries no body (the partial read is discarded, so nothing invalid is stored)."""

    OK = "ok"
    ABORTED = "aborted"


class RetrievalLimitReason(str, Enum):
    """The stable, PII-safe reason a retrieval was aborted — the single place each bound is named.

    - ``MISSING_CONTENT_TYPE`` — the response declared no (or a blank) content type; refused
      fail-closed before any body was read.
    - ``CONTENT_TYPE_NOT_ALLOWED`` — the declared type is not on the allowlist.
    - ``DECLARED_LENGTH_EXCEEDS_LIMIT`` — the advertised ``Content-Length`` already exceeds the body
      cap; refused before reading a single byte.
    - ``BODY_TOO_LARGE`` — the on-the-wire body exceeded the byte cap while streaming.
    - ``TIMEOUT_EXCEEDED`` — the total elapsed time exceeded the timeout (a slow / stalled response).
    - ``DECOMPRESSION_LIMIT_EXCEEDED`` — the decoded output exceeded its absolute cap, or (for a
      compressed response) the decoded/wire ratio ran away — a decompression bomb / stream-too-large.
    - ``CANCELLED`` — an injected cancellation predicate asked the read to stop (operator / shutdown).
    """

    MISSING_CONTENT_TYPE = "missing_content_type"
    CONTENT_TYPE_NOT_ALLOWED = "content_type_not_allowed"
    DECLARED_LENGTH_EXCEEDS_LIMIT = "declared_length_exceeds_limit"
    BODY_TOO_LARGE = "body_too_large"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    DECOMPRESSION_LIMIT_EXCEEDED = "decompression_limit_exceeded"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RetrievalLimits:
    """The resource caps applied to one retrieval — conservative, always bounded, never 0/unbounded.

    Built from configuration via :meth:`from_settings` (validated positive at boot) or constructed
    directly in tests. ``allowed_content_types`` is normalized (lower-cased, parameter-stripped) so
    matching is case/parameter-insensitive; an empty allowlist permits nothing (fail-closed)."""

    max_response_bytes: int
    total_timeout_seconds: float
    max_decompressed_bytes: int
    max_decompression_ratio: float
    allowed_content_types: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> RetrievalLimits:
        return cls(
            max_response_bytes=settings.retrieval_max_response_bytes,
            total_timeout_seconds=settings.retrieval_total_timeout_seconds,
            max_decompressed_bytes=settings.retrieval_max_decompressed_bytes,
            max_decompression_ratio=settings.retrieval_max_decompression_ratio,
            allowed_content_types=_normalize_types(settings.retrieval_allowed_content_types),
        )


@dataclass(frozen=True)
class Chunk:
    """One chunk of a response body the caller read (or would read) off the wire.

    ``decoded`` is the payload bytes after any transfer/content decoding (what a consumer would see);
    ``wire_size`` is how many bytes that chunk occupied ON the wire (equal to ``len(decoded)`` for an
    uncompressed response, smaller for a compressed one). Keeping both lets the guard bound the wire
    body, the decoded output, and the decompression ratio independently."""

    decoded: bytes
    wire_size: int

    @classmethod
    def of(cls, decoded: bytes, wire_size: int | None = None) -> Chunk:
        return cls(decoded, len(decoded) if wire_size is None else int(wire_size))


@dataclass(frozen=True)
class RetrievalStream:
    """The inert stand-in for a response the caller observed/would read — never a live socket.

    Carries the declared metadata (``content_type``, optional ``content_length``, optional
    ``content_encoding`` such as ``gzip``/``br``) plus ``chunks``, an iterator of :class:`Chunk` that
    stands in for the body the caller would pull off the wire. The guard resolves/opens nothing itself;
    it only consumes this iterator — and stops the instant a bound is crossed."""

    content_type: str | None
    chunks: Iterable[Chunk]
    content_length: int | None = None
    content_encoding: str | None = None

    @property
    def is_compressed(self) -> bool:
        enc = (self.content_encoding or "").strip().lower()
        return bool(enc) and enc not in {"identity", "none"}


@dataclass(frozen=True)
class RetrievalOutcome:
    """The inert result of bounding one retrieval — nothing has been (or will be) fetched or executed.

    On ``OK`` the whole stream stayed within every bound and ``body`` carries the bounded, validated
    bytes (safe to keep as evidence). On ``ABORTED`` ``body`` is empty (the partial read was discarded)
    and ``reason`` names the single decisive bound that was breached. ``content_type`` is the
    normalized declared type (when present); ``wire_bytes`` / ``decoded_bytes`` / ``elapsed_seconds``
    report how far the bounded read got before it stopped."""

    verdict: RetrievalVerdict
    reason: RetrievalLimitReason | None = None
    content_type: str = ""
    body: bytes = b""
    wire_bytes: int = 0
    decoded_bytes: int = 0
    elapsed_seconds: float = 0.0
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is RetrievalVerdict.OK

    @property
    def reason_code(self) -> str | None:
        """The ``reason`` as its stable string value (or ``None`` when OK) — handy for logging and
        manual-check output."""
        return self.reason.value if self.reason is not None else None

    @classmethod
    def ok(
        cls, *, content_type: str, body: bytes, wire_bytes: int, elapsed_seconds: float
    ) -> RetrievalOutcome:
        return cls(
            RetrievalVerdict.OK,
            content_type=content_type,
            body=body,
            wire_bytes=wire_bytes,
            decoded_bytes=len(body),
            elapsed_seconds=elapsed_seconds,
        )

    @classmethod
    def aborted(
        cls,
        reason: RetrievalLimitReason,
        *,
        content_type: str = "",
        wire_bytes: int = 0,
        decoded_bytes: int = 0,
        elapsed_seconds: float = 0.0,
        detail: str = "",
    ) -> RetrievalOutcome:
        return cls(
            RetrievalVerdict.ABORTED,
            reason=reason,
            content_type=content_type,
            body=b"",
            wire_bytes=wire_bytes,
            decoded_bytes=decoded_bytes,
            elapsed_seconds=elapsed_seconds,
            detail=detail,
        )


def _normalize_type(value: str | None) -> str:
    """Lower-case a content type and drop its parameters (``text/html; charset=utf-8`` -> ``text/html``)."""
    if not value:
        return ""
    return value.split(";", 1)[0].strip().lower()


def _normalize_types(values: Iterable[str]) -> frozenset[str]:
    return frozenset(t for t in (_normalize_type(v) for v in values) if t)


def bound_retrieval(
    stream: RetrievalStream,
    limits: RetrievalLimits,
    *,
    clock: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> RetrievalOutcome:
    """Read ``stream`` under ``limits``, purely and without any network access, stopping the instant a
    resource bound is crossed.

    ``limits`` are the caps to enforce (see :class:`RetrievalLimits`). ``clock`` supplies monotonic
    time for the timeout bound (default :func:`time.monotonic`; inject a fake clock in tests).
    ``cancelled`` is an optional predicate polled before each chunk so an operator stop / shutdown can
    abort a long read cooperatively.

    Returns a :class:`RetrievalOutcome`. ``OK`` carries the bounded, validated body (safe to store);
    ``ABORTED`` carries no body and names the single decisive bound breached. The content is only
    counted — never parsed, decoded-as-code, rendered, or executed — and nothing is fetched: acting on
    an ``OK`` stays the caller's still-disabled fetch capability.
    """
    is_cancelled = cancelled or (lambda: False)

    # 1) Content type — decided from declared metadata BEFORE reading any body byte (fail-closed).
    content_type = _normalize_type(stream.content_type)
    if not content_type:
        return RetrievalOutcome.aborted(
            RetrievalLimitReason.MISSING_CONTENT_TYPE, detail="no declared content type"
        )
    if content_type not in limits.allowed_content_types:
        return RetrievalOutcome.aborted(
            RetrievalLimitReason.CONTENT_TYPE_NOT_ALLOWED,
            content_type=content_type,
            detail=content_type,
        )

    # 2) Declared length — refuse an already-oversized body before reading a single byte.
    if stream.content_length is not None and stream.content_length > limits.max_response_bytes:
        return RetrievalOutcome.aborted(
            RetrievalLimitReason.DECLARED_LENGTH_EXCEEDS_LIMIT,
            content_type=content_type,
            detail=f"declared {stream.content_length} > cap {limits.max_response_bytes}",
        )

    # 3) Stream the body under the per-chunk bounds, stopping at the first breach so memory stays
    #    bounded (at most one chunk is read beyond a cap, then the whole partial read is discarded).
    start = clock()
    wire_total = 0
    decoded_total = 0
    buffer = bytearray()
    compressed = stream.is_compressed

    for chunk in stream.chunks:
        if is_cancelled():
            return RetrievalOutcome.aborted(
                RetrievalLimitReason.CANCELLED,
                content_type=content_type,
                wire_bytes=wire_total,
                decoded_bytes=decoded_total,
                elapsed_seconds=clock() - start,
                detail="cancellation requested",
            )

        elapsed = clock() - start
        if elapsed > limits.total_timeout_seconds:
            return RetrievalOutcome.aborted(
                RetrievalLimitReason.TIMEOUT_EXCEEDED,
                content_type=content_type,
                wire_bytes=wire_total,
                decoded_bytes=decoded_total,
                elapsed_seconds=elapsed,
                detail=f"elapsed {elapsed:.3f}s > cap {limits.total_timeout_seconds}s",
            )

        wire_total += chunk.wire_size
        decoded_total += len(chunk.decoded)

        if wire_total > limits.max_response_bytes:
            return RetrievalOutcome.aborted(
                RetrievalLimitReason.BODY_TOO_LARGE,
                content_type=content_type,
                wire_bytes=wire_total,
                decoded_bytes=decoded_total,
                elapsed_seconds=clock() - start,
                detail=f"wire {wire_total} > cap {limits.max_response_bytes}",
            )

        if decoded_total > limits.max_decompressed_bytes:
            return RetrievalOutcome.aborted(
                RetrievalLimitReason.DECOMPRESSION_LIMIT_EXCEEDED,
                content_type=content_type,
                wire_bytes=wire_total,
                decoded_bytes=decoded_total,
                elapsed_seconds=clock() - start,
                detail=f"decoded {decoded_total} > cap {limits.max_decompressed_bytes}",
            )

        # Decompression ratio: only meaningful for a compressed body and only once decoded output has
        # grown past the on-the-wire body cap — then a runaway ratio is a bomb (tiny on the wire, huge
        # decoded) and is stopped long before it can exhaust memory.
        if (
            compressed
            and wire_total > 0
            and decoded_total > limits.max_response_bytes
            and decoded_total / wire_total > limits.max_decompression_ratio
        ):
            return RetrievalOutcome.aborted(
                RetrievalLimitReason.DECOMPRESSION_LIMIT_EXCEEDED,
                content_type=content_type,
                wire_bytes=wire_total,
                decoded_bytes=decoded_total,
                elapsed_seconds=clock() - start,
                detail=(
                    f"ratio {decoded_total / wire_total:.1f} > cap {limits.max_decompression_ratio}"
                ),
            )

        # Only data that stayed within every bound is ever buffered.
        buffer.extend(chunk.decoded)

    return RetrievalOutcome.ok(
        content_type=content_type,
        body=bytes(buffer),
        wire_bytes=wire_total,
        elapsed_seconds=clock() - start,
    )
