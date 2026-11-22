# ruff: noqa: F811 - pytest fixtures imported from tests.test_report
"""The Δ1 report schema (`docs/pod-interface/report.schema.json`, `pod-h-report-1`; AC-13, AC-17;
D-30).

The schema is the other team's file, copied unchanged. Every report the service serves must
validate against it, and (which JSON Schema can't express) its timestamps must be in order:
detected_at < determined_at < assembled_at.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema_rs
import pytest

from app.modules.ledger.chain import GENESIS_HASH
from app.modules.review.models import DispositionTier
from app.modules.review.report_schemas import report_body
from tests.conftest import T0
from tests.fakes import ChainLedger
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
DETECTED = T0.replace(minute=1)  # the clock fixture's start, when the breach is flagged
ASSIGNED = DETECTED + timedelta(hours=4)  # the `disposed` fixture: A, OTHER_SERIOUS


def violations(document: dict) -> list[str]:
    return [f"{e.instance_path}: {e.message}" for e in VALIDATOR.iter_errors(document)]


def served(uow, ledger, clock, event, principal) -> dict:
    return json.loads(report_body(report(uow, ledger, clock, event, principal)))


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_the_schema_is_valid_draft_2020_12():
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert jsonschema_rs.meta.is_valid(SCHEMA)
    assert SCHEMA["properties"]["schema_version"] == {"const": "pod-h-report-1"}


def test_the_validator_rejects_an_empty_object():
    assert violations({})


def test_a_serious_report_validates(uow, ledger, clock, disposed, reviewer):
    assert violations(served(uow, ledger, clock, disposed, reviewer)) == []


def test_a_revised_report_validates(uow, ledger, clock, disposed, reviewer):
    """Model 5 Ex-1: OTHER_SERIOUS, then revised to DEATH three days later."""
    clock.advance(days=3)
    dispose(uow, ledger, clock, disposed, reviewer, DispositionTier.DEATH)
    assert violations(served(uow, ledger, clock, disposed, reviewer)) == []


@pytest.mark.parametrize("tier", list(DispositionTier))
def test_every_serious_tier_validates(uow, ledger, clock, event_id, reviewer, tier):
    if not tier.is_serious:
        pytest.skip("a NOT_SERIOUS event has no report (Model 5 Ex-2)")
    clock.advance(hours=1)
    dispose(uow, ledger, clock, event_id, reviewer, tier, fields=EX1_FIELDS)
    doc = served(uow, ledger, clock, event_id, reviewer)
    assert violations(doc) == []
    days = {"DEATH": 10, "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": 2, "OTHER_SERIOUS": 15}
    assert doc["regulation"]["reporting_window_days"] == days[tier.value]


def test_timestamps_are_in_order(uow, ledger, clock, disposed, reviewer):
    clock.advance(minutes=5)
    times = served(uow, ledger, clock, disposed, reviewer)["timestamps"]
    assert at(times["detected_at"]) < at(times["determined_at"]) < at(times["assembled_at"])


class TestContent:
    def test_ids_version_and_kind(self, uow, ledger, clock, disposed, reviewer):
        doc = served(uow, ledger, clock, disposed, reviewer)
        row = uow.reports.get(disposed, 1)
        assert doc["report_id"] == "rpt-" + row.id.hex
        assert (doc["report_version"], doc["report_kind"]) == (1, "SERIOUS_INCIDENT")
        assert doc["schema_version"] == "pod-h-report-1" and doc["submission_marking"] is None
        assert doc["determination"]["disposition_id"] == f"dsp-{disposed.hex}-1"

    def test_harm_fields_use_the_schema_names(self, uow, ledger, clock, disposed, reviewer):
        fields = served(uow, ledger, clock, disposed, reviewer)["harm_fields"]
        assert list(fields) == [
            "health_safety_harm",
            "critical_infrastructure_disruption",
            "fundamental_rights_infringement",
            "property_environmental_damage",
        ]
        rights = fields["fundamental_rights_infringement"]
        assert rights["status"] == "CONFIRMED"
        assert rights["evidence"] == "credit denied to applicants"
        assert rights["confirmed_by"] == str(reviewer.actor_id)
        assert fields["health_safety_harm"]["status"] == "NOT_APPLICABLE"

    def test_the_event_and_its_evidence(self, uow, ledger, clock, disposed, reviewer):
        event = served(uow, ledger, clock, disposed, reviewer)["event"]
        assert (event["event_id"], event["type"]) == (str(disposed), "threshold_breach")
        assert (event["metric_name"], event["observed_value"]) == ("accuracy", 0.61)
        assert event["threshold_value"] == 0.7
        assert event["evidence_bundle"]["window"] == {
            "start": "2026-09-27T19:00:00Z",
            "end": "2026-09-27T21:00:00Z",
        }
        assert [s["value"] for s in event["evidence_bundle"]["samples"]] == [0.61]
        assert len(event["evidence_sha256"]) == 64

    def test_determination_timestamps_and_risk(self, uow, ledger, clock, disposed, reviewer):
        """Ex-1: A at d0 13:00, revised to DEATH at d3; the report is assembled at d3 + 1 h."""
        clock.advance(days=3)
        dispose(uow, ledger, clock, disposed, reviewer, DispositionTier.DEATH)
        revised = clock.now()
        clock.advance(hours=1)
        doc = served(uow, ledger, clock, disposed, reviewer)
        d, t = doc["determination"], doc["timestamps"]
        assert (d["tier"], d["disposition_id"]) == ("DEATH", f"dsp-{disposed.hex}-2")
        assert d["tier_label"].startswith("Death")
        assert at(d["assigned_at"]) == revised and at(d["deadline_utc"]) == ASSIGNED + timedelta(
            days=10
        )
        assert d["revisions"] == [
            {
                "prior_tier": "OTHER_SERIOUS",
                "new_tier": "DEATH",
                "reviewer_id": str(reviewer.actor_id),
                "revised_at": d["assigned_at"],
            }
        ]
        assert at(t["detected_at"]) == DETECTED and at(t["determined_at"]) == ASSIGNED
        assert at(t["last_revised_at"]) == revised and at(t["assembled_at"]) == clock.now()
        assert doc["risk_indicator"] == {"detection_to_assignment_seconds": 4 * 3600}
        assert doc["regulation"]["reporting_window_days"] == 10

    def test_an_unrevised_report_has_no_revisions(self, uow, ledger, clock, disposed, reviewer):
        doc = served(uow, ledger, clock, disposed, reviewer)
        assert doc["determination"]["revisions"] == []
        assert doc["timestamps"]["last_revised_at"] is None

    def test_the_ledger_anchor_is_the_head_at_assembly(
        self, uow, ledger, clock, disposed, reviewer
    ):
        assert served(uow, ledger, clock, disposed, reviewer)["ledger_anchor"] == {
            "seq": 0,
            "hash": GENESIS_HASH,
        }
        chain = ChainLedger(uow.ledger, clock)
        chain.append(None, entry_type="VIEW", actor_id=None, payload={})
        head = uow.ledger.entries[-1]
        clock.advance(days=1)
        dispose(uow, ledger, clock, disposed, reviewer, DispositionTier.DEATH)
        anchor = served(uow, ledger, clock, disposed, reviewer)["ledger_anchor"]
        assert anchor == {"seq": head.seq, "hash": head.entry_hash}

    def test_the_article73_block(self, uow, ledger, clock, disposed, reviewer):
        block = served(uow, ledger, clock, disposed, reviewer)["article73_template"]
        assert block["template_reference"] == "SB-10407"
        fields = {f["field"]: f for f in block["fields"]}
        assert fields["2.2.a"]["value"] == "loan-scorer"
        assert at(fields["1.2.c"]["value"]) == DETECTED

    def test_nothing_is_appended_to_the_stored_text(self, uow, ledger, clock, disposed, reviewer):
        version = report(uow, ledger, clock, disposed, reviewer)
        assert report_body(version) == version.document.encode()
        assert "report_sha256" not in json.loads(version.document)
