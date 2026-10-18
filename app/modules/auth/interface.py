"""Authentication interface used by every protected endpoint (REQ-13).

Contract (Phase 2, Section 5): `Authorization: Bearer <api_key>`. Monitored-system keys are scoped
to one system_id and may only submit metrics for it; human-user keys carry a viewer, reviewer or
admin role.

Fails closed: a missing, malformed, unknown or revoked key is always 401, never a default
principal. Authentication runs before body validation, so an unauthenticated malformed request
gets 401, not 400.
"""

import hashlib
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.principal import READER_ROLES, Principal, Role

__all__ = [
    "READER_ROLES",
    "Principal",
    "Role",
    "generate_api_key",
    "get_principal",
    "hash_api_key",
]

API_KEY_PREFIX = "acm_"
# Characters of the key stored in clear (api_key.key_prefix) to tell keys apart.
KEY_PREFIX_LENGTH = len(API_KEY_PREFIX) + 6

_bearer = HTTPBearer(auto_error=False, description="API key: `Authorization: Bearer <api_key>`")


def generate_api_key() -> str:
    """A new random API key (256 bits of entropy)."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """The stored form of a key (api_key.key_hash)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    uow: UnitOfWork = Depends(get_uow),
) -> Principal:
    """FastAPI dependency returning the authenticated caller."""
    if credentials is None or not credentials.credentials:
        raise _unauthorized()
    key_hash = hash_api_key(credentials.credentials)
    with uow:
        principal = uow.auth.principal_for_key(key_hash)
    if principal is None:
        raise _unauthorized()
    return principal
