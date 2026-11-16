"""Auth configuration, built explicitly via `AuthSettings.from_env()`.

Nothing reads env at import time.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

APPLE_ISSUER = "https://appleid.apple.com"


class AuthConfigError(RuntimeError):
    """Required auth configuration is missing or malformed."""


# Needed to sign the client_secret for Apple's /auth/token and /auth/revoke.
APPLE_CREDENTIAL_VARS = ("APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY")


def parse_fernet_keys(raw: str) -> tuple[tuple[int, str], ...]:
    """Parse `"2:<fernet-key>,1:<fernet-key>"`; the first entry is the primary (encrypting) key."""
    keys: list[tuple[int, str]] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        key_id, sep, key = part.partition(":")
        if not sep or not key_id.strip().isdigit() or not key.strip():
            raise AuthConfigError("AUTH_FERNET_KEYS entries must look like '<key_id>:<key>'")
        keys.append((int(key_id), key.strip()))
    if not keys:
        raise AuthConfigError("AUTH_FERNET_KEYS must contain at least one key")
    if len({k for k, _ in keys}) != len(keys):
        raise AuthConfigError("AUTH_FERNET_KEYS key ids must be unique")
    return tuple(keys)


@dataclass(frozen=True)
class AuthSettings:
    # Expected `aud` of identity tokens; also the client_id for /auth/token. Empty = Sign in
    # with Apple is not configured (T6.0: Apple is optional; /auth/apple answers 503).
    apple_bundle_id: str
    fernet_keys: tuple[tuple[int, str], ...] = field(repr=False)
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key_pem: str = field(default="", repr=False)  # ES256 key for client_secret
    apple_issuer: str = APPLE_ISSUER
    apple_jwks_url: str = "https://appleid.apple.com/auth/keys"
    apple_token_url: str = "https://appleid.apple.com/auth/token"
    apple_revoke_url: str = "https://appleid.apple.com/auth/revoke"
    apple_http_timeout_s: float = 10.0
    jwks_ttl: timedelta = timedelta(hours=6)
    jwks_min_refresh_interval: timedelta = timedelta(minutes=1)  # kid-miss refetch rate limit
    jwks_refresh_backoff: timedelta = timedelta(seconds=60)  # no refetch after a failed one
    token_leeway: timedelta = timedelta(seconds=30)  # exp leeway; also nonce retention
    session_sliding: timedelta = timedelta(days=30)
    session_absolute: timedelta = timedelta(days=180)
    last_seen_interval: timedelta = timedelta(hours=1)

    @property
    def apple_configured(self) -> bool:
        """Sign in with Apple is offered iff APPLE_BUNDLE_ID is set (T6.0)."""
        return bool(self.apple_bundle_id)

    def missing_apple_credentials(self) -> tuple[str, ...]:
        values = (self.apple_team_id, self.apple_key_id, self.apple_private_key_pem)
        return tuple(
            name for name, value in zip(APPLE_CREDENTIAL_VARS, values, strict=True) if not value
        )

    def require_apple_credentials(self) -> None:
        """AuthConfigError naming every missing APPLE_* credential variable."""
        missing = self.missing_apple_credentials()
        if missing:
            raise AuthConfigError(
                f"{', '.join(missing)} must be set (required by the Apple REST client)"
            )

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, require_apple_credentials: bool = True
    ) -> AuthSettings:
        """Build from env. Pass `require_apple_credentials=False` only when an Apple
        client is injected (the HTTP client needs APPLE_TEAM_ID/KEY_ID/PRIVATE_KEY).

        Apple is optional (T6.0): with no APPLE_BUNDLE_ID and no APPLE_* credential the
        settings build with Apple unconfigured. Partial Apple configuration fails fast: a
        bundle id without the credentials (unless `require_apple_credentials=False`), or
        any credential without a bundle id."""
        env = os.environ if env is None else env

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise AuthConfigError(f"{name} must be set")
            return value

        def days(name: str, default: int) -> timedelta:
            return timedelta(days=int(env.get(name, default)))

        settings = cls(
            apple_bundle_id=env.get("APPLE_BUNDLE_ID", "").strip(),
            fernet_keys=parse_fernet_keys(required("AUTH_FERNET_KEYS")),
            apple_team_id=env.get("APPLE_TEAM_ID", "").strip(),
            apple_key_id=env.get("APPLE_KEY_ID", "").strip(),
            apple_private_key_pem=env.get("APPLE_PRIVATE_KEY", "").strip().replace("\\n", "\n"),
            apple_issuer=env.get("APPLE_ISSUER", APPLE_ISSUER),
            apple_jwks_url=env.get("APPLE_JWKS_URL", cls.apple_jwks_url),
            apple_token_url=env.get("APPLE_TOKEN_URL", cls.apple_token_url),
            apple_revoke_url=env.get("APPLE_REVOKE_URL", cls.apple_revoke_url),
            jwks_ttl=timedelta(seconds=int(env.get("APPLE_JWKS_TTL_SECONDS", 6 * 3600))),
            session_sliding=days("SESSION_SLIDING_DAYS", 30),
            session_absolute=days("SESSION_ABSOLUTE_DAYS", 180),
        )
        if not settings.apple_configured:
            stray = [n for n in APPLE_CREDENTIAL_VARS if env.get(n, "").strip()]
            if stray:
                raise AuthConfigError(
                    f"APPLE_BUNDLE_ID must be set when {', '.join(stray)} is set "
                    "(or unset every APPLE_* variable to run without Sign in with Apple)"
                )
        elif require_apple_credentials:
            settings.require_apple_credentials()
        return settings
