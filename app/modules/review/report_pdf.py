"""The PDF of a stored report: `GET /v1/events/{event_id}/report?format=pdf` (contract delta Δ2;
REQ-12; D-30).

The PDF is rendered from the stored `pod-h-report-1` JSON text, the same bytes `format=json`
serves, so both formats always carry the same report:

- every value of the JSON document is printed under its JSON path (`determination.tier`,
  `article73_template.fields[3].status`, ...), in document order, with nothing left out or
  added. JSON null, true and false print as `null`, `true` and `false`;
- the exact JSON text is embedded in the PDF as the attachment `report.json`, and the footer of
  every page gives its SHA-256 (the report's `X-Report-Sha256`);
- text is set in DejaVu Sans (embedded; Bitstream Vera licence, `app/assets/fonts`), which
  covers Latin, Greek, Cyrillic and common symbols. A character the font has no glyph for
  prints as `[U+XXXX]`, never as a blank or a substitute glyph; the attachment keeps it exactly.

Rendering is deterministic: the PDF's dates are the report's `assembled_at`, its file id is
derived from the report's hash, and the font and library versions are pinned, so the same stored
report gives the same PDF bytes on every request. The PDF is not stored separately: it holds
nothing the sealed JSON doesn't, and erasing the event's key (REQ-16) makes both unavailable.
"""

import functools
import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
from fpdf import FPDF
from fpdf.enums import WrapMode, XPos, YPos

FONT_PATH = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "DejaVuSans.ttf"
FONT = "DejaVuSans"
ATTACHMENT = "report.json"
TITLE = "Article 73 serious incident report"


@functools.cache
def _covered() -> frozenset[int]:
    """The code points the embedded font has glyphs for."""
    return frozenset(TTFont(FONT_PATH, lazy=True).getBestCmap())


def printable(text: str) -> str:
    """`text` with every character the font can't draw replaced by `[U+XXXX]`. Line breaks and
    tabs become spaces (each value is printed on its own lines)."""
    covered = _covered()
    out = []
    for ch in text:
        if ch in "\n\r\t":
            out.append(" ")
        elif ord(ch) in covered:
            out.append(ch)
        else:
            out.append(f"[U+{ord(ch):04X}]")
    return "".join(out)


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)  # null, true, false, numbers


def leaves(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """(JSON path, printed value) for every value in the document, in document order. An empty
    object or list is a leaf of its own (`{}`, `[]`), so nothing disappears."""
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from leaves(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list) and value:
        for i, item in enumerate(value):
            yield from leaves(item, f"{path}[{i}]")
    elif isinstance(value, dict | list):
        yield path, json.dumps(value)
    else:
        yield path, _scalar(value)


class _ReportPdf(FPDF):
    def __init__(self, footer_text: str) -> None:
        super().__init__(format="A4")
        self._footer_text = footer_text

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(FONT, size=7)
        self.cell(0, 4, f"{self._footer_text}   Page {self.page_no()}/{{nb}}", align="C")


def render_report_pdf(document: str, *, report_id: str, assembled_at: datetime) -> bytes:
    """The PDF of a stored report document (its JSON text, as served). `report_id` and
    `assembled_at` come from the stored row, so a report stored before D-30 (the D-25 shape)
    renders too."""
    report = json.loads(document)
    sha = hashlib.sha256(document.encode("utf-8")).hexdigest()

    pdf = _ReportPdf(f"{report_id}  ·  SHA-256 of report.json {sha}")
    pdf.set_creation_date(assembled_at)
    pdf.file_id = lambda: f"<{sha[:32]}><{sha[:32]}>"  # type: ignore[method-assign]
    pdf.set_title(f"{TITLE} {report_id}")
    pdf.set_subject(f"report {report_id}, schema {report.get('schema_version', 'D-25')}")
    pdf.set_lang("en")
    pdf.add_font(FONT, fname=str(FONT_PATH))
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    pdf.set_font(FONT, size=15)
    pdf.multi_cell(0, 8, TITLE, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(FONT, size=9)
    marking = report.get("submission_marking")
    intro = [
        f"Report {report_id}, version {report.get('report_version', '-')} "
        f"({report.get('report_kind', 'SERIOUS_INCIDENT')}, "
        f"schema {report.get('schema_version', 'D-25, before pod-h-report-1')}).",
        "Every value of the JSON report follows, under its JSON path. The exact JSON is "
        f"attached to this PDF as {ATTACHMENT}. Characters the embedded font can't draw are "
        "shown as [U+XXXX].",
    ]
    if marking:
        intro.insert(0, str(marking))
    for line in intro:
        pdf.multi_cell(0, 5, printable(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    for section, value in report.items():
        if isinstance(value, dict | list):  # a heading for each object section
            pdf.set_font(FONT, size=11)
            pdf.multi_cell(0, 7, section, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font(FONT, size=8)
        for path, text in leaves(value, section):
            pdf.multi_cell(
                0,
                4.2,
                printable(f"{path}: {text}"),
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
                wrapmode=WrapMode.CHAR,
                align="L",
            )
        if isinstance(value, dict | list):
            pdf.ln(2)

    pdf.embed_file(
        bytes=document.encode("utf-8"),
        basename=ATTACHMENT,
        modification_date=assembled_at,
        mime_type="application/json",
        desc="The report exactly as served by format=json",
    )
    return bytes(pdf.output())
