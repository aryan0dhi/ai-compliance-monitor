"""Data access for app_user and api_key."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.auth.models import ApiKey, AppUser
from app.modules.auth.principal import Principal, Role


class AuthRepository(Protocol):
    def principal_for_key(self, key_hash: str) -> Principal | None:
        """The principal owning an unrevoked key with this hash, or None."""
        ...

    def get_user_by_name(self, name: str) -> AppUser | None: ...

    def get_key(self, key_id: UUID) -> ApiKey | None: ...

    def add_user(self, user: AppUser) -> None: ...

    def add_key(self, key: ApiKey) -> None: ...


class SqlAuthRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def principal_for_key(self, key_hash: str) -> Principal | None:
        stmt = (
            select(AppUser)
            .join(ApiKey, ApiKey.user_id == AppUser.id)
            .where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )
        user = self._session.scalars(stmt).first()
        if user is None:
            return None
        return Principal(actor_id=user.id, role=Role(user.role), system_id=user.system_id)

    def get_user_by_name(self, name: str) -> AppUser | None:
        return self._session.scalars(select(AppUser).where(AppUser.name == name)).first()

    def get_key(self, key_id: UUID) -> ApiKey | None:
        return self._session.get(ApiKey, key_id)

    def add_user(self, user: AppUser) -> None:
        self._session.add(user)
        self._session.flush()

    def add_key(self, key: ApiKey) -> None:
        self._session.add(key)
        self._session.flush()
