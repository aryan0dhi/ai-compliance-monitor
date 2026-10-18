"""Account and API key management (used by the admin CLI, app/cli.py)."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.auth.interface import KEY_PREFIX_LENGTH, generate_api_key, hash_api_key
from app.modules.auth.models import ApiKey, AppUser
from app.modules.auth.principal import Role


class AccountError(ValueError):
    pass


@dataclass(frozen=True)
class IssuedKey:
    user_id: UUID
    key_id: UUID
    # The only time the key exists in clear text; it is not stored.
    api_key: str


def _new_key(user_id: UUID) -> tuple[ApiKey, str]:
    api_key = generate_api_key()
    row = ApiKey(
        id=uuid4(),
        user_id=user_id,
        key_hash=hash_api_key(api_key),
        key_prefix=api_key[:KEY_PREFIX_LENGTH],
    )
    return row, api_key


def create_user(
    uow: UnitOfWork, *, name: str, role: Role, system_id: UUID | None = None
) -> IssuedKey:
    """Create an account and its first API key. System accounts must name their system."""
    if (role is Role.SYSTEM) != (system_id is not None):
        raise AccountError("system accounts need a system_id; human accounts must not have one")
    with uow:
        if uow.auth.get_user_by_name(name) is not None:
            raise AccountError(f"user {name!r} already exists")
        if system_id is not None and uow.systems.get(system_id) is None:
            raise AccountError(f"unknown system_id {system_id}")
        user = AppUser(id=uuid4(), name=name, role=role.value, system_id=system_id)
        uow.auth.add_user(user)
        key, api_key = _new_key(user.id)
        uow.auth.add_key(key)
    return IssuedKey(user_id=user.id, key_id=key.id, api_key=api_key)


def create_key(uow: UnitOfWork, *, user_name: str) -> IssuedKey:
    """Issue an additional key for an existing account (e.g. for rotation)."""
    with uow:
        user = uow.auth.get_user_by_name(user_name)
        if user is None:
            raise AccountError(f"unknown user {user_name!r}")
        key, api_key = _new_key(user.id)
        uow.auth.add_key(key)
    return IssuedKey(user_id=user.id, key_id=key.id, api_key=api_key)


def revoke_key(uow: UnitOfWork, *, key_id: UUID, clock: Clock) -> None:
    with uow:
        key = uow.auth.get_key(key_id)
        if key is None:
            raise AccountError(f"unknown key {key_id}")
        if key.revoked_at is None:
            key.revoked_at = clock.now()
