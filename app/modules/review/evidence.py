"""The evidence bundle of a report's event (Δ1 `event.evidence_bundle`, `evidence_sha256`; REQ-05,
Model 2; D-05, D-30).

Phase 2 Model 2 defines the evidence bundle as the samples within ±1 h of the breach. D-05 (still
open) is about when the *event's* `evidence_bundle_hash` is computed, since the hour after the
breach doesn't exist yet when the event is flagged. A report is assembled later, so it uses the
Model 2 window directly:

- the window is centred on the triggering sample's `sampled_at` (the event's `metric_timestamp`)
  and starts one hour before it;
- it ends one hour after it, or at assembly if that is earlier (never before the breach), so a
  report never describes samples that couldn't have arrived yet;
- the samples are every stored sample of the same system and metric in the window, bounds
  included, oldest first. The triggering sample can't be deleted while the event exists
  (`fk_event_triggering_sample`), so there is always at least one.

`evidence_sha256` is the SHA-256 of the bundle as canonical JSON (sorted keys, no whitespace,
ASCII), the same canonical form as ledger entry hashes. The event's own `evidence_bundle_hash`
column stays null until D-05 is decided.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.modules.metrics.repository import MetricRepository

EVIDENCE_HALF_WINDOW = timedelta(hours=1)


def format_utc(at: datetime) -> str:
    """ISO-8601 in UTC with `Z`, as the API serializes datetimes (`report.schema.json`
    timestamp pattern)."""
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class EvidenceBundle:
    window_start: datetime
    window_end: datetime
    samples: list[dict[str, Any]]

    def as_json(self) -> dict[str, Any]:
        return {
            "window": {"start": format_utc(self.window_start), "end": format_utc(self.window_end)},
            "samples": self.samples,
        }

    @property
    def sha256(self) -> str:
        canonical = json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evidence_window(breach_at: datetime, assembled_at: datetime) -> tuple[datetime, datetime]:
    end = max(breach_at, min(breach_at + EVIDENCE_HALF_WINDOW, assembled_at))
    return breach_at - EVIDENCE_HALF_WINDOW, end


def build_evidence_bundle(
    metrics: MetricRepository,
    *,
    system_id: UUID,
    metric_name: str,
    breach_at: datetime,
    assembled_at: datetime,
) -> EvidenceBundle:
    start, end = evidence_window(breach_at, assembled_at)
    rows = metrics.in_window(system_id, metric_name, start, end)
    if not any(r.sampled_at == breach_at for r in rows):
        raise ValueError("the triggering sample is missing from its evidence window")
    samples = [
        {
            "sampled_at": format_utc(r.sampled_at),
            "value": float(r.value),
            "snapshot_ref": r.snapshot_ref,
        }
        for r in rows
    ]
    return EvidenceBundle(window_start=start, window_end=end, samples=samples)
