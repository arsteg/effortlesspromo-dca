"""Durable AI bookkeeping (B2/B4): rows persisted before the call, request ids deterministic."""

import httpx
import pytest
from sqlalchemy import select

from app.clients.ai_client import AiClient, AiServiceUnavailable
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AiInvocation
from app.services.ai_invoker import invoke_recorded


@pytest.fixture
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        yield s


def test_success_records_cost_and_dca_namespaced_ids(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "costEstimate": {"amount": 0.25, "currency": "USD"},
            },
        )

    client = AiClient("http://ai", "t", transport=httpx.MockTransport(handler))
    invoke_recorded(session, client, business_id="biz-1", capability="research")

    row = session.scalars(select(AiInvocation)).one()
    assert row.status == "completed"
    assert row.job_id == f"dca:{row.id}"
    assert row.request_id == f"dca:{row.id}"  # deterministic => timeout retry replays the same id
    assert row.cost_amount == 0.25
    assert row.business_id == "biz-1"


def test_unavailable_keeps_row_for_reconciliation(session):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    client = AiClient("http://ai", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(AiServiceUnavailable):
        invoke_recorded(session, client, business_id="biz-1", capability="research")

    row = session.scalars(select(AiInvocation)).one()
    assert row.status == "unavailable"  # durable: survives DCA restarts, unlike the AI-side cache
    assert row.request_id  # the key a retry must reuse
