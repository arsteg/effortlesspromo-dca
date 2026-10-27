"""DCA-038 — the targeting-validation HTTP contract (``POST /v1/targeting/validate``).

Proves the service surface over the resolver: the India-only gate, the headline ``+91``-alone denial,
the foreign/unresolved blocks, the separation of the three contexts, structured-field validation, and
that the route is stateless + fail-closed on auth. The route touches no database, so every call —
allow or deny — is reproducible with no side effect.
"""

from __future__ import annotations

from tests.conftest import AUTH


def _validate(client, body: dict):
    return client.post("/v1/targeting/validate", json=body, headers=AUTH)


def test_requires_service_auth(client):
    resp = client.post("/v1/targeting/validate", json={})
    assert resp.status_code == 401


def test_indian_pin_recipient_is_allowed(client):
    resp = _validate(client, {"recipientLocation": {"pinCode": "560001"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is True
    assert body["reasonCode"] == "targeting_allowed_in"
    assert body["recipient"]["status"] == "in"
    assert body["recipientStructured"]["pinValid"] is True


def test_plus91_with_no_location_is_blocked(client):
    # The developer manual-test case: a +91 contact with no location evidence.
    resp = _validate(
        client,
        {"recipientLocation": {"evidence": [{"kind": "phone_country_code", "value": "+91"}]}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is False
    assert body["reasonCode"] == "blocked_unresolved_jurisdiction"
    assert body["recipient"]["reasonCode"] == "unresolved_weak_signal_only"


def test_dot_in_domain_alone_is_blocked(client):
    resp = _validate(
        client, {"recipientLocation": {"evidence": [{"kind": "domain_tld", "value": ".in"}]}}
    )
    assert resp.json()["allowed"] is False
    assert resp.json()["recipient"]["reasonCode"] == "unresolved_weak_signal_only"


def test_empty_body_is_blocked_no_evidence(client):
    resp = _validate(client, {})
    body = resp.json()
    assert body["allowed"] is False
    assert body["reasonCode"] == "blocked_unresolved_jurisdiction"
    assert body["recipient"]["reasonCode"] == "unresolved_no_evidence"


def test_foreign_recipient_is_blocked(client):
    resp = _validate(client, {"recipientLocation": {"country": "US"}})
    body = resp.json()
    assert body["allowed"] is False
    assert body["reasonCode"] == "blocked_foreign_jurisdiction"
    assert body["recipient"]["country"] == "US"


def test_indian_legal_address_does_not_rescue_weak_recipient(client):
    resp = _validate(
        client,
        {
            "recipientLocation": {"evidence": [{"kind": "phone_country_code", "value": "+91"}]},
            "legalAddress": {"country": "IN", "stateUt": "KA", "pinCode": "560001"},
            "operationalArea": {"stateUt": "KA"},
        },
    )
    body = resp.json()
    assert body["allowed"] is False
    assert body["legalAddress"]["status"] == "in"
    assert body["operationalArea"]["status"] == "in"
    assert body["notes"]  # separation is called out


def test_conflicting_recipient_evidence_is_blocked(client):
    resp = _validate(
        client,
        {
            "recipientLocation": {
                "pinCode": "560001",
                "evidence": [{"kind": "declared_country", "value": "US"}],
            }
        },
    )
    body = resp.json()
    assert body["allowed"] is False
    assert body["recipient"]["reasonCode"] == "unresolved_conflicting_evidence"
    assert body["recipient"]["provenCountries"] == ["IN", "US"]


def test_structured_field_validation_echoed(client):
    resp = _validate(
        client,
        {"recipientLocation": {"country": "india", "stateUt": "Maharashtra", "pinCode": "40001"}},
    )
    structured = resp.json()["recipientStructured"]
    assert structured["country"] == "IN"
    assert structured["stateUt"] == "MH"
    assert structured["stateUtName"] == "Maharashtra"
    assert structured["pinValid"] is False  # five digits


def test_unrecognized_state_is_flagged(client):
    resp = _validate(client, {"recipientLocation": {"stateUt": "Atlantis"}})
    body = resp.json()
    structured = body["recipientStructured"]
    assert structured["stateUtRecognized"] is False
    # An unrecognized state proves nothing → recipient unresolved → blocked.
    assert body["allowed"] is False
    assert body["recipient"]["reasonCode"] == "unresolved_no_evidence"


def test_reference_route_lists_states_and_evidence_kinds(client):
    resp = client.get("/v1/targeting/reference", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["country"] == "IN"
    assert body["statesAndUts"]["MH"] == "Maharashtra"
    assert "pin_code" in body["evidenceKinds"]["strong"]
    assert "phone_country_code" in body["evidenceKinds"]["weak"]
