"""Data access for harm_field, disposition and report."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.review.models import (
    Disposition,
    HarmField,
    HarmFieldName,
    Report,
    ReportSubmission,
)

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


class DispositionRepository(Protocol):
    def add(self, disposition: Disposition) -> None: ...

    def for_event(self, event_id: UUID) -> list[Disposition]:
        """The event's dispositions, oldest first (seq order); the last is the current tier."""
        ...


class SqlDispositionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, disposition: Disposition) -> None:
        self._session.add(disposition)
        self._session.flush()

    def for_event(self, event_id: UUID) -> list[Disposition]:
        stmt = select(Disposition).where(Disposition.event_id == event_id).order_by(Disposition.seq)
        return list(self._session.scalars(stmt))


class ReportRepository(Protocol):
    def add(self, report: Report) -> None: ...

    def get(self, event_id: UUID, disposition_seq: int) -> Report | None:
        """The report assembled from this disposition, if any."""
        ...

    def by_id(self, report_id: UUID) -> Report | None: ...

    def for_event(self, event_id: UUID) -> list[Report]:
        """Every report version of the event, oldest first (disposition order)."""
        ...


class SqlReportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, report: Report) -> None:
        self._session.add(report)
        self._session.flush()

    def get(self, event_id: UUID, disposition_seq: int) -> Report | None:
        return self._session.get(Report, (event_id, disposition_seq))

    def by_id(self, report_id: UUID) -> Report | None:
        return self._session.scalars(select(Report).where(Report.id == report_id)).first()

    def for_event(self, event_id: UUID) -> list[Report]:
        stmt = select(Report).where(Report.event_id == event_id).order_by(Report.disposition_seq)
        return list(self._session.scalars(stmt))


class SubmissionRepository(Protocol):
    def add(self, submission: ReportSubmission) -> None: ...

    def for_event(self, event_id: UUID) -> list[ReportSubmission]:
        """The event's submissions, oldest first."""
        ...


class SqlSubmissionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, submission: ReportSubmission) -> None:
        self._session.add(submission)
        self._session.flush()

    def for_event(self, event_id: UUID) -> list[ReportSubmission]:
        stmt = (
            select(ReportSubmission)
            .where(ReportSubmission.event_id == event_id)
            .order_by(ReportSubmission.submitted_at, ReportSubmission.id)
        )
        return list(self._session.scalars(stmt))
