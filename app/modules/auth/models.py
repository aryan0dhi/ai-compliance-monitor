"""app_user and api_key (REQ-13; Model 2's users, Q7.3 hashed key storage)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in
from app.modules.auth.principal import Role


class AppUser(Base):
    """A human user or a monitored system's account. `id` is the actor_id written to the ledger."""

    __tablename__ = "app_user"
    __table_args__ = (
        check_in("role", Role, name="role"),
        # Section 5: monitored-system keys are scoped to exactly one system_id; human keys to none.
        CheckConstraint(f"(role = '{Role.SYSTEM}') = (system_id IS NOT NULL)", name="system_scope"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True)
    role: Mapped[str]
    system_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("monitored_system.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ApiKey(Base):
    """An API key. Only the sha256 of the key is stored; the key itself is shown once at creation.

    Keys are 256-bit random tokens, so an unsalted fast hash is sufficient (no dictionary to attack)
    and lets authentication look the key up by an indexed equality match.
    """

    __tablename__ = "api_key"
    __table_args__ = (
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="key_hash_sha256"),
        Index("ix_api_key_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    key_hash: Mapped[str] = mapped_column(unique=True)
    # Leading characters of the key, so an admin can tell keys apart without the secret.
    key_prefix: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Revoked keys are kept (never deleted) so ledger actor ids stay traceable to a key holder.
    revoked_at: Mapped[datetime | None]
