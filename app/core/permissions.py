from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_jwt
from app.database import get_db
from app.dependencies import oauth2_scheme

logger = logging.getLogger(__name__)


class Role(StrEnum):
    """User roles matching the user_role DB enum."""

    CITIZEN = "CITIZEN"
    AUTHORITY = "AUTHORITY"
    ADMIN = "ADMIN"


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Decode JWT and verify user is active in DB.

    Per TRD Section 6: even if JWT is cryptographically valid,
    if users.is_active = FALSE, reject with 401.
    """
    if token is None:
        raise UnauthorizedError("Authentication required")

    payload = decode_jwt(token)

    # Import here to avoid circular imports
    from app.models.user import User

    user = await db.get(User, payload["sub"])
    if not user or not user.is_active:
        raise UnauthorizedError("Account deactivated or not found")
    return user


async def get_current_user_optional(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Any | None:
    """Like get_current_user but returns None for unauthenticated requests.

    Used on endpoints that support both authenticated and anonymous access.
    """
    if token is None:
        return None
    try:
        return await get_current_user(token=token, db=db)
    except UnauthorizedError:
        return None


def require_role(*roles: Role):  # noqa: ANN201
    """FastAPI dependency factory that enforces RBAC role checks.

    Usage:
        @router.post("/admin/...", dependencies=[Depends(require_role(Role.ADMIN))])
    """

    async def _guard(
        token: str | None = Depends(oauth2_scheme),
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        user = await get_current_user(token=token, db=db)
        if user.role not in [r.value for r in roles]:
            raise ForbiddenError(f"Requires one of: {[r.value for r in roles]}")
        return user

    return _guard


# Convenience guards
require_citizen = require_role(Role.CITIZEN)
require_authority = require_role(Role.AUTHORITY, Role.ADMIN)
require_admin = require_role(Role.ADMIN)
