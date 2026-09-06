"""Pydantic schemas for `GET /admin/users` and `GET /admin/users/{user_id}`.

These are read-only mirrors of the `AppUser` row -- no write schemas needed here
since role elevation is always a manual DB operation, never a self-service endpoint.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class AdminUserSummary(BaseModel):
    """Compact user representation returned in the paginated list (`GET /admin/users`)."""

    user_id: uuid.UUID
    email: str
    role: str
    first_name: str | None = None
    institution_name: str | None = None
    degree: str | None = None
    current_year: str | None = None
    goals: list[str] = []
    created_at: datetime


class AdminUsersResponse(BaseModel):
    """Envelope for `GET /admin/users` -- includes the total count so the frontend
    can display a headline figure without counting the list itself."""

    total_count: int
    users: list[AdminUserSummary]


class AdminUserDetail(BaseModel):
    """Full user record returned by `GET /admin/users/{user_id}`.

    Includes `institution_verified` (omitted from the summary list to keep it compact)
    and `institution_id` for completeness.
    """

    user_id: uuid.UUID
    email: str
    role: str
    first_name: str | None = None
    institution_id: str | None = None
    institution_name: str | None = None
    institution_verified: bool = False
    degree: str | None = None
    current_year: str | None = None
    goals: list[str] = []
    created_at: datetime
