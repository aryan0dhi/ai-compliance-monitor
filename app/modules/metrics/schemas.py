"""POST /v1/metrics request and response bodies (Phase 2, Section 5.2 MetricSample)."""

import math
from decimal import Decimal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, StrictFloat, StrictInt, field_validator

from app.core.types import JsonDecimal, UtcDatetime
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import IngestResult, MetricSubmission


class MetricSampleIn(BaseModel):
    system_id: UUID
    metric_name: str = Field(min_length=1, max_length=255)
    metric_type: MetricType
    # JSON number only: strings and booleans are rejected. Converted to an exact Decimal.
    value: StrictFloat | StrictInt
    # ISO-8601 with an explicit offset; normalized to UTC. Naive timestamps are rejected.
    timestamp: AwareDatetime

    @field_validator("metric_name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("metric_name must not be blank")
        return v

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float | int) -> float | int:
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("value must be a finite number")
        return v

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
