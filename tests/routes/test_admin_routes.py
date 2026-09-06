"""`GET /admin/users` and `GET /admin/users/{user_id}` route tests.

Uses the same DB/client fixture infrastructure as the rest of the route tests -- each run
gets a fresh transaction rolled back at teardown, and `get_current_user` is stubbed so tests
don't need a real Supabase JWT.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.app_user import AppUser
from app.routes.deps import get_current_user, require_admin
from app.services.auth_service import CurrentUser


# ---------------------------------------------------------------------------
# Admin-user fixture and client that bypasses auth as an admin
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def admin_test_user(db_session: AsyncSession) -> CurrentUser:
    """Provisions an `admin` AppUser row for tests that need the admin guard to pass."""
    user = AppUser(id=uuid.uuid4(), email="admin@myelin.dev", role="admin")
    db_session.add(user)
    await db_session.flush()
    return CurrentUser(id=user.id, email=user.email, role=user.role)


@pytest_asyncio.fixture
async def admin_client(db_session: AsyncSession, admin_test_user: CurrentUser) -> AsyncClient:
    """ASGI test client whose `get_current_user` resolves to an admin user."""
    from collections.abc import AsyncGenerator
    from httpx import ASGITransport
    from app.core.db import get_db

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def override_get_current_user() -> CurrentUser:
        return admin_test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: GET /admin/users
# ---------------------------------------------------------------------------

async def test_non_admin_gets_403(client: AsyncClient) -> None:
    """The default `client` fixture resolves as a `student` -- admin gate must 403."""
    res = await client.get("/admin/users")
    assert res.status_code == 403
    body = res.json()
    assert body["error"] == "not_permitted"


async def test_unauthenticated_gets_401(client: AsyncClient) -> None:
    """Removing the auth override means no Bearer token -- gate must 401."""
    app.dependency_overrides.pop(get_current_user, None)
    res = await client.get("/admin/users")
    assert res.status_code == 401


async def test_admin_can_list_users(admin_client: AsyncClient, admin_test_user: CurrentUser) -> None:
    """Admin gets 200 with `total_count` and `users` list."""
    res = await admin_client.get("/admin/users")
    assert res.status_code == 200
    body = res.json()
    assert "total_count" in body
    assert "users" in body
    assert isinstance(body["total_count"], int)
    assert isinstance(body["users"], list)


async def test_user_count_reflects_actual_rows(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    admin_test_user: CurrentUser,
) -> None:
    """total_count must match the number of user rows in the DB."""
    # Create two extra users in addition to the admin fixture user
    db_session.add(AppUser(id=uuid.uuid4(), email="a@example.com", role="student"))
    db_session.add(AppUser(id=uuid.uuid4(), email="b@example.com", role="student"))
    await db_session.flush()

    res = await admin_client.get("/admin/users")
    assert res.status_code == 200
    body = res.json()
    # admin fixture + 2 extra = at least 3
    assert body["total_count"] >= 3
    assert len(body["users"]) == body["total_count"]


async def test_user_list_contains_expected_fields(
    admin_client: AsyncClient,
    admin_test_user: CurrentUser,
) -> None:
    """Each user summary must include the required fields."""
    res = await admin_client.get("/admin/users")
    assert res.status_code == 200
    users = res.json()["users"]
    assert len(users) >= 1
    user = users[0]
    for field in ("user_id", "email", "role", "created_at", "goals"):
        assert field in user, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Tests: GET /admin/users/{user_id}
# ---------------------------------------------------------------------------

async def test_admin_can_get_individual_user(
    admin_client: AsyncClient,
    admin_test_user: CurrentUser,
) -> None:
    """Admin fetching their own row by user_id gets 200 with full detail."""
    res = await admin_client.get(f"/admin/users/{admin_test_user.id}")
    assert res.status_code == 200
    body = res.json()
    assert body["user_id"] == str(admin_test_user.id)
    assert body["email"] == admin_test_user.email
    assert body["role"] == "admin"
    # All DetailResponse fields must be present
    for field in ("institution_id", "institution_name", "institution_verified", "degree", "current_year", "goals"):
        assert field in body, f"Missing field: {field}"


async def test_unknown_user_id_returns_404(admin_client: AsyncClient) -> None:
    """A UUID that doesn't exist in the DB must return 404."""
    res = await admin_client.get(f"/admin/users/{uuid.uuid4()}")
    assert res.status_code == 404


async def test_non_admin_cannot_get_individual_user(
    client: AsyncClient,
    admin_test_user: CurrentUser,
) -> None:
    """Student-role user must get 403 even on a per-user detail endpoint."""
    res = await client.get(f"/admin/users/{admin_test_user.id}")
    assert res.status_code == 403
