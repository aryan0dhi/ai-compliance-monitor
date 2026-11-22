"""The Article 73 template in a report (Δ1 `article73_template`; REQ-12; D-30).

The field list is the European Commission's "Incident Report for Serious Incidents under the AI
Act (High-risk AI systems)", Reporting Template Version 1.0.0 (SB-10407), published with the
draft Article 73 guidance (consultation of 26 September 2025):
https://ec.europa.eu/newsroom/dae/redirection/document/119623 (SHA-256 of the PDF read:
20f90c54a7cd0fb952d08965947dabfa6e423515cab925d9c4b9c81364ed5a82). Every section, group and field
below is in that document, in its order, with its label. Ids are the template's own numbering
with the field letter in lower case ("1.2.c"); the two items the template leaves unnumbered
(Section 5's comments and date) are "5.comments" and "5.date".

What the service fills in is its own choice, recorded per field with the `report.schema.json`
statuses:

- `mapped`: a value the service holds, as is;
- `derivable`: a value computed from what the service holds (the `note` says how);
- `requires_reviewer_input`: the reviewer must supply it when filing the report (some are asked
  for when the submission is recorded, Δ3);
- `not_available`: the service holds nothing for it (contact and address details, product
  identifiers, the authority's own reference);
- `section` and `group`: headings, with no value.

The service never fills a field it has no source for.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus

TEMPLATE = "Incident Report for Serious Incidents under the AI Act (High-risk AI systems)"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_REFERENCE = "SB-10407"

SECTION, GROUP = "section", "group"
MAPPED, DERIVABLE = "mapped", "derivable"
REVIEWER, NOT_AVAILABLE = "requires_reviewer_input", "not_available"

_NO_CONTACTS = "the service holds no organisation, contact or address details"
_NO_PRODUCT = "the service holds no product or registration details for monitored systems"
_AT_SUBMISSION = "recorded with the submission (POST /v1/events/{event_id}/report/submission)"


def _address(prefix: str, first: str, *, offset: int = 0) -> list[tuple[str, str, str, str]]:
    """The contact block the template repeats (first field `first`, then name, email, phone,
    country and address). `offset` shifts the letters where the template starts later."""
    labels = [
        first,
        "Contact’s first name",
        "Contact’s last name",
        "Email",
        "Phone",
        "Country",
        "Street",
        "Street number",
        "Address complement",
        "PO Box",
        "City name",
        "Postal code",
    ]
    if offset:
        labels = labels[offset:]
    return [
        (f"{prefix}.{chr(ord('a') + i)}", label, NOT_AVAILABLE, _NO_CONTACTS)
        for i, label in enumerate(labels)
    ]


# (field, name, status, note). Statuses here are the defaults; `_fill` sets the computed ones.
FIELDS: list[tuple[str, str, str, str]] = [
    ("1", "Administrative information", SECTION, ""),
    (
        "1.1",
        "Responsible market surveillance authority in which country the incident occurred",
        GROUP,
        "",
    ),
    ("1.1.a", "Name of receiving market surveillance authority", REVIEWER, ""),
    (
        "1.1.b",
        "Reference number assigned by market surveillance authority for this incident",
        NOT_AVAILABLE,
        "assigned by the authority; can be recorded as authority_reference " + _AT_SUBMISSION,
    ),
    ("1.2", "Date, type, and classification of incident report", GROUP, ""),
    ("1.2.a", "Date of report submission", NOT_AVAILABLE, _AT_SUBMISSION),
    ("1.2.b", "Date of incident", REVIEWER, "a date range (from, to) in the template"),
    ("1.2.c", "Date and time of detection of the incident", MAPPED, "timestamps.detected_at"),
    ("1.2.d", "Manufacturer awareness date of reportability", DERIVABLE, ""),
    ("1.2.e", "Type of report", REVIEWER, "report_type, " + _AT_SUBMISSION),
    (
        "1.2.f",
        "In case of initial and follow-up reports, please indicate the expected date of the "
        "next report",
        REVIEWER,
        "expected_next_report_date, " + _AT_SUBMISSION,
    ),
    ("1.2.g", "Classification of serious incident", REVIEWER, ""),
    ("1.3", "Submitter information", GROUP, ""),
    ("1.3.1", "Submitter of the report", GROUP, ""),
    (
        "1.3.1.a",
        "Submitter of the report",
        REVIEWER,
        "one of: Provider, Deployer, Authorised representative, Other (please specify)",
    ),
    ("1.3.2", "Provider information", GROUP, ""),
    *_address("1.3.2", "Provider organisation name"),
    ("1.3.3", "Authorised representative information", GROUP, ""),
    *_address("1.3.3", "Authorised representative organisation name"),
    ("1.3.4", "Submitter’s details if not also provider or authorised representative", GROUP, ""),
    *_address("1.3.4", "Registered commercial name of company"),
    (
        "1.3.5",
        "Information about other incident obligations",
        REVIEWER,
        "whether, and under which regulation or law, the incident was already reported",
    ),
    ("2", "AI System information", SECTION, ""),
    ("2.1", "Categorisation of system", GROUP, ""),
    ("2.1.a", "EU Database registration id", NOT_AVAILABLE, _NO_PRODUCT),
    ("2.2", "Description of system and commercial information", GROUP, ""),
    ("2.2.a", "System name (brand/trade /proprietary or common name)", DERIVABLE, ""),
    (
        "2.2.b",
        "Description of the System and its intended purpose",
        NOT_AVAILABLE,
        _NO_PRODUCT,
    ),
    ("2.2.c", "Model", NOT_AVAILABLE, _NO_PRODUCT),
    ("2.2.d", "Catalogue/reference number", NOT_AVAILABLE, _NO_PRODUCT),
    ("2.2.e", "Serial number", NOT_AVAILABLE, _NO_PRODUCT),
    ("2.2.f", "Lot/batch number", NOT_AVAILABLE, _NO_PRODUCT),
    ("2.2.g", "Software version", NOT_AVAILABLE, _NO_PRODUCT),
    ("2.2.h", "Firmware version", NOT_AVAILABLE, _NO_PRODUCT),
    ("3", "Incident information", SECTION, ""),
    ("3.1", "Nature of incident", GROUP, ""),
    (
        "3.1.a",
        "Provide a comprehensive description of the incident, including (1) what went wrong "
        "with the system (if applicable) (2) a description of the effects (if applicable) and "
        "(3)likely causality.",
        REVIEWER,
        "the breach itself is in event; the confirmed harms in harm_fields",
    ),
    ("3.2", "AI System information", GROUP, ""),
    ("3.2.a", "Estimated number of users affected", REVIEWER, ""),
    ("3.2.b", "Operator of system at the time of the incident", REVIEWER, ""),
    ("3.2.c", "Remedial actions taken subsequent to the incident", REVIEWER, ""),
    ("3.3", "Initial reporter", GROUP, ""),
    *_address("3.3", "", offset=1),
    ("4", "Provider analysis", SECTION, ""),
    ("4.1", "Provider’s preliminary comments", GROUP, ""),
    (
        "4.1.a",
        "For initial and follow-up reports: preliminary results and conclusions of provider’s "
        "investigation",
        REVIEWER,
        "",
    ),
    (
        "4.1.b",
        "Initial actions (corrective and/or preventive) implemented by the provider",
        REVIEWER,
        "",
    ),
    (
        "4.1.c",
        "What further investigations do you intend in view of reaching final conclusions?",
        REVIEWER,
        "",
    ),
    ("4.2", "Cause investigation and conclusion", GROUP, ""),
    (
        "4.2.a",
        "For Final (Serious incident): Description of the providers’s evaluation concerning "
        "possible root causes /causative factors and conclusion",
        REVIEWER,
        "",
    ),
    (
        "4.2.b",
        "For Final (Non-reportable incident): Fill out rationale for why this is considered not "
        "reportable",
        NOT_AVAILABLE,
        "this service reports serious-tier determinations only; a NOT_SERIOUS event has no "
        "report (Phase 2 Model 5 Ex-2)",
    ),
    (
        "4.2.c",
        "Has the risk assessment been reviewed?",
        REVIEWER,
        "with the rationale if not, and whether it is still adequate if so",
    ),
    ("5", "General comments", SECTION, ""),
    ("5.comments", "General comments", REVIEWER, ""),
    ("5.date", "Date", NOT_AVAILABLE, "the date the submitter signs the affirmation"),
]

# Template 1.2.g options a confirmed harm field names unambiguously. critical_infrastructure
# (management or operation) and property_environment (property or environment) each match two.
_CLASSIFICATION: Mapping[HarmFieldName, str] = {
    HarmFieldName.HEALTH_SAFETY: "Harm to a person’s health",
    HarmFieldName.FUNDAMENTAL_RIGHTS: (
        "Infringement of obligations under Union law intended to protect fundamental rights"
    ),
}
_AMBIGUOUS = {
    HarmFieldName.CRITICAL_INFRASTRUCTURE: (
        "Disruption of the management of critical infrastructure",
        "Disruption of the operation of critical infrastructure",
    ),
    HarmFieldName.PROPERTY_ENVIRONMENT: ("Harm to property", "Harm to environment"),
}


@dataclass(frozen=True)
class TemplateInputs:
    tier: DispositionTier
    harm_statuses: Mapping[HarmFieldName, HarmFieldStatus]
    detected_at: str
    determined_at: datetime
    system_name: str


def _classification(inputs: TemplateInputs) -> tuple[str, Any, str]:
    confirmed = [
        n for n in HarmFieldName if inputs.harm_statuses.get(n) == HarmFieldStatus.CONFIRMED
    ]
    options = ["Death"] if inputs.tier == DispositionTier.DEATH else []
    unclear = [n for n in confirmed if n in _AMBIGUOUS]
    if unclear:
        candidates = [option for n in unclear for option in _AMBIGUOUS[n]]
        return (
            REVIEWER,
            None,
            "confirmed harm fields "
            + ", ".join(unclear)
            + " each match two options: "
            + "; ".join(candidates),
        )
    options += [_CLASSIFICATION[n] for n in confirmed if n in _CLASSIFICATION]
    if not options:
        return REVIEWER, None, "no confirmed harm field names a template option"
    return DERIVABLE, options, "from the tier and the CONFIRMED harm fields"


def _fill(field: str, status: str, note: str, inputs: TemplateInputs) -> tuple[str, Any, str]:
    if field == "1.2.c":
        return status, inputs.detected_at, note
    if field == "1.2.d":
        return (
            DERIVABLE,
            inputs.determined_at.date().isoformat(),
            "the date of the first serious-tier determination (timestamps.determined_at)",
        )
    if field == "1.2.g":
        return _classification(inputs)
    if field == "2.2.a":
        return DERIVABLE, inputs.system_name, "the name the system is registered under here"
    return status, None, note


def article73_template(inputs: TemplateInputs) -> dict[str, Any]:
    """The report's `article73_template` object: every SB-10407 item, in template order."""
    fields: list[dict[str, Any]] = []
    for field, name, default, default_note in FIELDS:
        status, value, note = _fill(field, default, default_note, inputs)
        item: dict[str, Any] = {"field": field, "name": name, "status": status, "value": value}
        if note:
            item["note"] = note
        fields.append(item)
    return {
        "template": TEMPLATE,
        "template_version": TEMPLATE_VERSION,
        "template_reference": TEMPLATE_REFERENCE,
        "fields": fields,
    }
