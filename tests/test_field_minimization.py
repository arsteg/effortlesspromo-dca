"""DCA-058 — reject prohibited personal field classes (the minimization screen).

Focused unit tests over the pure deterministic screen, plus one integration test proving a prohibited
field is discarded BEFORE a candidate is persisted and never reaches storage. No AI, no network.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.compliance.field_minimization import (
    CLASS_ALLOWED,
    CLASS_CHILDREN_SENSITIVE,
    CLASS_PRIVATE_MOBILE,
    CLASS_UNNECESSARY_IDENTIFIER,
    classify_field,
    is_indian_mobile,
    minimize_contact,
    minimize_fields,
)
from app.compliance.rulepacks import RawCandidate
from app.main import create_app
from tests.conftest import AUTH, make_settings

BIZ = "11111111-1111-1111-1111-111111111111"

# --------------------------------------------------------------------------------------------------
# Classification (deterministic, name + value)
# --------------------------------------------------------------------------------------------------


def test_company_facts_are_allowed():
    for name in (
        "org_name",
        "org_identifier",
        "cin",
        "gstin",
        "role_email",
        "company_email",
        "website",
        "industry",
        "region",
        "address",
        "pincode",
    ):
        assert classify_field(name, "value") == CLASS_ALLOWED, name


def test_children_and_sensitive_fields_are_rejected():
    for name in (
        "age",
        "date_of_birth",
        "dob",
        "guardian_name",
        "religion",
        "caste",
        "health_condition",
        "blood_group",
        "biometric_hash",
        "political_party",
        "gender",
    ):
        assert classify_field(name, "x") == CLASS_CHILDREN_SENSITIVE, name


def test_unnecessary_personal_identifiers_are_rejected():
    for name in ("aadhaar", "aadhar_number", "pan", "passport_no", "voter_id", "din", "ssn"):
        assert classify_field(name, "x") == CLASS_UNNECESSARY_IDENTIFIER, name


def test_private_mobile_fields_are_rejected():
    for name in ("mobile", "personal_phone", "whatsapp", "home_phone", "cell_number"):
        assert classify_field(name, "9812345678") == CLASS_PRIVATE_MOBILE, name


def test_company_landline_stays_allowed():
    # A non-mobile office line is a legitimate company fact.
    assert classify_field("company_phone", "+91-11-2345-6789") == CLASS_ALLOWED
    assert classify_field("office_landline", "0112345678") == CLASS_ALLOWED


def test_indian_mobile_shape_detection():
    assert is_indian_mobile("9812345678")
    assert is_indian_mobile("+91 98765 43210")
    assert is_indian_mobile("098765 43210")
    assert not is_indian_mobile("0112345678")  # landline (leading 0 area code, not a mobile run)
    assert not is_indian_mobile("+91-00000-00000")  # obviously-fake placeholder, not 6-9 lead
    assert not is_indian_mobile("U17120MH2026PTC900001")  # a CIN is not a phone number


# --- the load-bearing rule: a business-role label does NOT bypass classification -----------------


def test_role_label_does_not_bypass_identifier_classification():
    # A director/CEO prefix must not launder a prohibited class.
    assert classify_field("director_aadhaar", "x") == CLASS_UNNECESSARY_IDENTIFIER
    assert classify_field("ceo_pan", "x") == CLASS_UNNECESSARY_IDENTIFIER
    assert classify_field("founderDob", "x") == CLASS_CHILDREN_SENSITIVE


def test_role_label_does_not_bypass_private_mobile_classification():
    # Declared "company" label but the VALUE is a personal mobile -> still a private mobile.
    assert classify_field("ceo_mobile", "9812345678") == CLASS_PRIVATE_MOBILE
    assert classify_field("company_phone", "9812345678") == CLASS_PRIVATE_MOBILE
    assert classify_field("director_contact_number", "+91-98765-43210") == CLASS_PRIVATE_MOBILE


# --------------------------------------------------------------------------------------------------
# minimize_fields / minimize_contact (discard + log-safety)
# --------------------------------------------------------------------------------------------------


def test_minimize_fields_keeps_allowed_and_discards_prohibited():
    result = minimize_fields(
        {
            "org_name": "Synthetic Textiles Pvt Ltd",
            "gstin": "27AAAAA0000A1Z5",
            "aadhaar": "1234-5678-9012",
            "age": "15",
            "personal_mobile": "9812345678",
        }
    )
    assert result.kept == {"org_name": "Synthetic Textiles Pvt Ltd", "gstin": "27AAAAA0000A1Z5"}
    assert set(result.rejected_names()) == {"aadhaar", "age", "personal_mobile"}
    assert result.rejected is True


def test_log_summary_carries_no_raw_values():
    forbidden_value = "1234-5678-9012"
    result = minimize_fields({"aadhaar": forbidden_value, "org_name": "Acme"})
    summary = result.log_summary()
    # The summary is names + classes + reasons only — the raw value must appear NOWHERE.
    assert forbidden_value not in repr(summary)
    assert summary["rejected"] == [
        {
            "field": "aadhaar",
            "class": CLASS_UNNECESSARY_IDENTIFIER,
            "reason": "prohibited_unnecessary_identifier",
        }
    ]
    assert summary["kept_fields"] == ["org_name"]


def test_minimize_contact_preserves_labels_but_strips_private_mobile():
    # kind=company_phone + role=CEO are labels; the phone VALUE is a personal mobile -> discarded.
    contact = {"kind": "company_phone", "role": "CEO", "phone": "+91-98765-43210"}
    result = minimize_contact(contact)
    assert result.kept == {"kind": "company_phone", "role": "CEO"}
    assert result.rejected_names() == ["phone"]
    assert "98765" not in repr(result.log_summary())


def test_minimize_contact_keeps_role_email_untouched():
    contact = {"kind": "role_email", "email": "info@synthetic-textiles.example"}
    result = minimize_contact(contact)
    assert result.kept == contact
    assert result.rejected is False


# --------------------------------------------------------------------------------------------------
# Integration: discarded BEFORE persistence (never reaches storage)
# --------------------------------------------------------------------------------------------------


class _ForbiddenFieldsAdapter:
    """A synthetic adapter that emits one clean, acquirable org whose contact ALSO smuggles prohibited
    personal fields (a personal mobile mislabelled as a company line + sensitive/identifier fields)."""

    key = "synthetic.forbidden"
    trust_tier = "official_registry"

    def fetch(self, icp: dict) -> list[RawCandidate]:
        return [
            RawCandidate(
                org_name="Synthetic Textiles Pvt Ltd",
                org_identifier="U17120MH2026PTC900099",
                source_key=self.key,
                source_ref="synthetic://mca/U17120MH2026PTC900099",
                trust_tier=self.trust_tier,
                contact={
                    "kind": "company_phone",
                    "role": "CEO",
                    "phone": "+91-98765-43210",  # a personal mobile behind a company label
                    "aadhaar": "1234-5678-9012",  # unnecessary identifier
                    "age": "15",  # children/sensitive
                },
            )
        ]


def _setup(client):
    assert client.post("/v1/tenants", json={"businessId": BIZ}, headers=AUTH).status_code == 201
    assert client.post(f"/v1/tenants/{BIZ}/enable", headers=AUTH).status_code == 200
    body = {"industries": [], "regions": [], "roles": []}
    assert client.put(f"/v1/tenants/{BIZ}/icp", json=body, headers=AUTH).status_code == 200


def test_prohibited_fields_discarded_before_persistence(monkeypatch):
    monkeypatch.setattr(
        "app.services.discovery.enabled_adapters", lambda settings: [_ForbiddenFieldsAdapter()]
    )
    app = create_app(make_settings())
    with TestClient(app) as client:
        _setup(client)
        run = client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH).json()
        assert run["suggested"] == 1  # the org itself is acquirable

        candidate = client.get(f"/v1/tenants/{BIZ}/candidates", headers=AUTH).json()[0]
        contact = candidate["contact"]
        # Labels kept; every prohibited field discarded; no forbidden raw value persisted.
        assert contact == {"kind": "company_phone", "role": "CEO"}
        blob = repr(candidate)
        for forbidden in ("98765", "43210", "1234-5678-9012", "aadhaar", "age"):
            assert forbidden not in blob, forbidden


def test_clean_contact_survives_minimization_unchanged(monkeypatch):
    # Failure case for the screen's discard: a fully-clean contact loses nothing.
    class _CleanAdapter(_ForbiddenFieldsAdapter):
        key = "synthetic.clean"

        def fetch(self, icp):
            return [
                RawCandidate(
                    org_name="Demo Agro Foods Pvt Ltd",
                    org_identifier="U15400GJ2026PTC900098",
                    source_key=self.key,
                    source_ref="synthetic://mca/U15400GJ2026PTC900098",
                    trust_tier=self.trust_tier,
                    contact={"kind": "role_email", "email": "contact@demo-agro.example"},
                )
            ]

    monkeypatch.setattr(
        "app.services.discovery.enabled_adapters", lambda settings: [_CleanAdapter()]
    )
    app = create_app(make_settings())
    with TestClient(app) as client:
        _setup(client)
        client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH)
        candidate = client.get(f"/v1/tenants/{BIZ}/candidates", headers=AUTH).json()[0]
        assert candidate["contact"] == {
            "kind": "role_email",
            "email": "contact@demo-agro.example",
        }
