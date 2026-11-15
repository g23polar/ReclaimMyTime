from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.auth.dependencies import AuthRuntime, build_runtime
from app.auth.errors import register_error_handlers
from app.auth.repository import AuthRepository, InMemoryAuthRepository
from app.auth.router import router
from app.auth.service import AuthService
from app.auth.settings import AuthSettings
from tests.auth.fixtures import (
    BUNDLE_ID,
    FakeAppleClient,
    FakeJwksFetcher,
    FrozenClock,
    SigningKey,
    TokenFactory,
)

T0 = datetime(2026, 11, 15, 2, 40, 3, tzinfo=UTC)
TZ_HEADERS = {"X-Timezone": "America/Los_Angeles"}

_nonce_counter = itertools.count()


@pytest.fixture(scope="session")
def signing_key() -> SigningKey:
    return SigningKey(kid="fixture-kid-1")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def settings() -> AuthSettings:
    return AuthSettings(
        apple_bundle_id=BUNDLE_ID,
        fernet_keys=((1, Fernet.generate_key().decode()),),
    )


@pytest.fixture
def jwks_fetcher(signing_key: SigningKey) -> FakeJwksFetcher:
    return FakeJwksFetcher([signing_key])


@pytest.fixture
def tokens(signing_key: SigningKey, clock: FrozenClock) -> TokenFactory:
    return TokenFactory(signing_key, clock)


@pytest.fixture
def apple(tokens: TokenFactory) -> FakeAppleClient:
    return FakeAppleClient(tokens)


@pytest.fixture
def repo() -> InMemoryAuthRepository:
    return InMemoryAuthRepository()


@pytest.fixture
def runtime(
    settings: AuthSettings,
    clock: FrozenClock,
    jwks_fetcher: FakeJwksFetcher,
    apple: FakeAppleClient,
    repo: InMemoryAuthRepository,
) -> AuthRuntime:
    @asynccontextmanager
    async def provide() -> AsyncIterator[AuthRepository]:
        async with repo.transaction() as tx:
            yield tx

    return build_runtime(
        settings,
        repository_provider=provide,
        clock=clock,
        jwks_fetcher=jwks_fetcher,
        apple_client=apple,
    )


@pytest.fixture
def service(runtime: AuthRuntime, repo: InMemoryAuthRepository) -> AuthService:
    return runtime.service(repo)


@pytest.fixture
def app(runtime: AuthRuntime) -> FastAPI:
    application = FastAPI()
    register_error_handlers(application)
    application.include_router(router)
    application.state.auth_runtime = runtime
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def new_nonce():
    def make() -> str:
        return f"raw-nonce-{next(_nonce_counter):08d}-abcdef"

    return make
