"""Thin proxy routes over Supabase Auth's own REST API -- password hashing and session-token
signing are entirely Supabase's; this backend never implements either (see
`app/services/auth_service.py`). Kept in the backend, rather than left to a frontend calling
Supabase directly, so the full register -> login -> play lifecycle stays testable end to end
via httpx/pytest, the same way Phase 12's acceptance tests exercise the run lifecycle.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.models.app_user import AppUser
from app.routes.deps import NotAuthenticatedError
from app.schemas.auth import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
)
from app.services.auth_service import (
    PasswordResetMisconfigured,
    SupabaseAuthClient,
    SupabaseAuthError,
    get_supabase_auth_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=201,
    summary="Register a new user",
    description="Creates a Supabase Auth identity via Supabase's own signup API and returns a "
    "session. Not run-scoped -- no Bearer token required to call this. Call this once per user, "
    "then `POST /auth/login` on return visits. 422 (plain `detail`) if Supabase rejects the "
    "email/password itself (invalid domain, weak password, already registered); 429 (plain "
    "`detail`) if Supabase's signup rate limit was hit.",
)
async def register(
    payload: RegisterRequest,
    client: SupabaseAuthClient = Depends(get_supabase_auth_client),
) -> AuthResponse:
    try:
        result = await client.sign_up(email=payload.email, password=payload.password)
    except SupabaseAuthError as exc:
        # Rate limiting is the one signup rejection that isn't about the input itself.
        if exc.status_code == 429:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many signup attempts. Please wait a minute and try again. Already have an account? Try logging in instead.",
            ) from exc
        
        # Provide specific, actionable error messages for common signup issues
        error_message = exc.message.lower() if exc.message else ""
        if "already registered" in error_message or "user already registered" in error_message or "already exists" in error_message:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This email is already registered. Log in instead, or use a different email address.",
            ) from exc
        elif "password" in error_message and ("weak" in error_message or "short" in error_message or "at least" in error_message):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Password is too weak. Please use at least 8 characters with a mix of letters and numbers.",
            ) from exc
        elif "email" in error_message and ("invalid" in error_message or "format" in error_message):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Please enter a valid email address.",
            ) from exc
        else:
            # Everything else Supabase's signup endpoint rejects (bad email, weak password,
            # already-registered, ...) is a malformed/rejected request, not a server failure.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                exc.message or "Unable to create account. Please check your information and try again.",
            ) from exc
    return AuthResponse(**result)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Log in an existing user",
    description="Proxies to Supabase Auth's password grant and returns a session. Send the "
    "returned `access_token` as `Authorization: Bearer <access_token>` on every subsequent "
    "request. 401 `{\"error\": \"not_authenticated\"}` for any rejected login attempt (wrong "
    "password, unknown email, unconfirmed email, ...); 429 (plain `detail`) if Supabase's "
    "login rate limit was hit.",
)
async def login(
    payload: LoginRequest,
    client: SupabaseAuthClient = Depends(get_supabase_auth_client),
    session: AsyncSession = Depends(get_db),
) -> AuthResponse:
    try:
        result = await client.sign_in(email=payload.email, password=payload.password)
    except SupabaseAuthError as exc:
        if exc.status_code == 429:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many login attempts. Please wait a moment and try again.",
            ) from exc
        # Provide user-friendly error messages for common authentication failures
        error_message = exc.message.lower() if exc.message else ""
        if "invalid" in error_message and ("credentials" in error_message or "login" in error_message or "email or password" in error_message):
            # Wrong email or password - don't reveal which one
            raise NotAuthenticatedError(
                "Incorrect email or password. Please check your credentials and try again."
            ) from exc
        elif "not found" in error_message or "user not found" in error_message or "email not confirmed" in error_message:
            # Account doesn't exist or not confirmed
            raise NotAuthenticatedError(
                "No account found with this email. New here? Create an account to get started."
            ) from exc
        elif "email not confirmed" in error_message or "confirmation" in error_message:
            # Email confirmation pending
            raise NotAuthenticatedError(
                "Please verify your email address before logging in. Check your inbox for the confirmation link."
            ) from exc
        else:
            # Generic fallback with helpful context
            raise NotAuthenticatedError(
                exc.message or "Unable to log in. Please check your email and password, or create an account if you're new."
            ) from exc

    # Check whether this user is an admin in the local app_users table.
    # The role is never stored in the Supabase JWT itself (Supabase has no concept of
    # Myelin's roles) -- we look it up from the row that get_or_create_app_user provisioned.
    is_admin = False
    user_id_str = result.get("user_id")
    if user_id_str:
        try:
            uid = uuid.UUID(str(user_id_str))
            app_user = await session.get(AppUser, uid)
            if app_user is not None:
                is_admin = app_user.role == "admin"
        except (ValueError, TypeError):
            pass  # Malformed UUID -- treat as non-admin, not a crash

    return AuthResponse(**result, is_admin=is_admin)


@router.post(
    "/refresh",
    response_model=AuthResponse,
    summary="Renew an expiring session",
    description="Proxies to Supabase Auth's refresh grant and returns a new session. Supabase "
    "access tokens expire after roughly an hour -- shorter than a four-quarter run takes to "
    "play -- so a client holding a `refresh_token` calls this instead of letting the session "
    "die mid-run. 401 `{\"error\": \"not_authenticated\"}` if the refresh token is itself "
    "expired, already used, or revoked: at that point the user really does have to log in "
    "again. 429 (plain `detail`) if Supabase's rate limit was hit.",
)
async def refresh(
    payload: RefreshRequest,
    client: SupabaseAuthClient = Depends(get_supabase_auth_client),
) -> AuthResponse:
    try:
        result = await client.refresh_session(refresh_token=payload.refresh_token)
    except SupabaseAuthError as exc:
        if exc.status_code == 429:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests. Please wait a moment before trying again.",
            ) from exc
        # A refresh token Supabase will not trade is indistinguishable, to the client, from
        # having no session at all -- same 401 envelope as `/login` and `get_current_user`,
        # so the frontend's one 401 handler is all that is needed to send them to login.
        raise NotAuthenticatedError(
            "Your session has expired. Please log in again to continue."
        ) from exc
    return AuthResponse(**result)


@router.post(
    "/forgot-password",
    summary="Request a password-reset email",
    description="Proxies to Supabase Auth's `/recover`. Always returns the same generic ack "
    "whether or not the email is registered -- Supabase itself never reveals that, to prevent "
    "email enumeration. The emailed link lands on the caller's own origin when this API "
    "already accepts browser requests from it (so a reset started on production lands on "
    "production, and one started on a preview deploy lands on that preview), and on the "
    "configured `FRONTEND_URL` otherwise. 500 if Supabase would not honour that landing page "
    "-- no email is sent in that case, because the link in it would 404. 429 (plain `detail`) "
    "if Supabase's rate limit was hit.",
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    client: SupabaseAuthClient = Depends(get_supabase_auth_client),
    settings: Settings = Depends(get_settings),
) -> dict:
    # The link has to point back at whichever deployment the caller is on. `Origin` is only
    # trusted when it is already on this API's own CORS allow-list; anything else falls back
    # to the configured FRONTEND_URL.
    redirect_to = settings.reset_redirect_for(request.headers.get("origin"))

    try:
        await client.request_password_reset(email=payload.email, redirect_to=redirect_to)
    except PasswordResetMisconfigured as exc:
        # Nothing was sent, and saying "check your inbox" here is exactly the lie that made
        # this bug invisible. The operator-facing fix goes to the log, not to the user.
        logger.error("Refused to send a password-reset email. %s", exc.detail)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Password reset is temporarily unavailable. Please contact support or try again later.",
        ) from exc
    except SupabaseAuthError as exc:
        if exc.status_code == 429:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many password reset attempts. Please wait an hour before trying again.",
            ) from exc
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            exc.message or "Unable to process password reset request. Please try again.",
        ) from exc
    return {"message": "If that email is registered, a password reset link has been sent. Check your inbox (and spam folder)."}



@router.post(
    "/reset-password",
    summary="Complete a password reset",
    description="Proxies to Supabase Auth's `PUT /user`, authenticated with the short-lived "
    "recovery `access_token` from the emailed reset link (the frontend reads it out of the "
    "URL fragment after Supabase's redirect). 422 (plain `detail`) if the token is expired/"
    "invalid or Supabase rejects the new password itself.",
)
async def reset_password(
    payload: ResetPasswordRequest,
    client: SupabaseAuthClient = Depends(get_supabase_auth_client),
) -> dict:
    try:
        await client.confirm_password_reset(
            access_token=payload.access_token, new_password=payload.new_password
        )
    except SupabaseAuthError as exc:
        error_message = exc.message.lower() if exc.message else ""
        if "expired" in error_message or "invalid" in error_message:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This password reset link has expired or is invalid. Please request a new one.",
            ) from exc
        elif "password" in error_message and ("weak" in error_message or "short" in error_message):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Password is too weak. Please use at least 8 characters with a mix of letters and numbers.",
            ) from exc
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            exc.message or "Unable to reset password. Please try again or request a new reset link.",
        ) from exc
    return {"message": "Password updated successfully. You can now log in with your new password."}
