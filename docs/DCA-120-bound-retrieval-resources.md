# DCA-120 — Bound retrieval resources

Stage: Approved source access · Key: `fetch-bounds` · Source sections 6, 8, 9

## What this adds

The centralized fetch client's **resource guard** — a pure, deterministic, **network-free** decider
that bounds *how much* a retrieval may consume. DCA-118/119 decide **where** a fetch may connect; this
story caps body size, total time, decoded output, the decompression ratio, and the content type, so
an oversized / slow / decompression-bomb / non-approved response is **stopped mid-read and never
returned as valid evidence**.

- Module: `app/sources/retrieval_bounds.py`
- Entry point: `bound_retrieval(stream, limits, *, clock=time.monotonic, cancelled=None)`
  → `RetrievalOutcome` (`RetrievalVerdict.OK | ABORTED`, a single `RetrievalLimitReason`, and on `OK`
  the bounded, validated `body`).
- Caps live in config (`RetrievalLimits.from_settings`), validated positive at boot.

Written in the same shape as the other pure guards in this service (`app/sources/destination_guard.py`,
`app/sources/redirect_guard.py`): a load-bearing docstring, enums for the verdict and every limit
reason, and a function that is a deterministic function of its inputs that touches no database, opens
no socket, and fetches nothing.

## The guarantee

**An oversized, slow, decompression-bomb, or non-approved-type response is stopped and is never
returned as evidence.** A breach yields an `ABORTED` outcome with an **empty** body (the partial read
is discarded), so a caller can only ever store the result of a retrieval that stayed inside every
bound. Content is only *counted* — never parsed, rendered, decoded-as-code, or executed (no script
execution), and at most one chunk is read past a cap (no unbounded memory).

## Bounds (fixed order, stop at first breach)

1. **Content-type allowlist** — from declared metadata, before any body byte: missing/blank →
   `missing_content_type` (fail-closed); not on the allowlist → `content_type_not_allowed`.
2. **Declared length** — advertised `Content-Length` already over the byte cap →
   `declared_length_exceeds_limit` (refused before reading a byte).
3. **Per-chunk streaming bounds** — re-checked on every chunk:
   - **Cancellation** (injected predicate) → `cancelled`.
   - **Total timeout** (injected monotonic clock) → `timeout_exceeded` (stops a *slow* response).
   - **On-the-wire body cap** → `body_too_large`.
   - **Decoded-output cap** → `decompression_limit_exceeded`.
   - **Decompression ratio** (compressed body, once decoded output passes the body cap) →
     `decompression_limit_exceeded` (the classic gzip/zip bomb — tiny on the wire, huge decoded).

## Configuration (no migration, safe/bounded by default)

New `Settings` fields (env prefix `EFFORTLESSPROMO_DCA_`), all validated at boot so a cap is never
`0`/unbounded and the ratio is never `<= 1`:

| Key | Default | Meaning |
|---|---|---|
| `RETRIEVAL_MAX_RESPONSE_BYTES` | `5242880` (5 MiB) | on-the-wire body cap per retrieval |
| `RETRIEVAL_TOTAL_TIMEOUT_SECONDS` | `30.0` | max total wall time for one retrieval |
| `RETRIEVAL_MAX_DECOMPRESSED_BYTES` | `52428800` (50 MiB) | absolute decoded-output cap |
| `RETRIEVAL_MAX_DECOMPRESSION_RATIO` | `100.0` | decoded/wire ceiling past the body cap |
| `RETRIEVAL_ALLOWED_CONTENT_TYPES` | html/xhtml/plain/xml/json/ld+json | the only types kept |

These are **policy values**, not a feature switch: the bounds exist and are enforced the moment any
fetch is wired. Actual fetching remains a disabled future capability — this story adds no socket,
HTTP, DNS, migration, schema, or network path.

## No fetch, no execution (disabled future capability)

The real wire stream is not implemented. The caller supplies a `RetrievalStream` — the declared
metadata plus an *iterator of byte chunks* standing in for the body it would read — and the guard
consumes it under the caps, stopping the instant a bound is crossed. Time comes from an injected
`clock` and cancellation from an injected predicate, so the decision is reproducible from its inputs
with no wall-clock or thread dependence. Acting on an `OK` stays the caller's still-disabled fetch
capability.

## Verification

```bash
# focused tests
.venv/Scripts/python.exe -m pytest tests/test_retrieval_bounds.py -q

# full suite (exit code is authoritative)
.venv/Scripts/python.exe -m pytest -q

# inspectable manual scenario (prints real OK + each breach; no network, no fetch)
.venv/Scripts/python.exe scripts/manual_check.py DCA-120
```

The manual check prints the real `verdict`/`reason`/byte counts for a within-limit retrieval (`OK`
with an 18-byte body) and for each breach — disallowed content type, declared-length-over-cap,
streamed body over cap, timeout, and a decompression bomb — each `ABORTED` with `body_len=0`, plus a
cancellation case. No network, DNS, socket, fetch, content execution, or paid/AI action occurs.

## Files

- `app/sources/retrieval_bounds.py` (new)
- `app/core/config.py` (new `retrieval_*` caps + boot validation)
- `tests/test_retrieval_bounds.py` (new)
- `tests/test_config.py` (added cap-validation cases)
- `scripts/manual_check.py` (registered `DCA-120` scenario)
- `docs/DCA-120-bound-retrieval-resources.md` (this file)

No migrations or schema changes.
