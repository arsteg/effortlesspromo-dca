"""Synthetic India registry fixtures — local, deterministic, obviously fake (story rule: "use local
synthetic fixtures; never auto-approve production policies/sources"). Shaped like MCA/GSTIN records
so the pipeline, screening, and UI stories exercise realistic data without touching any live source.
"""

from __future__ import annotations

from app.compliance.rulepacks import RawCandidate

KEY = "synthetic.mca"
TRUST_TIER = "official_registry"

_FIXTURES: list[dict] = [
    {
        "org_name": "Synthetic Textiles Pvt Ltd",
        "org_identifier": "U17120MH2026PTC900001",
        "industry": "textiles",
        "region": "MH",
        "contact": {"kind": "role_email", "email": "info@synthetic-textiles.example"},
    },
    {
        "org_name": "Demo Agro Foods Pvt Ltd",
        "org_identifier": "U15400GJ2026PTC900002",
        "industry": "food-processing",
        "region": "GJ",
        "contact": {"kind": "role_email", "email": "contact@demo-agro.example"},
    },
    {
        "org_name": "Sample Software Services LLP",
        "org_identifier": "AAB-9990",
        "industry": "software",
        "region": "KA",
        "contact": {"kind": "company_phone", "phone": "+91-00000-00000"},
    },
    {
        "org_name": "Placeholder Pharma Ltd",
        "org_identifier": "L24230TN2026PLC900004",
        "industry": "pharma",
        "region": "TN",
        # Named-person email — MUST be screened out by the IN rulepack (contact_kind_disallowed).
        "contact": {"kind": "personal_email", "email": "a.person@placeholder-pharma.example"},
    },
    {
        "org_name": "Nameless Trading Co",
        # Missing registry identifier — MUST be screened out (missing_org_identifier).
        "org_identifier": "",
        "industry": "trading",
        "region": "MH",
        "contact": {"kind": "role_email", "email": "sales@nameless-trading.example"},
    },
]


class SyntheticIndiaRegistry:
    key = KEY
    trust_tier = TRUST_TIER

    def fetch(self, icp: dict) -> list[RawCandidate]:
        industries = {str(i).lower() for i in icp.get("industries") or []}
        regions = {str(r).upper() for r in icp.get("regions") or []}
        out: list[RawCandidate] = []
        for row in _FIXTURES:
            if industries and row["industry"] not in industries:
                continue
            if regions and row["region"] not in regions:
                continue
            out.append(
                RawCandidate(
                    org_name=row["org_name"],
                    org_identifier=row["org_identifier"],
                    source_key=self.key,
                    source_ref=f"synthetic://mca/{row['org_identifier'] or 'unidentified'}",
                    trust_tier=self.trust_tier,
                    contact=dict(row["contact"]),
                )
            )
        return out
