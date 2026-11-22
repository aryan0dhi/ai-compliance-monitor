"""The Δ1 report schema (`docs/pod-interface/report.schema.json`, `pod-h-report-1`; AC-13; D-30).

The schema is the other team's file, copied unchanged. Every report the service serves must
validate against it, and (which JSON Schema can't express) its timestamps must be in order:
detected_at < determined_at < assembled_at.
"""

import json
from datetime import datetime
from pathlib import Path

import jsonschema_rs
import pytest

from app.modules.review.models import DispositionTier
from app.modules.review.report_schemas import report_body
from tests.test_report import (  # noqa: F401 - fixtures
    EX1_FIELDS,
    dispose,
    disposed,
    event_id,
    report,
    reviewer,
)

SCHEMA_PATH = Path(__file__).parent.parent / "docs" / "pod-interface" / "report.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = jsonschema_rs.validator_for(SCHEMA)

# Until the report document follows the schema (D-30), the served report fails it. strict=True
# makes the test fail as soon as it passes, so this marker can't outlive the gap.
NOT_YET = pytest.mark.xfail(
    strict=True, reason="the served report is still the D-25 document (D-30)"
)


def violations(document: dict) -> list[str]:
    return [f"{e.instance_path}: {e.message}" for e in VALIDATOR.iter_errors(document)]


def served(uow, ledger, clock, event, principal) -> dict:
    return json.loads(report_body(report(uow, ledger, clock, event, principal)))


def test_the_schema_is_valid_draft_2020_12():
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert jsonschema_rs.meta.is_valid(SCHEMA)
    assert SCHEMA["properties"]["schema_version"] == {"const": "pod-h-report-1"}


def test_the_validator_rejects_an_empty_object():
    assert violations({})


@NOT_YET
def test_a_serious_report_validates(uow, ledger, clock, disposed, reviewer):  # noqa: F811
    assert violations(served(uow, ledger, clock, disposed, reviewer)) == []


@NOT_YET
def test_a_revised_report_validates(uow, ledger, clock, disposed, reviewer):  # noqa: F811
    """Model 5 Ex-1: OTHER_SERIOUS, then revised to DEATH three days later."""
    clock.advance(days=3)
    dispose(uow, ledger, clock, disposed, reviewer, DispositionTier.DEATH)
    assert violations(served(uow, ledger, clock, disposed, reviewer)) == []


@NOT_YET
def test_timestamps_are_in_order(uow, ledger, clock, disposed, reviewer):  # noqa: F811
    clock.advance(minutes=5)
    times = served(uow, ledger, clock, disposed, reviewer)["timestamps"]
    at = {name: datetime.fromisoformat(times[name]) for name in times if times[name]}
    assert at["detected_at"] < at["determined_at"] < at["assembled_at"]


_ = (EX1_FIELDS, event_id)
