"""Shared pydantic field types for API schemas."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, AwareDatetime, BeforeValidator, PlainSerializer


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _require_iso_8601(value: Any) -> Any:
    """Accept only ISO-8601 strings (Section 5.2).

    Pydantic's lax mode would otherwise read numbers, and numeric strings such as "0.0" in query
    parameters, as Unix timestamps.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("must be an ISO-8601 date-time string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be an ISO-8601 date-time string") from exc


# Decimals are stored exactly (numeric) but emitted as JSON numbers, as Section 5.2 specifies.
JsonDecimal = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]

# Timezone-aware datetime normalized to UTC (serialized as ISO-8601 with "Z").
UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]

# Input datetime: ISO-8601 string with an explicit offset (naive or numeric values rejected).
IsoAwareDatetime = Annotated[AwareDatetime, BeforeValidator(_require_iso_8601)]

# metric_name: no leading/trailing whitespace and no control characters (C0, DEL, C1). Expressed
# as a pattern so the OpenAPI document states the rule.
METRIC_NAME_PATTERN = r"^[^\s\x00-\x1f\x7f-\x9f](?:[^\x00-\x1f\x7f-\x9f]*[^\s\x00-\x1f\x7f-\x9f])?$"
