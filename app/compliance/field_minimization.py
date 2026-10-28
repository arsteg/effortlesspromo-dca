"""DCA-058 — reject prohibited personal field classes (the data-minimization screen).

A pure, deterministic Python screen that classifies each candidate field by its *field class* and
**discards the prohibited personal classes** before any record is persisted or handed to a model. It
is the acquisition counterpart of the platform's compliance screens: screening is data/rules, never
AI, so the decision is reproducible and auditable.

Three prohibited classes (source sections 5/6, key ``field-screen``):

- :data:`CLASS_CHILDREN_SENSITIVE` — children / sensitive-category profiling (age, date of birth,
  guardian, religion, caste, health, biometric, political/sexual orientation, …). DPDP treats these
  as off-limits for a cold B2B acquisition flow.
- :data:`CLASS_PRIVATE_MOBILE` — private mobile harvesting (a personal mobile/cell/WhatsApp/home
  number). A role **label** on the field can NOT launder it: the class is decided from the field's
  own name/token AND from the value shape (an Indian mobile number is a private mobile no matter what
  ``kind``/``role`` the adapter declared).
- :data:`CLASS_UNNECESSARY_IDENTIFIER` — personal government identifiers a B2B org-contact flow never
  needs (Aadhaar, PAN of an individual, passport, voter/EPIC, driving licence, DIN, SSN, …).

Everything else is :data:`CLASS_ALLOWED` and kept — this is a denylist of *prohibited* classes, not an
acquisition allowlist (the rulepack in :mod:`app.compliance.rulepacks` still decides, at the record
level, what may be acquired at all). The two screens compose; neither grants any sending right.

The load-bearing guarantees (the DCA-058 acceptance criteria):

- **Disallowed fields are discarded before persistence/model processing.** :func:`minimize_fields`
  returns only the kept fields; the discovery pipeline sanitises a candidate's contact/attributes
  through it before a ``ProspectCandidate`` row is ever built.
- **Metadata/logs carry no rejected raw values.** A :class:`FieldRejection` records the field *name*,
  its *class* and a stable *reason code* — never the value. :meth:`MinimizationResult.log_summary`
  yields a log-safe dict (names + classes + counts only), so an audit/log line can explain *what* was
  dropped without re-leaking the very data that was prohibited.
- **Business-role labels do not bypass classification.** Role/title tokens (``ceo``, ``director``,
  ``founder``, …) are stripped before matching, so ``director_aadhaar`` is still an unnecessary
  identifier and ``ceo_mobile`` is still a private mobile; a declared ``role``/``kind`` label is
  ignored for the purpose of classifying the data it is attached to.

Deliberately NOT an AI call and wired into nothing beyond the acquisition minimisation point — future
capabilities stay disabled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------------------------------
# Field classes + stable reason codes (auditable, test-asserted — change them deliberately).
# --------------------------------------------------------------------------------------------------

CLASS_ALLOWED = "allowed"
CLASS_CHILDREN_SENSITIVE = "children_sensitive"
CLASS_PRIVATE_MOBILE = "private_mobile"
CLASS_UNNECESSARY_IDENTIFIER = "unnecessary_identifier"

PROHIBITED_CLASSES: frozenset[str] = frozenset(
    {CLASS_CHILDREN_SENSITIVE, CLASS_PRIVATE_MOBILE, CLASS_UNNECESSARY_IDENTIFIER}
)

REASON_KEPT = "field_allowed"
_REASON_FOR_CLASS: dict[str, str] = {
    CLASS_CHILDREN_SENSITIVE: "prohibited_children_or_sensitive",
    CLASS_PRIVATE_MOBILE: "prohibited_private_mobile",
    CLASS_UNNECESSARY_IDENTIFIER: "prohibited_unnecessary_identifier",
}


def reason_for(field_class: str) -> str:
    """The stable reason code that explains a field's classification (``field_allowed`` when kept)."""
    return _REASON_FOR_CLASS.get(field_class, REASON_KEPT)


# --------------------------------------------------------------------------------------------------
# Token vocabularies (deterministic, name-based classification).
# --------------------------------------------------------------------------------------------------

# Person/role *title* tokens. These are stripped from a field name before classification so a business
# role label can never launder a prohibited class: ``director_aadhaar`` -> ``aadhaar`` (still an
# unnecessary identifier), ``ceo_mobile`` -> ``mobile`` (still a private mobile). They say WHO, not
# WHAT, so they must not be allowed to change the class of the data attached to them.
_ROLE_LABEL_TOKENS: frozenset[str] = frozenset(
    {
        "ceo", "cto", "cfo", "coo", "cmo", "md", "vp", "svp", "evp",
        "founder", "cofounder", "director", "owner", "partner", "proprietor",
        "promoter", "principal", "manager", "head", "lead", "chief", "president",
        "contact", "person", "employee", "staff", "primary", "secondary",
    }
)  # fmt: skip

# Children / sensitive-category profiling tokens (DPDP "children" + "sensitive" categories).
_CHILDREN_SENSITIVE_TOKENS: frozenset[str] = frozenset(
    {
        "age", "dob", "birth", "birthday", "birthdate", "minor", "child", "children",
        "guardian", "parent", "school", "student",
        "religion", "religious", "caste", "ethnicity", "ethnic", "race",
        "health", "medical", "disability", "disabled", "illness", "diagnosis",
        "blood", "bloodgroup", "pregnancy", "pregnant", "genetic", "biometric", "fingerprint",
        "sexual", "sexuality", "orientation", "gender",
        "political", "party",
    }
)  # fmt: skip

# Personal government identifiers an org-contact flow never needs (GSTIN/CIN are org identifiers and
# are NOT here — they are legitimate company facts carried elsewhere).
_UNNECESSARY_ID_TOKENS: frozenset[str] = frozenset(
    {
        "aadhaar", "aadhar", "uidai", "uid",
        "pan", "passport", "voter", "epic",
        "license", "licence", "dl", "ssn", "din",
        "nationalid", "nid",
    }
)  # fmt: skip

# Personal (non-company) phone qualifiers — a phone-ish field carrying any of these is a private
# mobile regardless of a declared company label. ``mobile``/``cell``/``whatsapp`` are personal by
# nature in a cold B2B flow (there is no allowed "company mobile" class in the IN rulepack).
_PRIVATE_PHONE_TOKENS: frozenset[str] = frozenset(
    {"mobile", "cell", "cellphone", "whatsapp", "msisdn", "personal", "home", "residence", "private"}
)  # fmt: skip

# Tokens that mark a field as phone-ish at all (so the value-shape check knows to look for a mobile).
_PHONE_FIELD_TOKENS: frozenset[str] = frozenset(
    {"phone", "telephone", "tel", "number", "contact", "line"} | _PRIVATE_PHONE_TOKENS
)

# "Company line" qualifiers that keep a non-mobile phone allowed (office landline, reception, …).
_COMPANY_PHONE_TOKENS: frozenset[str] = frozenset(
    {"company", "office", "landline", "work", "reception", "switchboard", "corporate", "business"}
)

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _tokens(name: str) -> list[str]:
    """Normalise a field name into lowercase alphanumeric tokens (splits snake_case, camelCase, and
    separators), with role/title tokens removed so they cannot mask a prohibited class."""
    # camelCase -> camel_Case boundary before lowercasing, so ``ceoMobile`` splits like ``ceo_mobile``.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    raw = [t for t in _TOKEN_SPLIT_RE.split(spaced.lower()) if t]
    return [t for t in raw if t not in _ROLE_LABEL_TOKENS]


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value)) if value is not None else ""


def is_indian_mobile(value: Any) -> bool:
    """True when ``value`` is shaped like an Indian mobile number (10 digits starting 6-9, with an
    optional ``91``/``0`` trunk/country prefix). Deterministic and value-only — a mobile is a private
    mobile no matter how the adapter labelled the field it arrived in."""
    digits = _digits(value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return len(digits) == 10 and digits[0] in "6789"


def classify_field(name: str, value: Any = None) -> str:
    """Classify one field into an allowed/prohibited class from its name and (optionally) its value.

    Order matters: a sensitive/identifier name wins first; then a personal-phone name; then the
    value-shape check promotes any phone-ish field holding an Indian mobile to :data:`CLASS_PRIVATE_MOBILE`
    — so a personal mobile declared as a company line is still rejected. Unqualified company phones and
    every other field stay :data:`CLASS_ALLOWED`."""
    tokens = set(_tokens(name))

    if tokens & _CHILDREN_SENSITIVE_TOKENS:
        return CLASS_CHILDREN_SENSITIVE
    if tokens & _UNNECESSARY_ID_TOKENS:
        return CLASS_UNNECESSARY_IDENTIFIER

    is_phone_field = bool(tokens & _PHONE_FIELD_TOKENS)
    # A personal-phone qualifier ON the field name -> private mobile outright.
    if tokens & _PRIVATE_PHONE_TOKENS:
        return CLASS_PRIVATE_MOBILE
    # A mobile-shaped VALUE in any phone-ish field -> private mobile, even if labelled a company line.
    if is_phone_field and is_indian_mobile(value):
        return CLASS_PRIVATE_MOBILE

    return CLASS_ALLOWED


# --------------------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRejection:
    """A discarded field: its NAME, its prohibited CLASS and a stable REASON — never its raw value."""

    name: str
    field_class: str
    reason_code: str


@dataclass(frozen=True)
class MinimizationResult:
    """The outcome of screening a set of candidate fields.

    ``kept`` is the minimised mapping safe to persist / hand to a model. ``rejections`` explains each
    discarded field WITHOUT its value, so a log/audit line can be honest about what was dropped without
    re-leaking the prohibited data.
    """

    kept: dict[str, Any] = field(default_factory=dict)
    rejections: tuple[FieldRejection, ...] = ()

    @property
    def rejected(self) -> bool:
        return bool(self.rejections)

    def rejected_names(self) -> list[str]:
        return [r.name for r in self.rejections]

    def log_summary(self) -> dict[str, Any]:
        """A log-safe summary: field NAMES and CLASSES and counts only — no raw values ever."""
        return {
            "kept_fields": sorted(self.kept),
            "rejected_count": len(self.rejections),
            "rejected": [
                {"field": r.name, "class": r.field_class, "reason": r.reason_code}
                for r in self.rejections
            ],
        }


def minimize_fields(fields: Mapping[str, Any]) -> MinimizationResult:
    """Screen a flat mapping of candidate fields: keep the allowed ones, discard the prohibited classes.

    Pure and deterministic — no AI, no I/O, no mutation of the input. The returned ``kept`` mapping is
    what the caller should persist / process; ``rejections`` carries names+classes only (no values).
    """
    kept: dict[str, Any] = {}
    rejections: list[FieldRejection] = []
    for name, value in fields.items():
        field_class = classify_field(name, value)
        if field_class in PROHIBITED_CLASSES:
            rejections.append(FieldRejection(name, field_class, reason_for(field_class)))
        else:
            kept[name] = value
    return MinimizationResult(kept=kept, rejections=tuple(rejections))


# Contact sub-keys that are structural LABELS (who/what-kind), not harvested personal data. They are
# never persisted as contact payload to a model; the real data lives in ``email``/``phone`` etc., which
# ARE classified. Keeping the labels lets the record-level rulepack screen still see ``kind``.
_CONTACT_LABEL_KEYS: frozenset[str] = frozenset({"kind", "role", "title", "label", "type"})


def minimize_contact(contact: Mapping[str, Any]) -> MinimizationResult:
    """Minimise a candidate ``contact`` dict (``{kind, role?, email?, phone?, …}``).

    Structural label keys (``kind``/``role``/…) are preserved as-is — they are not harvested personal
    data and the record-level rulepack still reads ``kind`` — but every *data* sub-field is classified,
    so a private mobile carried under ``phone`` with ``role="CEO"``/``kind="company_phone"`` is still
    discarded: the business-role label does not bypass classification.
    """
    data_fields = {k: v for k, v in contact.items() if k.lower() not in _CONTACT_LABEL_KEYS}
    result = minimize_fields(data_fields)
    kept = {k: v for k, v in contact.items() if k.lower() in _CONTACT_LABEL_KEYS}
    kept.update(result.kept)
    return MinimizationResult(kept=kept, rejections=result.rejections)
