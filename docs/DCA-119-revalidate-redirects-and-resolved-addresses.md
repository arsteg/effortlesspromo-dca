# DCA-119 — Revalidate redirects and resolved addresses

Stage: Approved source access · Key: `fetch-redirect` · Source sections 6, 8, 9

## What this adds

A thin, pure, **network-free** extension of DCA-118's destination guard that re-runs that same guard
on **every hop of a redirect chain** — the initial URL and each redirect target, with each hop's
freshly-resolved IP(s). This makes good the guarantee a single-URL check cannot:

> **An initially permitted URL cannot redirect or connect into a forbidden address.**

- Module: `app/sources/redirect_guard.py`
- Entry point: `validate_redirect_chain(hops, *, allowlist, allowed_ports=..., max_redirects=5)`
  → `RedirectChainDecision` (`DestinationVerdict.ALLOW | DENY`, a single decisive `reason`, and on a
  `DENY` the `blocked_index` / `blocked_url` of the offending hop).
- Input: an ordered `Sequence[RedirectHop]` — `hops[0]` is the initial request URL, each later entry
  is a redirect target; each `RedirectHop` carries the DNS answer (`resolved_ips`) the caller already
  obtained for that hop.

It reuses `validate_destination` (DCA-118) for all URL/address classification rather than duplicating
any of it, and is written in the same shape as the other pure deciders in this service: a load-bearing
docstring, enums for the chain-level reasons, and a deterministic function that touches no database,
opens no socket, follows no redirect, and resolves/fetches nothing.

## Why per-hop re-validation

A URL that looks safe on the first lookup (allowlisted host, https, resolves public) can still reach
something internal:

- a `30x` `Location` header pointing at `http://127.0.0.1/`, `http://169.254.169.254/` (cloud
  metadata) or an RFC1918 host, or
- **DNS rebinding** — the same allowlisted host re-resolving to a private address on a later
  connection.

Validating only the first URL would miss both. DCA-119 re-checks the target and the fresh DNS answer
of every hop, so the first unsafe hop fails the whole chain.

## Checks (hop order, stop at first failure)

1. **Non-empty chain** — an empty chain → `malformed_url` (detail `empty redirect chain`).
2. **Redirect cap** — more than `DEFAULT_MAX_REDIRECTS` (= 5) redirects after the initial request →
   `too_many_redirects`. The bounded prefix is still fully re-validated first, so an unsafe hop inside
   the cap is reported with its own precise reason (a real client stops following at the cap).
3. **No scheme downgrade across a redirect** — an `https` hop redirecting to a plain `http` target →
   `scheme_downgrade` (a redirect-only property; the guard alone permits both schemes).
4. **The DCA-118 guard, re-run on every hop** — any guard refusal (`scheme_not_allowed`,
   `credentials_in_url`, `port_not_allowed`, `host_not_allowlisted`, `unresolved`, or any unsafe
   address class — `private_address`, `loopback_address`, `link_local_address`,
   `cloud_metadata_address`, `unique_local_address`, `reserved_address`) fails the whole chain at that
   hop and carries the guard's own `DenyReason`.

Chain-level reasons live in `RedirectDenyReason` (`too_many_redirects`, `scheme_downgrade`); every
per-hop reason is the DCA-118 `DenyReason`, reused unchanged. `RedirectChainDecision.reason_code`
flattens either to its stable string value for logging / manual output.

## No redirect-following, no DNS, no fetch (disabled future capability)

The guard **follows no redirect and resolves no name itself** — a client driving its own fetch/DNS
here could be the leak it defends against and would make decisions non-reproducible. The caller
supplies the chain it already observed (per-hop `resolved_ips`); the guard verifies it. An `ALLOW` is
permission to *attempt* the chain pinned to each hop's `safe_ips`; the guard connects to, resolves,
follows and fetches nothing. Actual fetching remains a disabled future capability — **no migration,
schema, config, or network path is added by this story.**

## Verification

```bash
# focused tests
.venv/Scripts/python.exe -m pytest tests/test_redirect_guard.py -q

# full suite (exit code is authoritative)
.venv/Scripts/python.exe -m pytest -q

# inspectable manual scenario (prints real per-hop verdicts for a safe chain + each blocked class)
.venv/Scripts/python.exe scripts/manual_check.py DCA-119
```

The manual check prints the real per-hop URLs/resolved_ips, the chain `verdict`, the decisive `reason`
and the `blocked_index` for a safe http→https→www chain (ALLOW) and for each blocked class — including
a safe-looking allowlisted URL that redirects to loopback / cloud-metadata, a same-host rebind to a
private address, an https→http downgrade, credentials appearing mid-chain, and an over-cap chain. No
network, DNS, redirect-following, socket, fetch, or paid/AI action occurs.

## Files

- `app/sources/redirect_guard.py` (new)
- `tests/test_redirect_guard.py` (new)
- `scripts/manual_check.py` (registered `DCA-119` scenario)
- `docs/DCA-119-revalidate-redirects-and-resolved-addresses.md` (this file)

No migrations, config, or schema changes.
