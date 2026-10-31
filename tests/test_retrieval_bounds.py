"""DCA-120 — the pure retrieval resource guard (the fetch client's byte/time/type/bomb bounds).

Unit tests over :mod:`app.sources.retrieval_bounds`, independent of any HTTP/DNS/socket: a within-cap
response of an approved type streams to an ``OK`` with its bounded body, and every bound — oversized
body (declared and streamed), slow/timed-out read, disallowed/missing content type, decompression
bomb / stream-too-large, and cooperative cancellation — is stopped with the correct deterministic
reason and NO body returned (so nothing invalid is ever stored). The guard is pure and network-free:
it only counts bytes, never parses or executes them, and fetches nothing.
"""

from __future__ import annotations

from app.sources.retrieval_bounds import (
    Chunk,
    RetrievalLimitReason,
    RetrievalLimits,
    RetrievalStream,
    RetrievalVerdict,
    bound_retrieval,
)

# Small, explicit caps so the fixtures are tiny and the breach points are obvious.
LIMITS = RetrievalLimits(
    max_response_bytes=1000,
    total_timeout_seconds=5.0,
    max_decompressed_bytes=10_000,
    max_decompression_ratio=10.0,
    allowed_content_types=frozenset({"text/html", "application/json", "text/plain"}),
)


def _stream(
    content_type: str | None,
    chunks: list[Chunk],
    *,
    content_length: int | None = None,
    content_encoding: str | None = None,
) -> RetrievalStream:
    return RetrievalStream(
        content_type=content_type,
        chunks=iter(chunks),
        content_length=content_length,
        content_encoding=content_encoding,
    )


class _FakeClock:
    """A deterministic monotonic clock: each call returns the next value in a fixed schedule, holding
    the last value once exhausted. Lets a 'slow' response be reproduced with no real waiting."""

    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._i = 0

    def __call__(self) -> float:
        value = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return value


# --------------------------------------------------------------------------------------------------
# Within-limit retrieval is OK and returns the bounded body
# --------------------------------------------------------------------------------------------------


def test_within_limits_approved_type_is_ok_with_body() -> None:
    stream = _stream(
        "text/html; charset=utf-8",
        [Chunk.of(b"<html>"), Chunk.of(b"hello"), Chunk.of(b"</html>")],
        content_length=18,
    )
    out = bound_retrieval(stream, LIMITS, clock=_FakeClock([0.0, 0.1, 0.2, 0.3, 0.4]))
    assert out.verdict is RetrievalVerdict.OK
    assert out.allowed
    assert out.reason is None
    assert out.body == b"<html>hello</html>"
    assert out.content_type == "text/html"  # parameters stripped, lower-cased
    assert out.decoded_bytes == len(b"<html>hello</html>")


def test_empty_body_within_limits_is_ok() -> None:
    out = bound_retrieval(_stream("application/json", []), LIMITS)
    assert out.allowed
    assert out.body == b""
    assert out.wire_bytes == 0


# --------------------------------------------------------------------------------------------------
# Content-type allowlist (decided before any body byte is read)
# --------------------------------------------------------------------------------------------------


def test_disallowed_content_type_refused() -> None:
    out = bound_retrieval(_stream("application/octet-stream", [Chunk.of(b"x")]), LIMITS)
    assert out.verdict is RetrievalVerdict.ABORTED
    assert out.reason is RetrievalLimitReason.CONTENT_TYPE_NOT_ALLOWED
    assert out.body == b""


def test_missing_content_type_refused_fail_closed() -> None:
    assert (
        bound_retrieval(_stream(None, [Chunk.of(b"x")]), LIMITS).reason
        is RetrievalLimitReason.MISSING_CONTENT_TYPE
    )
    assert (
        bound_retrieval(_stream("   ", [Chunk.of(b"x")]), LIMITS).reason
        is RetrievalLimitReason.MISSING_CONTENT_TYPE
    )


# --------------------------------------------------------------------------------------------------
# Oversized body — declared and streamed
# --------------------------------------------------------------------------------------------------


def test_declared_length_over_cap_refused_before_reading() -> None:
    # A body that blows up the moment it is iterated proves we refused BEFORE touching it.
    def _boom() -> "list[Chunk]":
        raise AssertionError("body must not be read when declared length already exceeds the cap")
        yield  # pragma: no cover — makes this a generator; never reached

    stream = RetrievalStream(
        content_type="text/html",
        chunks=_boom(),  # only advanced if the guard actually reads the body
        content_length=5000,
    )
    out = bound_retrieval(stream, LIMITS)
    assert out.reason is RetrievalLimitReason.DECLARED_LENGTH_EXCEEDS_LIMIT
    assert out.body == b""


def test_streamed_body_over_cap_aborted() -> None:
    # 3 x 400 wire bytes = 1200 > 1000 cap; aborted on the chunk that crosses it, body discarded.
    chunks = [Chunk.of(b"a" * 400) for _ in range(3)]
    out = bound_retrieval(_stream("text/plain", chunks), LIMITS)
    assert out.reason is RetrievalLimitReason.BODY_TOO_LARGE
    assert out.body == b""
    # No unbounded memory: at most one chunk beyond the cap was counted.
    assert out.wire_bytes <= LIMITS.max_response_bytes + 400


# --------------------------------------------------------------------------------------------------
# Slow / timed-out read
# --------------------------------------------------------------------------------------------------


def test_slow_read_times_out() -> None:
    # Clock jumps past the 5s timeout before the 2nd chunk is processed.
    clock = _FakeClock([0.0, 1.0, 9.0, 9.0])
    out = bound_retrieval(
        _stream("text/plain", [Chunk.of(b"a"), Chunk.of(b"b")]),
        LIMITS,
        clock=clock,
    )
    assert out.reason is RetrievalLimitReason.TIMEOUT_EXCEEDED
    assert out.body == b""


# --------------------------------------------------------------------------------------------------
# Decompression bomb / stream-too-large
# --------------------------------------------------------------------------------------------------


def test_decompression_ratio_bomb_aborted() -> None:
    # Tiny on the wire, huge decoded: 50 wire bytes -> 2000 decoded (ratio 40 > 10) once decoded
    # output passes the 1000-byte body cap. Caught as a decompression limit, not a plain body cap.
    stream = _stream(
        "text/html",
        [Chunk.of(b"x" * 2000, wire_size=50)],
        content_encoding="gzip",
    )
    out = bound_retrieval(stream, LIMITS)
    assert out.reason is RetrievalLimitReason.DECOMPRESSION_LIMIT_EXCEEDED
    assert out.body == b""
    assert out.wire_bytes <= LIMITS.max_response_bytes  # stayed small on the wire


def test_decoded_output_over_absolute_cap_aborted() -> None:
    # Within the wire cap per chunk but decoded output blows the absolute decoded cap (10_000).
    stream = _stream(
        "text/html",
        [Chunk.of(b"y" * 6000, wire_size=500), Chunk.of(b"y" * 6000, wire_size=400)],
        content_encoding="br",
    )
    out = bound_retrieval(stream, LIMITS)
    assert out.reason is RetrievalLimitReason.DECOMPRESSION_LIMIT_EXCEEDED
    assert out.body == b""


def test_uncompressed_high_apparent_ratio_is_not_a_bomb() -> None:
    # Not compressed => the ratio screen never fires; a normal small body is OK.
    out = bound_retrieval(_stream("text/html", [Chunk.of(b"z" * 100)]), LIMITS)
    assert out.allowed


# --------------------------------------------------------------------------------------------------
# Cooperative cancellation
# --------------------------------------------------------------------------------------------------


def test_cancellation_stops_the_read() -> None:
    out = bound_retrieval(
        _stream("text/plain", [Chunk.of(b"a"), Chunk.of(b"b")]),
        LIMITS,
        cancelled=lambda: True,
    )
    assert out.reason is RetrievalLimitReason.CANCELLED
    assert out.body == b""


# --------------------------------------------------------------------------------------------------
# Purity / determinism
# --------------------------------------------------------------------------------------------------


def test_outcome_is_deterministic_and_reason_code_stable() -> None:
    def run() -> object:
        return bound_retrieval(
            _stream("application/octet-stream", [Chunk.of(b"x")]),
            LIMITS,
            clock=_FakeClock([0.0]),
        )

    first, second = run(), run()
    assert first == second
    assert first.reason_code == "content_type_not_allowed"


def test_from_settings_builds_bounded_limits() -> None:
    from app.core.config import Settings

    settings = Settings(_env_file=None, environment="testing", database_url="sqlite://")
    limits = RetrievalLimits.from_settings(settings)
    assert limits.max_response_bytes > 0
    assert limits.total_timeout_seconds > 0
    assert limits.max_decompression_ratio > 1
    assert "text/html" in limits.allowed_content_types
