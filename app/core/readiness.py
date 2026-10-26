"""DCA-013 — database + local-dependency readiness, reported apart from optional integrations.

Readiness answers "can this instance serve requests?" — which here means its **required local
dependencies** are healthy. The DCA-owned database is the only hard requirement: if it is down,
readiness fails (HTTP 503) while liveness keeps answering 200 (liveness must never touch the DB, so
an orchestrator can tell "process alive but not serving" from "process dead").

**Optional integrations** (effortlesspromo-ai, synthetic sources, the worker loop) are reported in a
separate block and NEVER affect the ready/not-ready verdict — they are disabled by default and the
service is fully ready without them.

The report carries only *statuses*, never a connection string, driver error text, token, or any
other credential (DCA-013 acceptance). A failed DB probe is logged by type only — never with the
DSN or a traceback that could embed one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("dca.health")

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
STATUS_ENABLED = "enabled"
STATUS_CONFIGURED = "configured"


@dataclass(frozen=True)
class CheckResult:
    """One required-dependency probe. Deliberately carries no error detail — status only."""

    name: str
    ok: bool
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"status": STATUS_OK if self.ok else STATUS_ERROR, "required": self.required}


def check_database(engine: Engine) -> CheckResult:
    """Probe the DCA-owned database with a trivial round-trip. Any failure => not ready, but the
    cause is swallowed into a boolean: the response must never leak the DSN or driver error."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return CheckResult("database", ok=True)
    except Exception as exc:  # noqa: BLE001 — any DB failure must become "not ready", not a 500
        # Type only — never exc_info (a traceback frame can hold the connection URL/password).
        log.warning("database readiness probe failed (%s)", type(exc).__name__)
        return CheckResult("database", ok=False)


def optional_integrations(settings: Any) -> dict[str, dict[str, Any]]:
    """Optional, off-by-default integrations — reported for visibility, never gating readiness."""
    ai_enabled = bool(getattr(settings, "ai_enabled", False))
    synthetic = bool(getattr(settings, "synthetic_sources_enabled", False))
    worker = bool(getattr(settings, "worker_enabled", False))
    return {
        # No base URL / token is echoed — only whether the integration is wired at all.
        "ai_service": {
            "enabled": ai_enabled,
            "status": STATUS_CONFIGURED if ai_enabled else STATUS_DISABLED,
        },
        "synthetic_sources": {
            "enabled": synthetic,
            "status": STATUS_ENABLED if synthetic else STATUS_DISABLED,
        },
        "worker": {
            "enabled": worker,
            "status": STATUS_ENABLED if worker else STATUS_DISABLED,
        },
    }


def build_readiness(settings: Any, engine: Engine) -> tuple[dict[str, Any], bool]:
    """Assemble the readiness report. Returns ``(report, ready)`` — ``ready`` is true iff every
    required check passed; optional integrations never change it."""
    checks = [check_database(engine)]
    ready = all(check.ok for check in checks if check.required)
    report = {
        "status": "ready" if ready else "not_ready",
        "checks": {check.name: check.to_dict() for check in checks},
        "integrations": optional_integrations(settings),
    }
    return report, ready
