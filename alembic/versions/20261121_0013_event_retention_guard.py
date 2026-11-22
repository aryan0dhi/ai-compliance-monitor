"""event retention guard: no event is deleted, or its retain_until shortened, before retention

REQ-05, INV-10 (#69): the CHECK on retain_until and the RESTRICT foreign keys keep an event's
retention date and its triggering sample, but nothing stopped a direct `DELETE FROM event` (or an
UPDATE lowering retain_until) before the six months were up. This trigger refuses both, also for
direct SQL that bypasses the application.

"Now" is the service clock: the database time plus the test environment's simulated offset
(simulated_clock, D-16), which only exists in the test environment, so advancing the test clock
past retain_until allows the deletion as it would in real time. TRUNCATE (the test reset) doesn't
fire row triggers and is unaffected.

The trigger isn't part of the SQLAlchemy models, so autogenerate ignores it;
tests/integration/test_retention_pg.py covers it.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION event_retention_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            service_now timestamptz := now() + make_interval(secs => coalesce(
                (SELECT offset_seconds FROM simulated_clock WHERE id = 1), 0));
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.retain_until > service_now THEN
                    RAISE EXCEPTION 'event % must be retained until % (REQ-05)',
                        OLD.id, OLD.retain_until
                        USING ERRCODE = 'restrict_violation';
                END IF;
                RETURN OLD;
            END IF;
            IF NEW.retain_until < OLD.retain_until THEN
                RAISE EXCEPTION 'retain_until of event % can only move later (REQ-05)', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER event_retention_guard BEFORE DELETE OR UPDATE OF retain_until ON event "
        "FOR EACH ROW EXECUTE FUNCTION event_retention_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER event_retention_guard ON event")
    op.execute("DROP FUNCTION event_retention_guard()")
