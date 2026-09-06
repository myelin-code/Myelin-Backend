import uuid
from typing import Callable, Coroutine

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.engines.run_state import Move
from app.models.company import Company
from app.models.modifier import Modifier
from app.models.quarter import Quarter, QuarterStatus
from app.services.auth_service import CurrentUser, get_or_create_app_user, verify_jwt
from app.services.authorization_service import NotPermittedError, require_owner, require_read_access
from app.services.run_service import require_move

_bearer = HTTPBearer(auto_error=False)


class NotAuthenticatedError(Exception):
    """Raised by `get_current_user` when the request carries no usable Bearer token --
    distinct from `NotPermittedError` (authenticated, but not allowed) and from
    `IllegalMoveError` (allowed, but not legal right now). `main.py` maps this to its own 401
    `not_authenticated` envelope rather than FastAPI's default `{"detail": ...}` shape, so all
    three refusal reasons share one `{"error": ...}` convention."""


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """Turns an HTTP `Authorization` header into a domain object -- the one seam in this file
    that talks to Supabase's JWKS (`auth_service.verify_jwt`) rather than just the DB. `role`
    is read from the local `app_users` row, never from the JWT itself: Supabase's token has no
    concept of Myelin's roles.
    """
    if credentials is None:
        raise NotAuthenticatedError("missing bearer token")
    try:
        claims = verify_jwt(credentials.credentials, settings)
    except jwt.PyJWTError as exc:
        raise NotAuthenticatedError("invalid or expired token") from exc

    sub = uuid.UUID(claims["sub"])
    app_user = await get_or_create_app_user(session, sub=sub, email=claims.get("email", ""))
    return CurrentUser(id=app_user.id, email=app_user.email, role=app_user.role)


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CurrentUser | None:
    """Optional authentication -- returns CurrentUser if authenticated, None otherwise.
    Used for public endpoints that show different data based on auth status (e.g. leaderboards).
    """
    if credentials is None:
        return None
    try:
        claims = verify_jwt(credentials.credentials, settings)
        sub = uuid.UUID(claims["sub"])
        app_user = await get_or_create_app_user(session, sub=sub, email=claims.get("email", ""))
        return CurrentUser(id=app_user.id, email=app_user.email, role=app_user.role)
    except (jwt.PyJWTError, ValueError):
        # Invalid token: treat as anonymous rather than raising auth error
        return None


async def require_admin(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Authorization gate for admin-only endpoints.

    Depends on `get_current_user` (authentication -- 401 if no valid JWT) and then
    checks that the resolved `AppUser` carries `role='admin'` (authorization -- 403 if not).
    The two concerns are kept separate: identity first, privilege second, matching the pattern
    already used by `require_owner` / `require_read_access` in the authorization service.
    """
    if user.role != "admin":
        raise NotPermittedError("admin role required")
    return user


async def get_quarter(
    company_id: uuid.UUID,
    quarter_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> Quarter:
    """Loads the Quarter regardless of lock state -- for read-only routes (state, report).

    Phase 13: also the read-access gate (owner-or-instructor) every quarter-scoped route goes
    through, read or write -- `get_quarter_for_write` below layers the stricter owner-only
    check on top for writes. Identity (this check) always runs before legality
    (`require_move`), never the reverse.
    """
    quarter = await session.get(Quarter, quarter_id)
    if quarter is None or quarter.company_id != company_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Quarter {quarter_id} not found for company {company_id}")
    company = await session.get(Company, company_id)
    require_read_access(company, user)
    return quarter


async def get_quarter_for_write(
    quarter: Quarter = Depends(get_quarter),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> Quarter:
    """Ownership gate for any write route. Sits strictly between `get_quarter` (identity: can
    this user even see this quarter) and `require_quarter_move`/`get_open_quarter` (legality:
    is this move allowed on this run right now) -- its own function, not folded into either,
    so ownership and legality never share one code path.
    """
    company = await session.get(Company, quarter.company_id)
    require_owner(company, user)
    return quarter


async def get_open_quarter(quarter: Quarter = Depends(get_quarter_for_write)) -> Quarter:
    """Same lookup (plus the owner-only gate above), plus the immutability guard: decisions
    are rejected once a quarter is locked. Still used by the legacy per-decision pipeline
    (`routes/_factory.py`) -- the 22-line pipeline's own write routes use
    `require_quarter_move` below instead (Phase 12), which folds this same check into the
    single legal-move gatekeeper alongside crisis-quarter/Q4-only gating.
    """
    if quarter.status == QuarterStatus.CLOSED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Quarter {quarter.id} is locked; decisions are immutable")
    return quarter


def require_quarter_move(
    move: Move,
) -> Callable[[Quarter, AsyncSession], Coroutine[None, None, Quarter]]:
    """Dependency factory: loads the quarter through the owner-only gate
    (`get_quarter_for_write`), then consults `engines/run_state.py`'s single gatekeeper for
    `move` before letting a write route proceed. Raises `IllegalMoveError` (mapped to a
    consistent JSON body by `main.py`'s handler) rather than a route-specific 409/404 -- this
    is what Phase 12 routes writes through instead of `get_open_quarter`'s bare lock-state
    check, so a crisis-only or Q4-only move is refused for the right reason, not just "the
    quarter is locked". Ownership (`get_quarter_for_write`) is always checked before legality.
    """

    async def _dependency(
        quarter: Quarter = Depends(get_quarter_for_write),
        session: AsyncSession = Depends(get_db),
    ) -> Quarter:
        company = await session.get(Company, quarter.company_id)
        await require_move(session, company, move)
        return quarter

    return _dependency


async def get_quarter_modifiers(quarter_id: uuid.UUID, session: AsyncSession) -> dict[str, float]:
    result = await session.execute(select(Modifier).where(Modifier.quarter_id == quarter_id))
    return {m.modifier_key: m.value for m in result.scalars()}
