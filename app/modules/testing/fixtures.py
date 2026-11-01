"""Baseline fixtures reseeded by POST /v1/test/reset.

Read from docs/pod-interface/fixtures.json (B-20), the single copy that the docs, the contract
suite and other teams share. Proposed, not pod-agreed (docs/spec-decisions.md D-16).
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.config import get_settings
from app.modules.metrics.models import MetricType
from app.modules.thresholds.models import Direction

# Thresholds apply to every sample timestamped after this.
FIXTURE_VALID_FROM = datetime(2000, 1, 1, tzinfo=UTC)


class FixtureError(ValueError):
    """The fixtures file is missing or malformed."""


@dataclass(frozen=True)
class FixtureThreshold:
    metric_name: str
    metric_type: MetricType
    direction: Direction
    limit_value: Decimal


@dataclass(frozen=True)
class FixtureSystem:
    id: UUID
    name: str
    sampling_interval_s: int
    gap_missed_intervals: int
    thresholds: tuple[FixtureThreshold, ...]


def _parse_threshold(raw: dict[str, Any]) -> FixtureThreshold:
    return FixtureThreshold(
        metric_name=raw["metric_name"],
        metric_type=MetricType(raw["metric_type"]),
        direction=Direction(raw["direction"]),
        limit_value=Decimal(raw["limit"]),
    )


def parse_fixtures(data: dict[str, Any]) -> tuple[FixtureSystem, ...]:
    try:
        if data["valid_from"] != "2000-01-01T00:00:00Z":
            raise FixtureError("valid_from must be 2000-01-01T00:00:00Z")
        systems = tuple(
            FixtureSystem(
                id=UUID(s["id"]),
                name=str(s["name"]),
                sampling_interval_s=int(s["sampling_interval_s"]),
                gap_missed_intervals=int(s["gap_missed_intervals"]),
                thresholds=tuple(_parse_threshold(t) for t in s["thresholds"]),
            )
            for s in data["systems"]
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        if isinstance(exc, FixtureError):
            raise
        raise FixtureError(f"invalid fixtures file: {type(exc).__name__}: {exc}") from exc
    if not systems or len({s.id for s in systems}) != len(systems):
        raise FixtureError("fixtures need at least one system, each with a distinct id")
    return systems


@lru_cache
def baseline_systems() -> tuple[FixtureSystem, ...]:
    """The systems in the fixtures file (FIXTURES_PATH). Loaded on first use, then cached."""
    path = Path(get_settings().fixtures_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FixtureError(f"cannot read fixtures file {path}: {type(exc).__name__}") from exc
    return parse_fixtures(data)
