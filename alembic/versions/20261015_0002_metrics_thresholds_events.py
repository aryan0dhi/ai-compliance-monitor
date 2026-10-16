"""metrics, thresholds, events: monitored_system, metric_sample, threshold_version, event

REQ-01 (metric collection), REQ-03 (thresholds and flagged events) and the storage portion of
REQ-05 (retain_until column and its minimum-six-months constraint). See docs/database.md.

Written by hand (no database available for autogenerate); kept in sync with the models by
tests/test_migrations.py.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "monitored_system",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sampling_interval_s", sa.Integer(), server_default="300", nullable=False),
        sa.Column("last_seen_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitored_system")),
        sa.UniqueConstraint("name", name=op.f("uq_monitored_system_name")),
        sa.CheckConstraint(
            "sampling_interval_s > 0", name=op.f("ck_monitored_system_sampling_interval_positive")
        ),
    )

    op.create_table(
        "metric_sample",
        sa.Column("system_id", sa.Uuid(), nullable=False),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("sampled_at", TZ, nullable=False),
        sa.Column("metric_type", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("snapshot_ref", sa.Text(), nullable=True),
        sa.Column("received_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint(
            "system_id", "metric_name", "sampled_at", name=op.f("pk_metric_sample")
        ),
        sa.ForeignKeyConstraint(
            ["system_id"],
            ["monitored_system.id"],
            ondelete="RESTRICT",
            name=op.f("fk_metric_sample_system_id_monitored_system"),
        ),
        sa.CheckConstraint(
            "metric_type IN ('performance', 'drift')", name=op.f("ck_metric_sample_metric_type")
        ),
        sa.CheckConstraint("value <> 'NaN'", name=op.f("ck_metric_sample_value_not_nan")),
    )

    op.create_table(
        "threshold_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("system_id", sa.Uuid(), nullable=False),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("metric_type", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("limit_value", sa.Numeric(), nullable=False),
        sa.Column("valid_from", TZ, nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_threshold_version")),
        sa.ForeignKeyConstraint(
            ["system_id"],
            ["monitored_system.id"],
            ondelete="RESTRICT",
            name=op.f("fk_threshold_version_system_id_monitored_system"),
        ),
        sa.UniqueConstraint(
            "system_id",
            "metric_name",
            "valid_from",
            name=op.f("uq_threshold_version_metric_valid_from"),
        ),
        sa.CheckConstraint(
            "metric_type IN ('performance', 'drift')", name=op.f("ck_threshold_version_metric_type")
        ),
        sa.CheckConstraint(
            "direction IN ('above', 'below')", name=op.f("ck_threshold_version_direction")
        ),
        sa.CheckConstraint(
            "limit_value <> 'NaN'", name=op.f("ck_threshold_version_limit_value_not_nan")
        ),
    )

    op.create_table(
        "event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("system_id", sa.Uuid(), nullable=False),
        sa.Column("metric_name", sa.Text(), nullable=True),
        sa.Column("metric_timestamp", TZ, nullable=True),
        sa.Column("observed_value", sa.Numeric(), nullable=True),
        sa.Column("threshold_version_id", sa.Uuid(), nullable=True),
        sa.Column("threshold_value", sa.Numeric(), nullable=True),
        sa.Column("detected_at", TZ, nullable=False),
        sa.Column("retain_until", TZ, nullable=False),
        sa.Column("evidence_bundle_hash", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
        sa.ForeignKeyConstraint(
            ["system_id"],
            ["monitored_system.id"],
            ondelete="RESTRICT",
            name=op.f("fk_event_system_id_monitored_system"),
        ),
        sa.ForeignKeyConstraint(
            ["threshold_version_id"],
            ["threshold_version.id"],
            ondelete="RESTRICT",
            name=op.f("fk_event_threshold_version_id_threshold_version"),
        ),
        sa.ForeignKeyConstraint(
            ["system_id", "metric_name", "metric_timestamp"],
            ["metric_sample.system_id", "metric_sample.metric_name", "metric_sample.sampled_at"],
            ondelete="RESTRICT",
            name=op.f("fk_event_triggering_sample"),
        ),
        sa.CheckConstraint(
            "type IN ('threshold_breach', 'monitoring_gap')", name=op.f("ck_event_type")
        ),
        sa.CheckConstraint(
            "state IN ('FLAGGED', 'UNDER_REVIEW', 'DISPOSITIONED', 'CLOSED')",
            name=op.f("ck_event_state"),
        ),
        sa.CheckConstraint(
            "type <> 'threshold_breach' OR (metric_name IS NOT NULL AND metric_timestamp IS NOT "
            "NULL AND observed_value IS NOT NULL AND threshold_version_id IS NOT NULL AND "
            "threshold_value IS NOT NULL)",
            name=op.f("ck_event_breach_fields_present"),
        ),
        sa.CheckConstraint(
            "retain_until >= ((detected_at AT TIME ZONE 'UTC') + interval '6 months') "
            "AT TIME ZONE 'UTC'",
            name=op.f("ck_event_retain_until_min_6_months"),
        ),
        sa.CheckConstraint(
            "evidence_bundle_hash IS NULL OR evidence_bundle_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_event_evidence_bundle_hash_sha256"),
        ),
        sa.UniqueConstraint(
            "system_id",
            "metric_name",
            "metric_timestamp",
            name=op.f("uq_event_triggering_sample"),
        ),
    )
    op.create_index(op.f("ix_event_detected_at_id"), "event", ["detected_at", "id"])
    op.create_index(op.f("ix_event_system_id_detected_at"), "event", ["system_id", "detected_at"])
    op.create_index(op.f("ix_event_state"), "event", ["state"])
    op.create_index(op.f("ix_event_type"), "event", ["type"])


def downgrade() -> None:
    op.drop_index(op.f("ix_event_type"), table_name="event")
    op.drop_index(op.f("ix_event_state"), table_name="event")
    op.drop_index(op.f("ix_event_system_id_detected_at"), table_name="event")
    op.drop_index(op.f("ix_event_detected_at_id"), table_name="event")
    op.drop_table("event")
    op.drop_table("threshold_version")
    op.drop_table("metric_sample")
    op.drop_table("monitored_system")
