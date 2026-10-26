"""DCA-033 — a local, non-production contract-test HTTP server harness.

This is the reusable, generalised form of the ad-hoc ``FakeHost`` the DCA-032 manual check wired
inline: a configurable fake endpoint that the real :class:`~app.clients.platform_transport.
PlatformHttpTransport` (or any ``httpx``-based client) can be pointed at **without a network**. It
exists so a single contract — a transport outcome, a classified response, a denial — can be driven,
reviewed and accepted in isolation.

What it does and, just as importantly, what it refuses to do:

- **Simulates transport outcomes.** A fixture route can return any HTTP status + body/headers, or it
  can *fail at the transport layer* — a ``timeout`` (connect or read phase) or an ``unreachable``
  network error — so a caller's whole classification surface (success / 4xx / 5xx / timeout /
  unreachable) is exercisable. An optional real ``delay`` lets a tester induce a genuinely slow call
  (e.g. to trip a client's timeout).
- **Records every call.** Each served request is captured as a :class:`RecordedCall` (method, path,
  correlation id, redacted headers, body) so a manual tester can diff the wire against expectations.
  The call log is the state-change probe: a denied/unknown request still records (or not) exactly as
  the contract requires.
- **Never invents a missing business contract.** A request to a path with *no registered fixture*
  gets a deliberate, clearly-labelled ``404`` "no fixture configured" response — it does **not**
  fabricate a plausible success. Reusing only identity/entitlement fixtures (already implemented) is
  opt-in; every later adapter story registers its *own* domain fixtures here. Nothing domain-specific
  is pre-wired.
- **Cannot run under production settings.** :func:`build_fake_contract_server` refuses a production
  configuration (:class:`FakeServerForbiddenInProduction`), mirroring the identity/entitlement fakes
  and the dev-only seed. The server also carries an explicit non-production :meth:`~FakeContractServer
  .banner`.

No real network, no AI, no provider SDK/key, no paid action, and no writes to any database happen
here. The server is an ``httpx`` request handler; point a client's transport at :meth:`FakeContract
Server.as_transport` and nothing leaves the box.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from app.core.logging import CORRELATION_HEADER, REQUEST_ID_HEADER

# A loud, unmistakable marker so no output (banner, repr, log) can be mistaken for a real endpoint.
NON_PRODUCTION_BANNER = (
    "DCA FAKE CONTRACT SERVER — NON-PRODUCTION / SYNTHETIC FIXTURES ONLY. "
    "No real network, AI, or paid action occurs here."
)

# Headers whose values must never be echoed verbatim into the recorded call log — the credential
# header above all. The value is replaced with a fixed marker (the DCA-012 redactor also scrubs the
# literal, but masking here keeps the recorded call obviously synthetic and leak-free by construction).
_SENSITIVE_HEADERS = frozenset({"authorization", "x-service-auth", "x-api-key", "cookie"})
_REDACTED = "«redacted»"


class FakeServerForbiddenInProduction(RuntimeError):
    """Raised when a fake contract server is requested under production configuration.

    Mirrors :class:`~app.identity.fake.IdentityContractDisabled` /
    :class:`~app.entitlements.fake.EntitlementContractDisabled`: the harness is dev/test-only and must
    never be selectable in production, so construction fails rather than handing back a fake endpoint.
    """


class OutcomeKind(str, Enum):
    """What a fixture route does when hit.

    ``RESPONSE`` returns a real HTTP response (any status + body). ``TIMEOUT`` and ``UNREACHABLE``
    raise the matching ``httpx`` transport error *before* a response exists, so a client sees a
    genuine transport failure (and, for the platform transport, classifies it as ``timeout`` /
    ``unreachable``). These three cover the full outcome surface a caller must handle.
    """

    RESPONSE = "response"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"


class TimeoutPhase(str, Enum):
    """Which phase a simulated timeout occurs in. A caller that classifies on the common timeout base
    must treat both identically, so the harness can drive either."""

    CONNECT = "connect"
    READ = "read"


@dataclass(frozen=True)
class FixtureOutcome:
    """A single scripted outcome for a route. Immutable; build one with the factory helpers below.

    ``delay_seconds`` is a *real* sleep applied before the outcome is produced — use it to induce a
    slow call (e.g. to exceed a client's read timeout). It defaults to 0 so tests never sleep unless
    they ask to.
    """

    kind: OutcomeKind = OutcomeKind.RESPONSE
    status_code: int = 200
    json: Any | None = None
    text: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0
    timeout_phase: TimeoutPhase = TimeoutPhase.READ

    @classmethod
    def response(
        cls,
        status_code: int = 200,
        *,
        json: Any | None = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
        delay_seconds: float = 0.0,
    ) -> FixtureOutcome:
        """An HTTP response with the given status/body. Any status is allowed (incl. 4xx/5xx/429)."""
        return cls(
            kind=OutcomeKind.RESPONSE,
            status_code=status_code,
            json=json,
            text=text,
            headers=dict(headers or {}),
            delay_seconds=delay_seconds,
        )

    @classmethod
    def timeout(
        cls, *, phase: TimeoutPhase = TimeoutPhase.READ, delay_seconds: float = 0.0
    ) -> FixtureOutcome:
        """A transport timeout (no response). ``phase`` selects connect vs read; a caller that
        classifies on the timeout base treats both the same."""
        return cls(kind=OutcomeKind.TIMEOUT, timeout_phase=phase, delay_seconds=delay_seconds)

    @classmethod
    def unreachable(cls, *, delay_seconds: float = 0.0) -> FixtureOutcome:
        """A hard network failure (no response) — e.g. connection refused/DNS failure."""
        return cls(kind=OutcomeKind.UNREACHABLE, delay_seconds=delay_seconds)


@dataclass(frozen=True)
class FixtureRoute:
    """A registered fixture: ``(method, path)`` → :class:`FixtureOutcome`.

    ``path`` is matched exactly against the request's URL path (leading slash optional — it is
    normalised). Keeping matching exact and explicit means the harness never guesses: an unmatched
    path is a *missing contract*, not a fuzzy near-hit.
    """

    method: str
    path: str
    outcome: FixtureOutcome

    @property
    def key(self) -> tuple[str, str]:
        return (self.method.upper(), _normalise_path(self.path))


@dataclass(frozen=True)
class RecordedCall:
    """One request the fake server was asked to serve — the audit/diff surface for a manual tester.

    Sensitive header values are masked (see :data:`_SENSITIVE_HEADERS`); ``matched`` records whether a
    fixture existed for this ``(method, path)`` so an "unknown contract" denial is visible in the log.
    """

    index: int
    method: str
    path: str
    url: str
    correlation_id: str | None
    headers: dict[str, str]
    body: bytes
    matched: bool

    @property
    def text(self) -> str:
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError:  # pragma: no cover - synthetic fixtures are always text
            return ""


def _normalise_path(path: str) -> str:
    p = (path or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    # Drop any trailing slash (except the root) so "/x" and "/x/" register/match identically.
    return p[:-1] if len(p) > 1 and p.endswith("/") else p


class FakeContractServer:
    """A configurable, non-production fake HTTP endpoint for contract tests.

    Register fixture routes, then hand :meth:`as_transport` to an ``httpx`` client (or pass
    :meth:`handle` directly to :class:`httpx.MockTransport`). Every served request is recorded in
    :attr:`calls`. A request with no registered fixture returns a deliberate 404 — the server never
    fabricates a business contract it was not given.
    """

    def __init__(self, *, base_path: str = "", sleep: Any = time.sleep) -> None:
        # ``base_path`` is the prefix the endpoint is mounted under (e.g. the client's base URL path,
        # ``/svc``); it is stripped from an incoming request path before matching, so fixtures are
        # registered relative to the service root regardless of where it is mounted.
        # ``sleep`` is injectable purely so a test can assert a configured delay was applied without a
        # real wall-clock wait; it defaults to the real ``time.sleep``.
        self._base_path = _normalise_path(base_path) if base_path.strip() else ""
        self._routes: dict[tuple[str, str], FixtureRoute] = {}
        self._sleep = sleep
        self.calls: list[RecordedCall] = []

    def _match_path(self, request_path: str) -> str:
        """The path used for fixture matching: the request path with the mount prefix removed."""
        path = _normalise_path(request_path)
        if self._base_path and path.startswith(self._base_path):
            stripped = path[len(self._base_path) :]
            return _normalise_path(stripped) if stripped else "/"
        return path

    # -- banner / identity -------------------------------------------------------------------------

    def banner(self) -> str:
        """The explicit non-production banner. Printed by harness entrypoints and the manual check."""
        return NON_PRODUCTION_BANNER

    # -- fixture registration ----------------------------------------------------------------------

    def register(self, route: FixtureRoute) -> FixtureRoute:
        """Register (or replace) a fixture route. Returns the route for convenience."""
        self._routes[route.key] = route
        return route

    def set_route(self, method: str, path: str, outcome: FixtureOutcome) -> FixtureRoute:
        """Register/replace a fixture by its parts — the common case (and how a tester 'switches' a
        route to, say, a timeout response: just call it again with a new outcome)."""
        return self.register(FixtureRoute(method=method, path=path, outcome=outcome))

    def register_all(self, routes: Iterable[FixtureRoute]) -> None:
        for route in routes:
            self.register(route)

    def clear_routes(self) -> None:
        self._routes.clear()

    @property
    def routes(self) -> tuple[FixtureRoute, ...]:
        return tuple(self._routes.values())

    # -- inspection --------------------------------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_call(self) -> RecordedCall | None:
        return self.calls[-1] if self.calls else None

    def reset_calls(self) -> None:
        self.calls.clear()

    # -- the httpx handler -------------------------------------------------------------------------

    def as_transport(self) -> httpx.MockTransport:
        """An :class:`httpx.MockTransport` wrapping this server — pass to ``httpx.Client(transport=...)``
        or the platform transport's ``transport=`` seam."""
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Serve one request: record it, apply any configured delay, then produce the fixture outcome
        (or a 404 'no fixture' response if the contract was never registered)."""
        key = (request.method.upper(), self._match_path(request.url.path))
        route = self._routes.get(key)
        self.calls.append(self._record(request, matched=route is not None))

        if route is None:
            # No registered fixture: deliberately a 404 that says so. The harness never invents a
            # missing business contract — an absent adapter contract stays absent/disabled.
            return httpx.Response(
                404,
                json={
                    "error": "no_fixture_configured",
                    "message": (
                        f"no fake fixture registered for {key[0]} {key[1]}; "
                        "the harness does not invent missing contracts"
                    ),
                    "synthetic": True,
                },
                request=request,
            )

        outcome = route.outcome
        if outcome.delay_seconds > 0:
            self._sleep(outcome.delay_seconds)

        if outcome.kind is OutcomeKind.TIMEOUT:
            exc_type = (
                httpx.ConnectTimeout
                if outcome.timeout_phase is TimeoutPhase.CONNECT
                else httpx.ReadTimeout
            )
            raise exc_type(f"fake contract server: simulated {outcome.timeout_phase.value} timeout")
        if outcome.kind is OutcomeKind.UNREACHABLE:
            raise httpx.ConnectError("fake contract server: simulated unreachable host")

        body = dict(outcome.json) if isinstance(outcome.json, dict) else outcome.json
        return httpx.Response(
            outcome.status_code,
            json=body if outcome.text is None else None,
            text=outcome.text,
            headers=outcome.headers or None,
            request=request,
        )

    # -- internals ---------------------------------------------------------------------------------

    def _record(self, request: httpx.Request, *, matched: bool) -> RecordedCall:
        safe_headers = {
            name: (_REDACTED if name.lower() in _SENSITIVE_HEADERS else value)
            for name, value in request.headers.items()
        }
        cid = request.headers.get(CORRELATION_HEADER) or request.headers.get(REQUEST_ID_HEADER)
        return RecordedCall(
            index=len(self.calls),
            method=request.method.upper(),
            path=_normalise_path(request.url.path),
            url=str(request.url),
            correlation_id=cid,
            headers=safe_headers,
            body=request.content,
            matched=matched,
        )

    def __repr__(self) -> str:
        return (
            f"FakeContractServer(routes={len(self._routes)}, calls={len(self.calls)}) "
            "[NON-PRODUCTION]"
        )


def build_fake_contract_server(
    settings: Any, *, base_path: str = "", sleep: Any = time.sleep
) -> FakeContractServer:
    """Build a fake contract server, refusing production configuration.

    The harness is dev/test-only, so — exactly like the identity and entitlement fakes — calling this
    under a production configuration raises :class:`FakeServerForbiddenInProduction` rather than
    handing back a fake endpoint. ``settings`` only needs an ``is_production`` flag/attribute.
    ``base_path`` is the mount prefix the endpoint is served under (see :class:`FakeContractServer`).
    """
    if getattr(settings, "is_production", False):
        raise FakeServerForbiddenInProduction(
            "the fake contract-test server is non-production only and cannot be selected under "
            "production settings (set a non-production environment to use it)"
        )
    return FakeContractServer(base_path=base_path, sleep=sleep)


def register_identity_entitlement_fixtures(server: FakeContractServer) -> FakeContractServer:
    """Opt-in: seed ONLY the already-implemented identity & entitlement synthetic fixtures as read
    routes, so a contract test has realistic synthetic bodies to assert against.

    This deliberately reuses the existing fakes (:mod:`app.identity.fake` / :mod:`app.entitlements.
    fake`) and registers nothing domain-specific — every later adapter story (suppression, CRM,
    delivery, …) registers its OWN fixtures here. The routes are illustrative read-only contracts for
    the two synthetic businesses A and B; no business contract is invented.
    """
    from app.entitlements.contract import EntitlementRequest
    from app.entitlements.fake import FakeEntitlementClient
    from app.identity.contract import IdentityRequest
    from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, FakeIdentityClient

    identity = FakeIdentityClient()
    entitlements = FakeEntitlementClient()

    for business in (BUSINESS_A, BUSINESS_B):
        ident = identity.resolve(IdentityRequest(subject=USER_ALICE, business_id=business))
        ent = entitlements.resolve(EntitlementRequest(business_id=business))
        # The contract responses are Pydantic models; ``mode="json"`` renders enums/datetimes into
        # wire-ready JSON. The identity body is marked synthetic (the entitlement snapshot already is).
        server.set_route(
            "GET",
            f"/identity/{business}",
            FixtureOutcome.response(200, json={**ident.model_dump(mode="json"), "synthetic": True}),
        )
        server.set_route(
            "GET",
            f"/entitlements/{business}",
            FixtureOutcome.response(200, json=ent.model_dump(mode="json")),
        )
    return server
