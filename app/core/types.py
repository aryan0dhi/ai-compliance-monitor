"""Shared pydantic field types for API schemas."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Decimals are stored exactly (numeric) but emitted as JSON numbers, as Section 5.2 specifies.
JsonDecimal = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]

# Timezone-aware datetime normalized to UTC (serialized as ISO-8601 with "Z").
UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]
