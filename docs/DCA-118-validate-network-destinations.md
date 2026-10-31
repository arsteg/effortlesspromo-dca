# DCA-118 — Validate network destinations

Stage: Approved source access · Key: `fetch-ssrf` · Source sections 6, 8, 9

## What this adds

The centralized fetch client's **destination guard** — a pure, deterministic, **network-free**
decider that rejects a forbidden network destination **before any connection**, so that a
model-supplied (or crawled / redirected) URL cannot be used to reach anything internal.

- Module: `app/sources/destination_guard.py`
- Entry point: `validate_destination(url, *, allowlist, resolved_ips=None, allowed_ports=...)`
  → `DestinationDecision` (`DestinationVerdict.ALLOW | DENY`, a single `DenyReason`, and on ALLOW
  the exact public `safe_ips` the caller must pin to).

It is written in the same shape as the other pure deciders in this service
(`app/jobs/retry.py`, `app/compliance/eligibility_outcome.py`, `app/normalization/domains.py`):
a load-bearing docstring, enums for the verdict and every deny reason, and a function that is a
deterministic function of its inputs that touches no database, opens no socket, and fetches nothing.

## The guarantee

**A forbidden destination is rejected before connection, and a model-supplied URL cannot bypass the
guard.** Every would-be fetch must pass through `validate_destination` first; only an explicit
`ALLOW` may be connected to, and the caller must pin that connection to the returned `safe_ips`.

## Checks (fixed order, stop at first failure)

1. **Malformed / hostless URL** → `malformed_url` (a schemeless/relative string lands here).
2. **Scheme** — only `http`/`https` → else `scheme_not_allowed`.
3. **Embedded credentials** — `user:pass@host` → `credentials_in_url`.
4. **Port** — only 80/443 (or an implicit default); else `port_not_allowed`.
5. **Address safety + allowlist**:
   - A **literal IP host** is classified by address first (an unsafe literal is denied by its precise
     class; a public literal must itself be on the allowlist, else `host_not_allowlisted`).
   - A **hostname** must be on the domain `allowlist` (empty allowlist allows nothing — fail-closed),
     then its DNS answer is checked: `resolved_ips` must be supplied (`unresolved` otherwise) and
     **every** resolved address must be public. Any private/metadata/… answer fails the whole
     destination (defeats DNS rebinding / a mixed public+private answer).

Address-class reasons: `cloud_metadata_address` (169.254.169.254 & AWS/Alibaba variants),
`loopback_address`, `link_local_address`, `private_address` (RFC1918), `unique_local_address`
(IPv6 `fc00::/7`), `reserved_address` (unspecified / multicast / otherwise non-global).
IPv4-mapped IPv6 (`::ffff:a.b.c.d`) is unwrapped before classification.

## No DNS, no fetch (disabled future capability)

The guard performs **no name resolution of its own** — a resolver it drove could be the leak it is
defending against and would make decisions non-reproducible. The caller supplies the DNS answer it
already obtained (`resolved_ips`); the guard verifies it. Deciding `ALLOW` is permission to *attempt*
a connection; the guard connects to, resolves and fetches nothing. Actual fetching remains a disabled
future capability (no migration, no schema, no network path added by this story).

## Verification

```bash
# focused tests
.venv/Scripts/python.exe -m pytest tests/test_destination_guard.py -q

# full suite (exit code is authoritative)
.venv/Scripts/python.exe -m pytest -q

# inspectable manual scenario (prints real verdicts for an allow + several blocks)
.venv/Scripts/python.exe scripts/manual_check.py DCA-118
```

The manual check prints the real `verdict`/`reason`/`safe_ips` for a representative allowlisted
public destination (ALLOW) and for each blocked class — including an allowlisted URL whose DNS answer
points at the metadata service (blocked `cloud_metadata_address`) and a mixed public+private answer
(blocked `private_address`). No network, DNS, socket, fetch, or paid/AI action occurs.

## Files

- `app/sources/destination_guard.py` (new)
- `tests/test_destination_guard.py` (new)
- `scripts/manual_check.py` (registered `DCA-118` scenario)
- `docs/DCA-118-validate-network-destinations.md` (this file)

No migrations, config, or schema changes.
