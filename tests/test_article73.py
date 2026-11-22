"""The SB-10407 Article 73 template in a report (Δ1 `article73_template`; D-30)."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import jsonschema_rs
import pytest

from app.modules.review.article73 import (
    DERIVABLE,
    FIELDS,
    GROUP,
    MAPPED,
    NOT_AVAILABLE,
    REVIEWER,
    SECTION,
    TemplateInputs,
    article73_template,
)
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus

SCHEMA = json.loads(
    (Path(__file__).parent.parent / "docs/pod-interface/report.schema.json").read_text("utf-8")
)
TEMPLATE_SCHEMA = jsonschema_rs.validator_for(
    {"$defs": SCHEMA["$defs"], **SCHEMA["properties"]["article73_template"]}
)
NA, CONFIRMED = HarmFieldStatus.NOT_APPLICABLE, HarmFieldStatus.CONFIRMED
EX1 = {
    HarmFieldName.HEALTH_SAFETY: NA,
    HarmFieldName.CRITICAL_INFRASTRUCTURE: NA,
    HarmFieldName.FUNDAMENTAL_RIGHTS: CONFIRMED,
    HarmFieldName.PROPERTY_ENVIRONMENT: NA,
}


def inputs(tier=DispositionTier.OTHER_SERIOUS, statuses=None):
    return TemplateInputs(
        tier=tier,
        harm_statuses=statuses or EX1,
        detected_at="2026-09-27T20:01:00Z",
        determined_at=datetime.fromisoformat("2026-09-28T00:01:00+00:00"),
        system_name="loan-scorer",
    )


def by_id(template):
    return {f["field"]: f for f in template["fields"]}


class TestTheFieldList:
    def test_ids_are_unique_and_in_template_order(self):
        ids = [f[0] for f in FIELDS]
        assert not [i for i, n in Counter(ids).items() if n > 1]
        assert ids[:3] == ["1", "1.1", "1.1.a"] and ids[-3:] == ["5", "5.comments", "5.date"]
        assert [i for i in ids if "." not in i] == ["1", "2", "3", "4", "5"]

    def test_every_lettered_field_of_the_template_is_there(self):
        """The template's lettered fields, counted from the Commission's PDF."""
        letters = Counter(i.rsplit(".", 1)[0] for i, *_ in FIELDS if i[-1].isalpha() and "." in i)
        assert letters == {
            "1.1": 2,
            "1.2": 7,
            "1.3.1": 1,
            "1.3.2": 12,
            "1.3.3": 12,
            "1.3.4": 12,
            "2.1": 1,
            "2.2": 8,
            "3.1": 1,
            "3.2": 3,
            "3.3": 11,
            "4.1": 3,
            "4.2": 3,
            "5": 2,  # comments, date
        }

    def test_contact_blocks_follow_the_template(self):
        names = {i: n for i, n, *_ in FIELDS}
        assert names["1.3.2.a"] == "Provider organisation name"
        assert names["1.3.2.l"] == "Postal code"
        assert names["3.3.a"] == "Contact’s first name" and names["3.3.k"] == "Postal code"


class TestFilled:
    def test_it_validates_against_the_schema(self):
        errors = [e.message for e in TEMPLATE_SCHEMA.iter_errors(article73_template(inputs()))]
        assert errors == []

    def test_headings_and_unfilled_fields_have_no_value(self):
        for f in article73_template(inputs())["fields"]:
            if f["status"] in (SECTION, GROUP, REVIEWER, NOT_AVAILABLE):
                assert f["value"] is None, f
            else:
                assert f["value"] is not None and f["note"], f

    def test_only_four_fields_are_filled_by_the_service(self):
        filled = {
            i: f["value"]
            for i, f in by_id(article73_template(inputs())).items()
            if f["status"] in (MAPPED, DERIVABLE)
        }
        assert filled == {
            "1.2.c": "2026-09-27T20:01:00Z",
            "1.2.d": "2026-09-28",
            "1.2.g": [
                "Infringement of obligations under Union law intended to protect fundamental rights"
            ],
            "2.2.a": "loan-scorer",
        }

    def test_death_is_classified_death(self):
        field = by_id(article73_template(inputs(DispositionTier.DEATH)))["1.2.g"]
        assert field["status"] == DERIVABLE and field["value"][0] == "Death"

    @pytest.mark.parametrize(
        "name", [HarmFieldName.CRITICAL_INFRASTRUCTURE, HarmFieldName.PROPERTY_ENVIRONMENT]
    )
    def test_an_ambiguous_harm_is_left_to_the_reviewer(self, name):
        statuses = {**EX1, name: CONFIRMED}
        field = by_id(article73_template(inputs(statuses=statuses)))["1.2.g"]
        assert field["status"] == REVIEWER and field["value"] is None
        assert name.value in field["note"]

    def test_no_confirmed_harm_leaves_it_to_the_reviewer(self):
        field = by_id(article73_template(inputs(statuses={n: NA for n in HarmFieldName})))["1.2.g"]
        assert field["status"] == REVIEWER

    def test_the_template_constants(self):
        t = article73_template(inputs())
        assert (t["template_version"], t["template_reference"]) == ("1.0.0", "SB-10407")
