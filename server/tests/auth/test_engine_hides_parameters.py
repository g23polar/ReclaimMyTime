"""Issue #6: the auth runtime's own engine (default_runtime) hides bound SQL parameters, so a
failed auth query never puts token hashes, emails or encrypted refresh tokens into error text.

Skipped unless TEST_DATABASE_URL is set; it must name a test database (tests/dbguard.py).
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthRuntime, default_runtime
from tests.dbguard import resolve_test_database_url

DATABASE_URL = resolve_test_database_url()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")

_APPLE_VARS = ("APPLE_BUNDLE_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY")

TOKEN_HASH = "9f2c4e7a1b3d5f60817263544536271809abcdef0123456789fedcba98765432"
EMAIL = "leaky.user+issue6@example.com"
ENCRYPTED_REFRESH = b"gAAAAABleakyEncryptedRefreshTokenIssue6"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> AuthRuntime:
    assert DATABASE_URL
    for name in _APPLE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_FERNET_KEYS", f"1:{Fernet.generate_key().decode()}")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    return default_runtime()


async def test_failed_auth_query_hides_bound_values(runtime: AuthRuntime) -> None:
    session: AsyncSession | None = None
    try:
        with pytest.raises(DBAPIError) as info:
            async with runtime.repository_provider() as repo:
                session = repo._s  # type: ignore[attr-defined]
                await session.execute(
                    text(
                        "SELECT 1 FROM issue6_no_such_table "
                        "WHERE token_hash = :token_hash AND email = :email "
                        "AND refresh = :refresh"
                    ),
                    {"token_hash": TOKEN_HASH, "email": EMAIL, "refresh": ENCRYPTED_REFRESH},
                )
    finally:
        if session is not None:
            await session.bind.dispose()
    message = str(info.value)
    assert "issue6_no_such_table" in message
    assert "SQL parameters hidden" in message
    for value in (TOKEN_HASH, EMAIL, ENCRYPTED_REFRESH.decode()):
        assert value not in message


async def test_runtime_runs_a_query_on_the_test_database(runtime: AuthRuntime) -> None:
    session: AsyncSession | None = None
    try:
        async with runtime.repository_provider() as repo:
            session = repo._s  # type: ignore[attr-defined]
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        if session is not None:
            await session.bind.dispose()
