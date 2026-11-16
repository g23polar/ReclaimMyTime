"""Google Calendar configuration, built explicitly via `GcalSettings.from_env()`.

Nothing reads env at import time. Endpoint URLs and timeouts are injectable (tests point
them at an httpx MockTransport; nothing in CI talks to Google).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta

from cryptography.fernet import Fernet

SCOPE_FREEBUSY = "https://www.googleapis.com/auth/calendar.freebusy"
SCOPE_APP_CREATED = "https://www.googleapis.com/auth/calendar.app.created"
# ARCH §1 / review S9: narrow from day one. openid + email only to learn google_email.
CALENDAR_SCOPES = (SCOPE_FREEBUSY, SCOPE_APP_CREATED)
REQUESTED_SCOPES = (*CALENDAR_SCOPES, "openid", "email")

GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

# Needed to build the auth URL and talk to the token endpoint. Revocation needs none.
GOOGLE_OAUTH_VARS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")


class GcalConfigError(RuntimeError):
    """Required Google configuration is missing or malformed."""


@dataclass(frozen=True)
class GcalSettings:
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    redirect_uri: str = ""  # our GET /gcal/callback, as registered with Google
    state_key: str = field(default="", repr=False)  # Fernet key; NOT one of AUTH_FERNET_KEYS
    app_callback_url: str = "reclaimmytime://gcal/callback"  # D15 placeholder scheme
    auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: str = "https://oauth2.googleapis.com/token"
    revoke_url: str = "https://oauth2.googleapis.com/revoke"
    http_timeout_s: float = 10.0
    state_ttl: timedelta = timedelta(minutes=10)
    id_token_leeway: timedelta = timedelta(seconds=30)

    def missing_oauth_settings(self) -> tuple[str, ...]:
        values = (self.client_id, self.client_secret, self.redirect_uri)
        missing = [n for n, v in zip(GOOGLE_OAUTH_VARS, values, strict=True) if not v]
        if not self.state_key:
            missing.append("GCAL_STATE_KEY")
        return tuple(missing)

    def require_oauth_settings(self) -> None:
        """Everything the consent flow (connect/callback/complete) needs: the OAuth vars,
        a usable state key, and a well-formed app callback URL. Shared by `from_env` and
        `build_gcal_runtime`. The revoke-only path (DELETE /me) never calls this."""
        missing = self.missing_oauth_settings()
        if missing:
            raise GcalConfigError(f"{', '.join(missing)} must be set (Google OAuth)")
        validate_app_callback_url(self.app_callback_url)
        try:
            Fernet(self.state_key.encode())
        except (ValueError, TypeError):
            raise GcalConfigError("GCAL_STATE_KEY must be a Fernet key") from None

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, require_oauth: bool = True
    ) -> GcalSettings:
        """Build from env. `require_oauth=False` is for revocation-only use (DELETE /me):
        it reads and validates only the revoke endpoint and timeout, so a broken consent
        config (callback URL, state key, client credentials) can never stop the auth
        runtime from being installed."""
        env = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            return env.get(name, default).strip()

        raw_timeout = get("GOOGLE_HTTP_TIMEOUT_SECONDS", str(cls.http_timeout_s))
        try:
            timeout = float(raw_timeout)
        except ValueError:
            raise GcalConfigError("GOOGLE_HTTP_TIMEOUT_SECONDS must be a number") from None
        revoke_only = cls(
            revoke_url=get("GOOGLE_REVOKE_URL", cls.revoke_url), http_timeout_s=timeout
        )
        if not require_oauth:
            return revoke_only
        settings = replace(
            revoke_only,
            client_id=get("GOOGLE_CLIENT_ID"),
            client_secret=get("GOOGLE_CLIENT_SECRET"),
            redirect_uri=get("GOOGLE_REDIRECT_URI"),
            state_key=get("GCAL_STATE_KEY"),
            app_callback_url=get("GCAL_APP_CALLBACK_URL", cls.app_callback_url),
            auth_url=get("GOOGLE_AUTH_URL", cls.auth_url),
            token_url=get("GOOGLE_TOKEN_URL", cls.token_url),
        )
        settings.require_oauth_settings()
        validate_state_key(settings.state_key, env.get("AUTH_FERNET_KEYS", ""))
        return settings


def validate_app_callback_url(url: str) -> None:
    """The callback redirect appends `?result=…`, so the base must carry no query or
    fragment of its own."""
    if not url or "?" in url or "#" in url:
        raise GcalConfigError("GCAL_APP_CALLBACK_URL must be set, without a query or fragment")


def validate_state_key(state_key: str, auth_fernet_keys: str) -> None:
    """GCAL_STATE_KEY must be a Fernet key and must not reuse an AUTH_FERNET_KEYS key
    (decision T4.1: a leaked/rotated state key never touches stored credentials)."""
    try:
        Fernet(state_key.encode())
    except (ValueError, TypeError):
        raise GcalConfigError("GCAL_STATE_KEY must be a Fernet key") from None
    auth_keys = {
        part.partition(":")[2].strip() for part in auth_fernet_keys.split(",") if part.strip()
    }
    if state_key in auth_keys:
        raise GcalConfigError("GCAL_STATE_KEY must differ from every AUTH_FERNET_KEYS key")
