"""GET /v1/events/{event_id}/report?format=pdf (contract delta Δ2; REQ-12; D-30).

The PDF is the same stored report as format=json: every value under its JSON path, the exact
JSON attached, Unicode shown faithfully, the same bytes on every request.
"""

# ruff: noqa: F811 - pytest fixtures imported from tests.test_report

import hashlib
import io
import json
import re
from datetime import UTC, datetime

import pytest
from pypdf import PdfReader

from app.modules.ledger.interface import LedgerEntryType
from app.modules.records.service import erase_expired
from app.modules.review.disposition import FieldConfirmation
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus
from app.modules.review.report_pdf import leaves, printable, render_report_pdf
from tests.fakes import user_principal
from tests.test_report import (  # noqa: F401 - fixtures
    EX1_FIELDS,
    dispose,
    disposed,
    event_id,
    http,
    reviewer,
)

PDF = {"format": "pdf"}


def text_of(pdf: bytes) -> str:
    return "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def squashed(text: str) -> str:
    """Wrapping breaks lines anywhere, so compare without whitespace."""
    return re.sub(r"\s+", "", text)


def attachment(pdf: bytes) -> bytes:
    (content,) = PdfReader(io.BytesIO(pdf)).attachments["report.json"]
    return content


def get_pdf(http, event):
    return http.client.get(f"/v1/events/{event}/report", params=PDF)


class TestHttp:
    def test_pdf_of_the_same_report(self, http, disposed):
        as_json = http.get(disposed)
        resp = get_pdf(http, disposed)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")
        # The report's identity is the same; the ETag is this representation's own.
        for name in ("x-report-id", "x-report-version", "x-report-sha256"):
            assert resp.headers[name] == as_json.headers[name], name
        assert resp.headers["etag"] == f'"{hashlib.sha256(resp.content).hexdigest()}"'
        assert resp.headers["etag"] != as_json.headers["etag"]
        rid = as_json.headers["x-report-id"]
        assert resp.headers["content-disposition"] == f'inline; filename="{rid}.pdf"'

    def test_the_exact_json_is_attached(self, http, disposed):
        as_json = http.get(disposed)
        assert attachment(get_pdf(http, disposed).content) == as_json.content

    def test_every_value_is_printed_under_its_path(self, http, disposed):
        document = http.get(disposed).json()
        printed = squashed(text_of(get_pdf(http, disposed).content))
        missing = [
            f"{path}: {value}"
            for path, value in leaves(document)
            if squashed(printable(f"{path}: {value}")) not in printed
        ]
        assert missing == []
        assert len(list(leaves(document))) > 100  # the whole report, template included

    def test_the_same_bytes_on_every_request(self, http, clock, disposed):
        first = get_pdf(http, disposed)
        clock.advance(days=2)
        assert get_pdf(http, disposed).content == first.content

    def test_a_first_pdf_request_assembles_the_report(self, http, uow, ledger, disposed):
        """Like JSON: the first request assembles and stores the report (B-14 ledger entries)."""
        assert get_pdf(http, disposed).status_code == 200
        assert len(uow.reports.rows) == 1
        http.get(disposed)
        assert [c["entry_type"] for c in ledger.calls] == [
            LedgerEntryType.REPORT_ASSEMBLED,
            LedgerEntryType.VIEW,
        ]
        assert attachment(get_pdf(http, disposed).content) == http.get(disposed).content

    def test_a_revision_gets_its_own_pdf(self, http, uow, ledger, clock, disposed, reviewer):
        first = get_pdf(http, disposed)
        clock.advance(days=3)
        dispose(uow, ledger, clock, disposed, reviewer, DispositionTier.DEATH)
        second = get_pdf(http, disposed)
        assert second.headers["x-report-version"] == "2" and second.content != first.content
        assert json.loads(attachment(second.content))["determination"]["tier"] == "DEATH"


class TestRefusals:
    """format=pdf is refused exactly like format=json."""

    def test_no_disposition_is_409(self, http, event_id):
        resp = get_pdf(http, event_id)
        assert resp.status_code == 409 and resp.json()["error_code"] == "NO_DISPOSITION"

    def test_not_serious_is_409(self, http, uow, ledger, clock, event_id, reviewer):
        """Phase 2 Model 5 Ex-2: no report, so no PDF."""
        all_na = {n: FieldConfirmation(HarmFieldStatus.NOT_APPLICABLE) for n in HarmFieldName}
        dispose(uow, ledger, clock, event_id, reviewer, DispositionTier.NOT_SERIOUS,
                rationale="no impact", fields=all_na)  # fmt: skip
        assert get_pdf(http, event_id).status_code == 409

    def test_after_erasure_is_410(self, http, uow, ledger, clock, disposed):
        get_pdf(http, disposed)
        clock.advance(days=400)
        erase_expired(lambda: uow, ledger=ledger, clock=clock)
        resp = get_pdf(http, disposed)
        assert resp.status_code == 410 and resp.json()["error_code"] == "RECORD_ERASED"

    @pytest.mark.parametrize("role", ["VIEWER", "ADMIN"])
    def test_non_reviewers_are_403(self, http, disposed, role):
        from app.modules.auth.interface import Role

        http.principal = user_principal(Role[role])
        assert get_pdf(http, disposed).status_code == 403


class TestUnicode:
    def test_reviewer_text_in_any_script(self, uow, ledger, clock, event_id, reviewer, http):
        evidence = "Zoë’s loan — Δ risk, Москва; 中文 (tab\there)"
        fields = {**EX1_FIELDS, HarmFieldName.FUNDAMENTAL_RIGHTS: FieldConfirmation(
            HarmFieldStatus.CONFIRMED, evidence)}  # fmt: skip
        clock.advance(hours=1)
        dispose(uow, ledger, clock, event_id, reviewer, DispositionTier.OTHER_SERIOUS,
                fields=fields)  # fmt: skip
        pdf = get_pdf(http, event_id).content
        printed = text_of(pdf)
        # Covered scripts print as themselves; CJK (no glyph in the font) as code points.
        assert "Zoë’s loan — Δ risk, Москва;" in printed
        assert "[U+4E2D][U+6587]" in printed
        # The attachment keeps the exact text, tab included.
        stored = json.loads(attachment(pdf))
        assert stored["harm_fields"]["fundamental_rights_infringement"]["evidence"] == evidence

    def test_printable(self):
        assert printable("a\tb\nc") == "a b c"
        assert printable("Δ中") == "Δ[U+4E2D]"
        assert printable("\U0001f600") in ("\U0001f600", "[U+1F600]")


class TestRendering:
    DOC = json.dumps({"report_id": "rpt-" + "0" * 32, "event": {"samples": [], "x": {}}, "n": None})
    AT = datetime(2026, 9, 28, tzinfo=UTC)

    def test_deterministic(self):
        a = render_report_pdf(self.DOC, report_id="rpt-" + "0" * 32, assembled_at=self.AT)
        assert a == render_report_pdf(self.DOC, report_id="rpt-" + "0" * 32, assembled_at=self.AT)

    def test_empty_containers_and_null_are_printed(self):
        assert list(leaves(json.loads(self.DOC))) == [
            ("report_id", "rpt-" + "0" * 32),
            ("event.samples", "[]"),
            ("event.x", "{}"),
            ("n", "null"),
        ]

    def test_a_report_stored_before_d30_renders(self):
        """The D-25 shape has no report_id, timestamps or schema_version keys."""
        legacy = json.dumps({"event": {"event_id": "e"}, "detected_at": "2026-09-27T20:01:00Z"})
        pdf = render_report_pdf(legacy, report_id="rpt-" + "1" * 32, assembled_at=self.AT)
        assert "detected_at: 2026-09-27T20:01:00Z" in text_of(pdf)
        assert attachment(pdf) == legacy.encode()
