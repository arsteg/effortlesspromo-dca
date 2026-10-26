"""Developer/test-only harnesses for the DCA.

Everything under ``app.testing`` is **non-production** by construction: each tool here refuses a
production configuration and is meant only for local contract tests, the manual-check runner, and
demos against synthetic fixtures. Nothing here performs real network, AI, or paid I/O.
"""

from __future__ import annotations

from app.testing.fake_server import (
    NON_PRODUCTION_BANNER,
    FakeContractServer,
    FakeServerForbiddenInProduction,
    FixtureOutcome,
    FixtureRoute,
    OutcomeKind,
    RecordedCall,
    TimeoutPhase,
    build_fake_contract_server,
    register_identity_entitlement_fixtures,
)

__all__ = [
    "NON_PRODUCTION_BANNER",
    "FakeContractServer",
    "FakeServerForbiddenInProduction",
    "FixtureOutcome",
    "FixtureRoute",
    "OutcomeKind",
    "RecordedCall",
    "TimeoutPhase",
    "build_fake_contract_server",
    "register_identity_entitlement_fixtures",
]
