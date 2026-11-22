"""Public report and disposition ids (contract delta Δ1 `report.schema.json`; D-30).

The schema requires `report_id` to match `^rpt-[0-9a-f]{32}$` and `disposition_id` to start with
`dsp-`. Both are derived from keys we already store, so no migration is needed:

- a report's id is `rpt-` followed by the 32 hex digits of its UUID (`report.id`);
- a disposition's id is `dsp-<event UUID hex>-<seq>`, from its primary key `(event_id, seq)`.

Clients treat both as opaque strings. Report ids are accepted in either the `rpt-` form or the
UUID form served before D-30, so ids that clients already hold keep working.
"""

import re
from uuid import UUID

from app.core.types import to_internal_id

REPORT_ID_PREFIX = "rpt-"
DISPOSITION_ID_PREFIX = "dsp-"
_REPORT_ID = re.compile(r"rpt-([0-9a-f]{32})")


def report_public_id(report_id: UUID) -> str:
    return REPORT_ID_PREFIX + report_id.hex


def parse_report_id(value: str) -> UUID | None:
    """The report UUID that `value` names, or None. Accepts `rpt-<32 lowercase hex>` and the
    canonical UUID spelling served before D-30; any other spelling names no report."""
    match = _REPORT_ID.fullmatch(value)
    if match is not None:
        return UUID(hex=match.group(1))
    return to_internal_id(value)


def disposition_public_id(event_id: UUID, seq: int) -> str:
    return f"{DISPOSITION_ID_PREFIX}{event_id.hex}-{seq}"
