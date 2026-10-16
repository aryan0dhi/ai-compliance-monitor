"""POST /v1/metrics request and response bodies (Phase 2, Section 5.2 MetricSample)."""

import math
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, StrictFloat, StrictInt, field_validator

from app.core.types import METRIC_NAME_PATTERN, IsoAwareDatetime, JsonDecimal, UtcDatetime
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import IngestResult, MetricSubmission


class MetricSampleIn(BaseModel):
    system_id: UUID
    # No leading/trailing whitespace: "drift_score " would be a different metric that silently
    # never matches its threshold, hiding breaches (REQ-03). No control characters:
    # PostgreSQL text rejects NUL, and the rest enable log injection.
    metric_name: str = Field(min_length=1, max_length=255, pattern=METRIC_NAME_PATTERN)
    metric_type: MetricType
    # JSON number only: strings and booleans are rejected. Converted to an exact Decimal.
    value: StrictFloat | StrictInt
    # ISO-8601 with an explicit offset; normalized to UTC. Naive timestamps are rejected.
    timestamp: IsoAwareDatetime

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float | int) -> float | int:
        # Integers too large for a double would be returned as null/Infinity in responses.
        try:
            finite = math.isfinite(float(v))
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("value must be a finite number within double-precision range")
        return v

    @field_validator("timestamp")
    @classmethod
    def _to_utc(cls, v: datetime) -> datetime:
        try:
            return v.astimezone(UTC)
        except OverflowError as exc:
            raise ValueError("timestamp is out of range") from exc

    def to_submission(self) -> MetricSubmission:
        # repr() of a float is its shortest round-trip form, so 0.3 becomes Decimal("0.3"),
        # not 0.299999...; this keeps the AC-03 at-threshold boundary exact.
        value = Decimal(repr(self.value)) if isinstance(self.value, float) else Decimal(self.value)
        return MetricSubmission(
            system_id=self.system_id,
            metric_name=self.metric_name,
            metric_type=self.metric_type,
            value=value,
            sampled_at=self.timestamp,
        )


class MetricSampleOut(BaseModel):
    system_id: UUID
    metric_name: str
    metric_type: MetricType
    value: JsonDecimal
    timestamp: UtcDatetime
    # Present only when this metric breached its threshold (Section 5.1).
    flagged_event_id: UUID | None = None

    @classmethod
    def from_result(cls, result: IngestResult) -> "MetricSampleOut":
        s = result.sample
        return cls(
            system_id=s.system_id,
            metric_name=s.metric_name,
            metric_type=MetricType(s.metric_type),
            value=s.value,
            timestamp=s.sampled_at,
            flagged_event_id=result.flagged_event_id,
        )
