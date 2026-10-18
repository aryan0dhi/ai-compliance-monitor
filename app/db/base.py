"""Declarative base shared by every module's models."""

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, MetaData, Numeric, Text
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names so Alembic autogenerate produces stable, reviewable migrations
# across all four developers' branches.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Project-wide column type defaults:
    # - every datetime is timezone-aware (timestamptz) and stored in UTC
    # - every Decimal is unbounded `numeric`, so threshold comparisons are exact (AC-03 boundary)
    # - every str is `text`
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        Decimal: Numeric(),
        str: Text(),
    }


def check_in(column: str, values: Iterable[StrEnum | str], name: str) -> CheckConstraint:
    """CHECK constraint restricting a text column to a fixed set of values.

    Used instead of native PostgreSQL ENUM types because the value sets (event states in
    particular, see docs/spec-decisions.md D-03) are expected to change, and altering a CHECK is a
    one-line migration.
    """
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def check_not_nan(column: str, name: str) -> CheckConstraint:
    """PostgreSQL `numeric` accepts 'NaN'; metric values and limits must be real numbers."""
    return CheckConstraint(f"{column} <> 'NaN'", name=name)
