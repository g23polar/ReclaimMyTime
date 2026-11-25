"""FastAPI wiring: the auth runtime, a request-scoped repository, and bearer dependencies.

`AuthRuntime` holds the long-lived collaborators (JWKS cache, Apple client, Google revoker,
cipher, clock, nonce guard) plus a `repository_provider` — an async context manager that yields an
`AuthRepository` inside one transaction (commit on success, rollback on error).
It lives on `app.state.auth_runtime`; tests install one with in-memory parts. If none is
installed, `default_runtime()` builds the production one lazily from env on first use.

Transactions, two modes:
- Request-scoped (default): `ServiceDep` / `CurrentUser` / `CurrentSession` share one
  transaction, committed before the response is sent.
- Per-step (opt-in): `StepwiseCurrentUser` authenticates in its own transaction, committed
  before the route body runs; the route then opens its own short transactions (e.g.
  `DeletionServiceDep`), so no connection or lock is held across a slow external call.
  A per-step route must not also depend on `ServiceDep`/`RepoDep`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.apple import (
    AppleClient,
    AppleIdentityVerifier,
    HttpAppleClient,
    HttpJwksFetcher,
    InMemoryNonceGuard,
    JwksCache,
    JwksFetcher,
    NonceGuard,
    UnconfiguredAppleClient,
)
from app.auth.clock import Clock, SystemClock
from app.auth.crypto import TokenCipher
from app.auth.errors import AppleNotConfiguredError, TimezoneInvalidError, UnauthenticatedError
from app.auth.repository import (
    AuthRepository,
    RepositoryProvider,
    SessionRecord,
    SqlAlchemyAuthRepository,
    UserRecord,
)
from app.auth.service import AccountDeletionService, Authenticated, AuthService
from app.auth.sessions import SessionPolicy
from app.auth.settings import AuthSettings
from app.gcal.client import GoogleRevoker, HttpGoogleRevoker
from app.gcal.settings import GcalSettings

_IANA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")  # openapi IanaTimezone


@dataclass
class AuthRuntime:
    settings: AuthSettings
    clock: Clock
    verifier: AppleIdentityVerifier
    nonce_guard: NonceGuard
    apple: AppleClient
    cipher: TokenCipher
    policy: SessionPolicy
    repository_provider: RepositoryProvider
    google: GoogleRevoker  # DELETE /me revokes the Google grant too (T4.1)
    # False when Sign in with Apple is not configured (no APPLE_BUNDLE_ID; T6.0, D26):
    # /auth/apple answers 503 apple_unavailable; everything else is unaffected.
    apple_enabled: bool = True

    def service(self, repo: AuthRepository) -> AuthService:
        return AuthService(
            repo,
            verifier=self.verifier,
            nonce_guard=self.nonce_guard,
            apple=self.apple,
            cipher=self.cipher,
            policy=self.policy,
            clock=self.clock,
        )

    def deletion_service(self) -> AccountDeletionService:
        return AccountDeletionService(
            self.repository_provider,
            apple=self.apple,
            cipher=self.cipher,
            google=self.google,
            clock=self.clock,
        )


def build_runtime(
    settings: AuthSettings,
    *,
    repository_provider: RepositoryProvider,
    clock: Clock | None = None,
    jwks_fetcher: JwksFetcher | None = None,
    apple_client: AppleClient | None = None,
    nonce_guard: NonceGuard | None = None,
    google_revoker: GoogleRevoker | None = None,
) -> AuthRuntime:
    """AuthConfigError if Apple is configured (APPLE_BUNDLE_ID set), no `apple_client` is
    injected, and the APPLE_TEAM_ID / APPLE_KEY_ID / APPLE_PRIVATE_KEY credentials the HTTP
    client needs are missing. Without APPLE_BUNDLE_ID, Apple is disabled (T6.0) and an
    `UnconfiguredAppleClient` stands in unless one is injected. Without `google_revoker`,
    Google's default revoke endpoint is used (it needs no client credentials)."""
    clock = clock or SystemClock()
    if apple_client is None:
        if settings.apple_configured:
            settings.require_apple_credentials()
            apple_client = HttpAppleClient(settings, clock)
        else:
            apple_client = UnconfiguredAppleClient()
    jwks = JwksCache(
        jwks_fetcher or HttpJwksFetcher(settings.apple_jwks_url, settings.apple_http_timeout_s),
        clock,
        ttl=settings.jwks_ttl,
        min_refresh_interval=settings.jwks_min_refresh_interval,
        refresh_backoff=settings.jwks_refresh_backoff,
    )
    return AuthRuntime(
        settings=settings,
        clock=clock,
        verifier=AppleIdentityVerifier(jwks, settings, clock),
        nonce_guard=nonce_guard
        or InMemoryNonceGuard(clock, retention_leeway=settings.token_leeway),
        apple=apple_client,
        cipher=TokenCipher(settings.fernet_keys),
        policy=SessionPolicy(
            sliding=settings.session_sliding,
            absolute=settings.session_absolute,
            last_seen_interval=settings.last_seen_interval,
        ),
        repository_provider=repository_provider,
        google=google_revoker or HttpGoogleRevoker(),
        apple_enabled=settings.apple_configured,
    )


def sqlalchemy_repository_provider(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> RepositoryProvider:
    @asynccontextmanager
    async def provide() -> AsyncIterator[AuthRepository]:
        async with sessionmaker() as session, session.begin():
            yield SqlAlchemyAuthRepository(session)

    return provide


def default_runtime(*, apple_client: AppleClient | None = None) -> AuthRuntime:
    """Production runtime from env (AUTH_FERNET_KEYS, DATABASE_URL, optional APPLE_*).

    Apple is optional (T6.0): with no APPLE_* variable the runtime builds with Apple
    disabled. Fails fast (AuthConfigError) on partial Apple configuration (a bundle id
    without credentials, unless `apple_client` is injected, or credentials without a bundle
    id).

    TODO(T2.1): replace the auth-local engine with the shared engine/sessionmaker from
    app/db once T2.1 lands it.
    """
    import os

    from sqlalchemy.ext.asyncio import create_async_engine

    settings = AuthSettings.from_env(require_apple_credentials=apple_client is None)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set")
    # hide_parameters: bound values (token hashes, emails, encrypted refresh tokens) never
    # reach logs or exception messages.
    engine = create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    gcal = GcalSettings.from_env(require_oauth=False)  # revoke endpoint/timeout only
    return build_runtime(
        settings,
        repository_provider=sqlalchemy_repository_provider(sessionmaker),
        apple_client=apple_client,
        google_revoker=HttpGoogleRevoker.from_settings(gcal),
    )


_runtime_lock = asyncio.Lock()


async def get_auth_runtime(request: Request) -> AuthRuntime:
    state = request.app.state
    runtime = getattr(state, "auth_runtime", None)
    if runtime is None:
        async with _runtime_lock:
            runtime = getattr(state, "auth_runtime", None)
            if runtime is None:
                runtime = default_runtime()
                state.auth_runtime = runtime
    return runtime


RuntimeDep = Annotated[AuthRuntime, Depends(get_auth_runtime)]


async def get_auth_repository(runtime: RuntimeDep) -> AsyncIterator[AuthRepository]:
    # One transaction per request, committed before the response is sent.
    async with runtime.repository_provider() as repo:
        yield repo


RepoDep = Annotated[AuthRepository, Depends(get_auth_repository, scope="function")]


async def get_auth_service(runtime: RuntimeDep, repo: RepoDep) -> AuthService:
    return runtime.service(repo)


ServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def get_authenticated(
    service: ServiceDep,
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> Authenticated:
    token = _bearer_token(authorization)
    if token is None:
        raise UnauthenticatedError()
    return await service.authenticate(token)


AuthenticatedDep = Annotated[Authenticated, Depends(get_authenticated)]


async def current_session(auth: AuthenticatedDep) -> SessionRecord:
    return auth.session


async def current_user(auth: AuthenticatedDep) -> UserRecord:
    return auth.user


CurrentSession = Annotated[SessionRecord, Depends(current_session)]
CurrentUser = Annotated[UserRecord, Depends(current_user)]


# ----------------------------------------------------------------- per-step transactions


async def get_authenticated_stepwise(
    runtime: RuntimeDep,
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> Authenticated:
    """Authenticate in a transaction of its own, committed before the route runs."""
    token = _bearer_token(authorization)
    if token is None:
        raise UnauthenticatedError()
    async with runtime.repository_provider() as repo:
        return await runtime.service(repo).authenticate(token)


async def stepwise_current_user(
    auth: Annotated[Authenticated, Depends(get_authenticated_stepwise)],
) -> UserRecord:
    return auth.user


StepwiseCurrentUser = Annotated[UserRecord, Depends(stepwise_current_user)]


async def get_deletion_service(runtime: RuntimeDep) -> AccountDeletionService:
    return runtime.deletion_service()


DeletionServiceDep = Annotated[AccountDeletionService, Depends(get_deletion_service)]


async def require_apple_configured(runtime: RuntimeDep) -> None:
    """503 `apple_unavailable` before anything else when Apple is not configured (T6.0)."""
    if not runtime.apple_enabled:
        raise AppleNotConfiguredError()


async def require_timezone(
    x_timezone: Annotated[str | None, Header(alias="X-Timezone")] = None,
) -> ZoneInfo:
    """X-Timezone is required on every authenticated route (NOTES.md, D11)."""
    if not x_timezone or len(x_timezone) > 64 or not _IANA_RE.match(x_timezone):
        raise TimezoneInvalidError()
    try:
        return ZoneInfo(x_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise TimezoneInvalidError() from None


DeviceTimezone = Annotated[ZoneInfo, Depends(require_timezone)]
