"""Durable wrapper around :class:`AiClient` — resolves DCA-001 blockers B2/B4 on the DCA side.

Every gateway call gets a persisted ``AiInvocation`` row BEFORE the HTTP request:
- ``job_id = "dca:<row id>"`` — DCA-owned namespace (the envelope's jobId is an opaque string; the
  platform's AIJob table never sees DCA jobs, so we must not collide with or imitate its ids);
- ``request_id = "dca:<row id>"`` — deterministic, so a retry after timeout replays the SAME
  ``X-Request-Id`` and the AI-side idempotency cache prevents double spend within its TTL;
- outcome (status/cost) is written back, giving DCA its own durable spend/audit record because the
  AI-side cache is in-memory and does not survive restarts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.clients.ai_client import AiClient, AiServiceUnavailable
from app.db.models import AiInvocation


def invoke_recorded(
    session: Session,
    client: AiClient,
    *,
    business_id: str,
    capability: str,
    input_context: dict[str, Any] | None = None,
    untrusted: list[dict[str, str]] | None = None,
    dry_run: bool = False,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    invocation = AiInvocation(
        business_id=business_id, capability=capability, dry_run=dry_run, job_id="", request_id=""
    )
    session.add(invocation)
    session.flush()  # assigns the id
    invocation.job_id = f"dca:{invocation.id}"
    invocation.request_id = f"dca:{invocation.id}"
    session.commit()

    try:
        envelope = client.invoke(
            capability,
            business_id=business_id,
            job_id=invocation.job_id,
            request_id=invocation.request_id,
            input_context=input_context,
            untrusted=untrusted,
            correlation_id=correlation_id,
            dry_run=dry_run,
        )
    except AiServiceUnavailable:
        invocation.status = "unavailable"  # retryable later with the same request_id
        session.commit()
        raise

    invocation.status = "completed" if envelope.get("status") == "completed" else "failed"
    invocation.response_status = envelope.get("status")
    cost = envelope.get("costEstimate") or {}
    invocation.cost_amount = float(cost.get("amount") or 0.0)
    invocation.cost_currency = str(cost.get("currency") or "USD")
    invocation.completed_at = datetime.now(UTC)
    session.commit()
    return envelope
