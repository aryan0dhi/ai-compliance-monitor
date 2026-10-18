"""auth, ledger, notifications, monitoring gaps, test clock, worker heartbeat

REQ-02 (gap columns and states on event, configurable timeout on monitored_system), REQ-04
(webhook, notification outbox, notification_delivery), REQ-06/REQ-14 (ledger_entry), REQ-13
(app_user, api_key; FK on threshold_version.changed_by), plus the test environment's shared
clock offset and the worker heartbeat. See docs/database.md.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)

OLD_EVENT_STATES = "state IN ('FLAGGED', 'UNDER_REVIEW', 'DISPOSITIONED', 'CLOSED')"
NEW_EVENT_STATES = (
    "state IN ('FLAGGED', 'UNDER_REVIEW', 'DISPOSITIONED', 'CLOSED', 'GAP_OPEN', "
    "'REVIEWER_NOTIFIED', 'GAP_CLOSED')"
)


def upgrade() -> None:
    # --- REQ-02: configurable gap timeout -----------------------------------------------------
    op.add_column(
        "monitored_system",
        sa.Column("gap_missed_intervals", sa.Integer(), server_default="3", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_monitored_system_gap_missed_intervals_positive"),
        "monitored_system",
        "gap_missed_intervals > 0",
    )
    op.create_index(
        op.f("ix_metric_sample_system_id_received_at"),
        "metric_sample",
        ["system_id", "received_at"],
    )

    # --- REQ-13: accounts and API keys ------------------------------------------------------
    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("system_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("name", name=op.f("uq_app_user_name")),
        sa.ForeignKeyConstraint(
            ["system_id"],
            ["monitored_system.id"],
            ondelete="RESTRICT",
            name=op.f("fk_app_user_system_id_monitored_system"),
        ),
        sa.CheckConstraint(
            "role IN ('system', 'viewer', 'reviewer', 'admin')", name=op.f("ck_app_user_role")
        ),
        sa.CheckConstraint(
            "(role = 'system') = (system_id IS NOT NULL)", name=op.f("ck_app_user_system_scope")
        ),
    )
    op.create_table(
        "api_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_key")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_key_key_hash")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
            name=op.f("fk_api_key_user_id_app_user"),
        ),
        sa.CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_api_key_key_hash_sha256")),
    )
    op.create_index(op.f("ix_api_key_user_id"), "api_key", ["user_id"])
    op.create_foreign_key(
        op.f("fk_threshold_version_changed_by_app_user"),
        "threshold_version",
        "app_user",
        ["changed_by"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- REQ-02: gap events ---------------------------------------------------------------
    op.add_column("event", sa.Column("gap_started_at", TZ, nullable=True))
    op.add_column("event", sa.Column("gap_ended_at", TZ, nullable=True))
    op.drop_constraint(op.f("ck_event_state"), "event", type_="check")
    op.create_check_constraint(op.f("ck_event_state"), "event", NEW_EVENT_STATES)
    op.create_check_constraint(
        op.f("ck_event_state_matches_type"),
        "event",
        "(type = 'threshold_breach' AND state IN ('FLAGGED', 'UNDER_REVIEW', 'DISPOSITIONED', "
        "'CLOSED')) OR (type = 'monitoring_gap' AND state IN ('GAP_OPEN', 'REVIEWER_NOTIFIED', "
        "'GAP_CLOSED'))",
    )
    op.create_check_constraint(
        op.f("ck_event_gap_fields_present"),
        "event",
        "type <> 'monitoring_gap' OR (gap_started_at IS NOT NULL AND (gap_ended_at IS NOT NULL) "
        "= (state = 'GAP_CLOSED'))",
    )
    op.create_check_constraint(
        op.f("ck_event_breach_has_no_gap_fields"),
        "event",
        "type <> 'threshold_breach' OR (gap_started_at IS NULL AND gap_ended_at IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_event_gap_ends_after_start"), "event", "gap_ended_at >= gap_started_at"
    )
    op.create_index(
        op.f("uq_event_one_open_gap_per_system"),
        "event",
        ["system_id"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'monitoring_gap' AND state IN ('GAP_OPEN', 'REVIEWER_NOTIFIED')"
        ),
    )

    # --- REQ-06 / REQ-14: ledger -------------------------------------------------------------
    op.create_table(
        "ledger_entry",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("at", TZ, nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("entry_hash", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_ledger_entry")),
        sa.UniqueConstraint("prev_hash", name=op.f("uq_ledger_entry_prev_hash")),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.id"],
            ondelete="RESTRICT",
            name=op.f("fk_ledger_entry_event_id_event"),
        ),
        sa.CheckConstraint(
            "entry_type IN ('FLAGGED', 'VIEW', 'GAP_OPENED', 'GAP_CLOSED', 'NOTIFIED')",
            name=op.f("ck_ledger_entry_entry_type"),
        ),
        sa.CheckConstraint("seq > 0", name=op.f("ck_ledger_entry_seq_positive")),
        sa.CheckConstraint(
            "prev_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_ledger_entry_prev_hash_sha256")
        ),
        sa.CheckConstraint(
            "entry_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_ledger_entry_entry_hash_sha256")
        ),
    )
    op.create_index(op.f("ix_ledger_entry_event_id_seq"), "ledger_entry", ["event_id", "seq"])

    # --- REQ-04: webhooks and the notification outbox ----------------------------------------
    op.create_table(
        "webhook",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("event_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("disabled_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook")),
        sa.CheckConstraint(
            "cardinality(event_types) > 0 AND event_types <@ "
            "ARRAY['flag_notification', 'reminder']::text[]",
            name=op.f("ck_webhook_event_types_known"),
        ),
    )
    op.create_table(
        "notification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("notification_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("deliver_by", TZ, nullable=False),
        sa.Column("dispatched_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification")),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.id"],
            ondelete="RESTRICT",
            name=op.f("fk_notification_event_id_event"),
        ),
        sa.CheckConstraint(
            "notification_type IN ('flag_notification', 'reminder')",
            name=op.f("ck_notification_notification_type"),
        ),
        sa.CheckConstraint(
            "deliver_by >= created_at", name=op.f("ck_notification_deliver_by_after_created")
        ),
    )
    op.create_index(op.f("ix_notification_event_id"), "notification", ["event_id"])
    op.create_index(
        op.f("ix_notification_undispatched"),
        "notification",
        ["created_at"],
        postgresql_where=sa.text("dispatched_at IS NULL"),
    )
    op.create_index(op.f("ix_notification_deliver_by"), "notification", ["deliver_by"])
    op.create_table(
        "notification_delivery",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("webhook_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", TZ, nullable=False),
        sa.Column("last_attempt_at", TZ, nullable=True),
        sa.Column("delivered_at", TZ, nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint(
            "notification_id", "webhook_id", name=op.f("pk_notification_delivery")
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notification.id"],
            ondelete="RESTRICT",
            name=op.f("fk_notification_delivery_notification_id_notification"),
        ),
        sa.ForeignKeyConstraint(
            ["webhook_id"],
            ["webhook.id"],
            ondelete="RESTRICT",
            name=op.f("fk_notification_delivery_webhook_id_webhook"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DELIVERED', 'FAILED')",
            name=op.f("ck_notification_delivery_status"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_notification_delivery_attempts_non_negative")
        ),
        sa.CheckConstraint(
            "(status = 'DELIVERED') = (delivered_at IS NOT NULL)",
            name=op.f("ck_notification_delivery_delivered_at_iff_delivered"),
        ),
    )
    op.create_index(
        op.f("ix_notification_delivery_due"),
        "notification_delivery",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        op.f("ix_notification_delivery_webhook_id"), "notification_delivery", ["webhook_id"]
    )

    # --- test environment clock and worker heartbeat ------------------------------------------
    op.create_table(
        "simulated_clock",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("offset_seconds", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_simulated_clock")),
        sa.CheckConstraint("id = 1", name=op.f("ck_simulated_clock_single_row")),
        sa.CheckConstraint(
            "offset_seconds >= 0", name=op.f("ck_simulated_clock_offset_non_negative")
        ),
    )
    op.create_table(
        "worker_heartbeat",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("beat_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_heartbeat")),
        sa.CheckConstraint("id = 1", name=op.f("ck_worker_heartbeat_single_row")),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeat")
    op.drop_table("simulated_clock")
    op.drop_index(op.f("ix_notification_delivery_webhook_id"), table_name="notification_delivery")
    op.drop_index(op.f("ix_notification_delivery_due"), table_name="notification_delivery")
    op.drop_table("notification_delivery")
    op.drop_index(op.f("ix_notification_deliver_by"), table_name="notification")
    op.drop_index(op.f("ix_notification_undispatched"), table_name="notification")
    op.drop_index(op.f("ix_notification_event_id"), table_name="notification")
    op.drop_table("notification")
    op.drop_table("webhook")
    op.drop_index(op.f("ix_ledger_entry_event_id_seq"), table_name="ledger_entry")
    op.drop_table("ledger_entry")

    # Gap events can't satisfy the old constraints; they're removed with the gap columns.
    op.execute("DELETE FROM event WHERE type = 'monitoring_gap'")
    op.drop_index(op.f("uq_event_one_open_gap_per_system"), table_name="event")
    op.drop_constraint(op.f("ck_event_gap_ends_after_start"), "event", type_="check")
    op.drop_constraint(op.f("ck_event_breach_has_no_gap_fields"), "event", type_="check")
    op.drop_constraint(op.f("ck_event_gap_fields_present"), "event", type_="check")
    op.drop_constraint(op.f("ck_event_state_matches_type"), "event", type_="check")
    op.drop_constraint(op.f("ck_event_state"), "event", type_="check")
    op.create_check_constraint(op.f("ck_event_state"), "event", OLD_EVENT_STATES)
    op.drop_column("event", "gap_ended_at")
    op.drop_column("event", "gap_started_at")

    op.drop_constraint(
        op.f("fk_threshold_version_changed_by_app_user"), "threshold_version", type_="foreignkey"
    )
    op.drop_index(op.f("ix_api_key_user_id"), table_name="api_key")
    op.drop_table("api_key")
    op.drop_table("app_user")

    op.drop_index(op.f("ix_metric_sample_system_id_received_at"), table_name="metric_sample")
    op.drop_constraint(
        op.f("ck_monitored_system_gap_missed_intervals_positive"), "monitored_system", type_="check"
    )
    op.drop_column("monitored_system", "gap_missed_intervals")
