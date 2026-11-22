"""Public report and disposition ids (Δ1 `report.schema.json` patterns; D-30)."""

import re
from uuid import UUID

import pytest

from app.modules.review.ids import disposition_public_id, parse_report_id, report_public_id
from tests.test_report import (  # noqa: F401 - fixtures
    SUBMISSION,
    disposed,
    event_id,
    http,
    reviewer,
)

U = UUID("0f1e2d3c-4b5a-4697-8877-665544332211")


class TestFormat:
    def test_report_ids_match_the_schema_pattern(self):
        rid = report_public_id(U)
        assert rid == "rpt-0f1e2d3c4b5a46978877665544332211"
        assert re.fullmatch(r"rpt-[0-9a-f]{32}", rid)

    def test_disposition_ids_start_with_dsp(self):
        assert disposition_public_id(U, 2) == "dsp-0f1e2d3c4b5a46978877665544332211-2"

    @pytest.mark.parametrize("value", [report_public_id(U), str(U)])
    def test_both_report_id_forms_parse(self, value):
        assert parse_report_id(value) == U

    @pytest.mark.parametrize(
        "value",
        [
            "rpt-0F1E2D3C4B5A46978877665544332211",  # upper case: not the issued spelling
            "rpt-0f1e2d3c4b5a4697887766554433221",  # 31 digits
            "rpt-" + str(U),  # prefix on the dashed form
            str(U).upper(),
            U.hex,  # bare hex
            "dsp-0f1e2d3c4b5a46978877665544332211-1",
            "",
        ],
    )
    def test_other_spellings_name_no_report(self, value):
        assert parse_report_id(value) is None


class TestHttp:
    def test_the_version_list_and_headers_use_rpt_ids(self, http, uow, disposed):  # noqa: F811
        resp = http.get(disposed)
        listed = http.client.get(f"/v1/events/{disposed}/reports").json()
        rid = "rpt-" + uow.reports.get(disposed, 1).id.hex
        assert resp.headers["x-report-id"] == rid and listed[0]["report_id"] == rid

    def test_a_report_reads_by_either_id_form(self, http, uow, disposed):  # noqa: F811
        first = http.get(disposed)
        row = uow.reports.get(disposed, 1)
        for rid in ("rpt-" + row.id.hex, str(row.id)):
            resp = http.client.get(f"/v1/reports/{rid}")
            assert resp.status_code == 200 and resp.content == first.content

    def test_an_unissued_spelling_is_404(self, http, uow, disposed):  # noqa: F811
        http.get(disposed)
        row = uow.reports.get(disposed, 1)
        assert http.client.get(f"/v1/reports/rpt-{row.id.hex.upper()}").status_code == 404

    def test_a_submission_names_its_report_by_rpt_id(self, http, uow, disposed):  # noqa: F811
        version = http.get(disposed)
        body = {
            **SUBMISSION,
            "report_id": version.headers["x-report-id"],
            "json_sha256": version.headers["x-report-sha256"],
        }
        resp = http.submit(disposed, json=body)
        assert resp.status_code == 201, resp.text
        assert resp.json()["report_id"] == body["report_id"]


_ = (event_id, reviewer)
