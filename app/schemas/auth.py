import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas._examples import example


class RegisterRequest(BaseModel):
    """`POST /auth/register` -- proxies straight to Supabase Auth's signup API. Password hashing
    and session-token signing are entirely Supabase's; this backend never implements either."""

    model_config = ConfigDict(json_schema_extra={"example": example("auth_register_request")})

    email: str
    password: str


class LoginRequest(BaseModel):
    """`POST /auth/login` -- proxies to Supabase Auth's password grant."""

    model_config = ConfigDict(json_schema_extra={"example": example("auth_login_request")})

    email: str
    password: str


class RefreshRequest(BaseModel):
    """`POST /auth/refresh` -- trades the `refresh_token` handed out with a session for a fresh
    `access_token`. Supabase access tokens last about an hour, which is shorter than a full
    four-quarter run, so a long session has to be renewed rather than simply expiring."""

    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    """`POST /auth/forgot-password` -- proxies to Supabase Auth's `/recover`. Always answers
    the same way regardless of whether the email is registered (Supabase's own anti-enumeration
    behavior)."""

    email: str


class ResetPasswordRequest(BaseModel):
    """`POST /auth/reset-password` -- `access_token` is the short-lived recovery token Supabase's
    emailed link redirects the user back with (read from the URL fragment client-side, never
    logged server-side)."""

    access_token: str
    new_password: str


class AuthResponse(BaseModel):
    """The session returned by both `/auth/register` and `/auth/login`. Send `access_token` as
    `Authorization: Bearer <access_token>` on every subsequent request."""

    model_config = ConfigDict(json_schema_extra={"example": example("auth_login_response")})

    access_token: str = Field(description="The Supabase-issued JWT -- send as a Bearer token on every other request.")
    refresh_token: str | None = None
    user_id: uuid.UUID = Field(description="The Supabase auth user id -- becomes this user's `owner_id` on any company they create.")
    email: str
    is_admin: bool = Field(
        default=False,
        description="True when this user's role is 'admin'. The frontend uses this to redirect "
        "to the admin panel instead of the regular application on login.",
    )
