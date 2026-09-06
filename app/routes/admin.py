"""Admin-only user management routes.

All endpoints under this router require an authenticated user with `role == "admin"`.
Non-admin authenticated requests get a 403; unauthenticated requests get a 401.
The admin credentials are:
    email:    myelindi@gmail.com
    password: Admin@123
    role:     admin  (set once via SQL UPDATE on the app_users table)
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.app_user import AppUser
from app.routes.deps import require_admin
from app.schemas.admin import AdminUserDetail, AdminUserSummary, AdminUsersResponse
from app.services.auth_service import CurrentUser

router = APIRouter(prefix="/admin", tags=["admin"])


def _to_summary(row: AppUser) -> AdminUserSummary:
    return AdminUserSummary(
        user_id=row.id,
        email=row.email,
        role=row.role,
        first_name=row.first_name,
        institution_name=row.institution_name,
        degree=row.degree,
        current_year=row.current_year,
        goals=list(row.goals),
        created_at=row.created_at,
    )


def _to_detail(row: AppUser) -> AdminUserDetail:
    return AdminUserDetail(
        user_id=row.id,
        email=row.email,
        role=row.role,
        first_name=row.first_name,
        institution_id=row.institution_id,
        institution_name=row.institution_name,
        institution_verified=row.institution_verified,
        degree=row.degree,
        current_year=row.current_year,
        goals=list(row.goals),
        created_at=row.created_at,
    )


@router.get(
    "/users",
    response_model=AdminUsersResponse,
    summary="List all signed-up users",
    description="Returns the total number of registered users and their profile data. "
    "Requires `role = 'admin'` -- non-admin authenticated users get 403.",
)
async def list_users(
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AdminUsersResponse:
    result = await session.execute(select(AppUser).order_by(AppUser.created_at.desc()))
    rows = result.scalars().all()

    count_result = await session.execute(select(func.count()).select_from(AppUser))
    total = count_result.scalar_one()

    return AdminUsersResponse(
        total_count=total,
        users=[_to_summary(r) for r in rows],
    )


@router.get(
    "/users/{user_id}",
    response_model=AdminUserDetail,
    summary="Get individual user details",
    description="Returns detailed profile information for a single user. "
    "404 if the user ID does not exist. "
    "Requires `role = 'admin'`.",
)
async def get_user(
    user_id: uuid.UUID,
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AdminUserDetail:
    row = await session.get(AppUser, user_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"User {user_id} not found.",
        )
    return _to_detail(row)
