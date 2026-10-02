import json
import re
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# The frontend route that completes a password reset. Lives here because it is the one piece
# of the frontend's own routing this backend has to know: every recovery email points at it.
RESET_PATH = "/reset-password"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Myelin Backend"
    environment: str = "development"

    database_url: str
    redis_url: str

    # `NoDecode` on every list field: without it pydantic-settings JSON-decodes the raw env
    # string before validation runs, so a plain `a,b,c` -- the only thing a hosting dashboard's
    # env-var box invites you to type -- raises SettingsError and the process dies at import,
    # before a single log line. `_split_csv` accepts both that and a JSON array.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000", "http://127.0.0.1:3000", "https://myelinworks.com", "https://www.myelinworks.com"]

    # Vercel mints a distinct origin per deployment (`<project>-<hash>-<scope>.vercel.app`), so an
    # enumerated allow-list goes stale the next time the frontend deploys and every browser call
    # starts failing preflight. This regex covers the whole project's deployment family in one
    # entry. Anchored at both ends -- an unanchored pattern would match any host that merely
    # *contains* the project name, e.g. `evil-myelin-frontend.attacker.com`.
    cors_origin_regex: str = ""

    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_secret_key: str = ""
    supabase_jwks_url: str = ""

    # The browser origin this API's frontend is served from. Used to derive any URL that has
    # to point *back* at the frontend -- currently just the password-reset landing page.
    frontend_url: str = "http://localhost:3000"

    # Where Supabase redirects after a user clicks the password-reset email link (the
    # frontend's own reset-password page). Passed as `redirect_to` on `/auth/v1/recover`.
    # Left blank on purpose: `password_reset_redirect` below derives it from `frontend_url`
    # rather than letting a blank value mean "send no redirect_to at all", which is how reset
    # links silently ended up on the Supabase project's Site URL (a 404) instead of this page.
    password_reset_redirect_url: str = ""

    # The valid-role universe, kept in config so a new role is an env change, not a code
    # change. Which of these roles get cross-ownership read access is a business rule, not
    # a config knob -- see authorization_service.INSTRUCTOR_ROLES.
    app_roles: Annotated[list[str], NoDecode] = ["student", "instructor", "admin"]

    @property
    def password_reset_redirect(self) -> str:
        """The configured fallback `redirect_to` -- never empty.

        Supabase only honours a `redirect_to` that its own Auth -> URL Configuration ->
        Redirect URLs allow-list matches; anything else (including *no* `redirect_to`) is
        silently replaced with the project's Site URL. That silent substitution is what turned
        every reset link into a 404, so this always resolves to a real page and
        `probe_password_reset_redirect` checks at startup that Supabase actually accepts it.

        Used only when the request carries no origin this API recognises -- see
        `reset_redirect_for`, which is what the route actually calls.
        """
        return self.password_reset_redirect_url or f"{self.frontend_url.rstrip('/')}{RESET_PATH}"

    def allows_origin(self, origin: str | None) -> bool:
        """Whether `origin` is one this API already accepts browser calls from.

        Deliberately the same two knobs CORS is configured with: an origin the operator has
        declared to be our frontend is an origin we are willing to send a password-reset link
        back to. Nothing else qualifies -- `Origin` is just a request header, forgeable by
        anyone with curl, and an unchecked one would put an attacker's URL in a recovery email.
        """
        if not origin:
            return False
        if origin in self.cors_origins:
            return True
        return bool(self.cors_origin_regex and re.fullmatch(self.cors_origin_regex, origin))

    def reset_redirect_for(self, origin: str | None) -> str:
        """Where a reset link requested from `origin` should land the user.

        The link has to point back at the deployment the user is actually on, and a static env
        var cannot know that: one backend serves production, every Vercel preview, and local
        dev. Reading it off the (allow-list-checked) request origin makes a reset requested
        from production land on production by construction, instead of depending on someone
        having remembered to set FRONTEND_URL on the host -- which is exactly how every
        emailed link ended up pointing at `http://localhost:3000` and, once Supabase swapped
        in the project's Site URL, at a 404.
        """
        if self.allows_origin(origin):
            return f"{origin.rstrip('/')}{RESET_PATH}"
        return self.password_reset_redirect

    @field_validator("cors_origins", "app_roles", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept `a,b,c` or `["a","b","c"]`. Empty entries are dropped so a trailing comma
        can't produce an empty-string origin, which CORS would then try to match."""
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if stripped.startswith("["):
            return json.loads(stripped)
        return [item.strip() for item in stripped.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
