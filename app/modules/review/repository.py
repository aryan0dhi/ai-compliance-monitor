"""Data access for harm_field."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.review.models import HarmField, HarmFieldName

# Response and storage order: Art. 3(49) order, as listed in HarmFieldName.
_ORDER = {name.value: i for i, name in enumerate(HarmFieldName)}


class HarmFieldRepository(Protocol):
    def add_all(self, fields: list[HarmField]) -> None: ...

    def for_event(self, event_id: UUID) -> list[HarmField]:
        """The event's harm fields in Art. 3(49) order; empty for gaps and pre-0005 breaches."""
        ...


class SqlHarmFieldRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_all(self, fields: list[HarmField]) -> None:
        self._session.add_all(fields)
        self._session.flush()

    def for_event(self, event_id: UUID) -> list[HarmField]:
        rows = self._session.scalars(select(HarmField).where(HarmField.event_id == event_id))
        return sorted(rows, key=lambda f: _ORDER[f.name])
