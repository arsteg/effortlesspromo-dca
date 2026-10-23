"""Inbound service-to-service authentication — same fail-closed pattern as effortlesspromo-ai.

Callers present a shared token in ``X-Service-Auth``. No token configured => every privileged call
is rejected (401), never served open. This gate authenticates the CALLER SERVICE only; it does not
authorize any tenant. Per DCA-001 B3, tenant authority must be established by the platform before a
request reaches this service — endpoints here only ever operate on the business id given in the
path and never let one tenant read or mutate another's rows.

Reads the app's own Settings (app.state.settings) so tests and multi-instance setups never fall
back to ambient environment state.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request


def require_service_auth(request: Request, x_service_auth: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.service_auth_token
    if not expected:
        raise HTTPException(status_code=401, detail="service auth not configured")
    if not x_service_auth or not hmac.compare_digest(x_service_auth, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Service-Auth")
