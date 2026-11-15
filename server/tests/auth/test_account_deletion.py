"""DELETE /me: per-user lock, no transaction held across Apple revocation, re-check pass."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

import pytest

from app.auth.dependencies import AuthRuntime
from app.auth.errors import AppleRevocationFailedError
from app.auth.repository import InMemoryAuthRepository
from app.auth.service import AccountDeletionService, AuthService
from tests.auth.fixtures import DEFAULT_SUB, FakeAppleClient, TokenFactory

KEY = ("apple", DEFAULT_SUB)


@pytest.fixture
def deletion(runtime: AuthRuntime) -> AccountDeletionService:
    return runtime.deletion_service()


async def _sign_in(service: AuthService, tokens: TokenFactory, nonce: str) -> uuid.UUID:
    return (await service.sign_in_with_apple(tokens.make(raw_nonce=nonce), "code", nonce)).user.id


async def test_no_transaction_or_lock_held_while_revoking(
    deletion: AccountDeletionService,
    service: AuthService,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    user_id = await _sign_in(service, tokens, new_nonce())
    observed: list[tuple[int, bool]] = []
    original = apple.revoke_refresh_token

    async def revoke(token: str) -> None:
        observed.append((repo.open_transactions, repo.is_user_locked(user_id)))
        await original(token)

    apple.revoke_refresh_token = revoke  # type: ignore[method-assign]
    await deletion.delete_account(user_id)
    assert observed == [(0, False)]
    assert repo.lock_log == [user_id, user_id]  # tx A and tx B both locked
    assert repo.users == {} and repo.identities == {}


async def test_deletion_waits_for_concurrent_sign_in_lock(
    deletion: AccountDeletionService,
    service: AuthService,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    user_id = await _sign_in(service, tokens, new_nonce())
    async with repo.transaction() as sign_in_tx:
        await sign_in_tx.lock_user(user_id)
        task = asyncio.create_task(deletion.delete_account(user_id))
        await asyncio.sleep(0.01)
        assert not task.done() and apple.revoked == []
    await asyncio.wait_for(task, timeout=1)
    assert apple.revoked == [apple.refresh_token]
    assert repo.users == {}


async def test_credential_stored_during_revoke_is_revoked_too(
    deletion: AccountDeletionService,
    runtime: AuthRuntime,
    service: AuthService,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    user_id = await _sign_in(service, tokens, new_nonce())
    original = apple.revoke_refresh_token
    raced = False

    async def revoke(token: str) -> None:
        nonlocal raced
        await original(token)
        if not raced:  # a returning sign-in lands while Apple is being called
            raced = True
            apple.refresh_token = "apple-refresh-token-0002"
            nonce = new_nonce()
            async with runtime.repository_provider() as tx:
                await runtime.service(tx).sign_in_with_apple(
                    tokens.make(raw_nonce=nonce), "code-2", nonce
                )

    apple.revoke_refresh_token = revoke  # type: ignore[method-assign]
    await asyncio.wait_for(deletion.delete_account(user_id), timeout=1)
    assert apple.revoked == ["apple-refresh-token-0001", "apple-refresh-token-0002"]
    assert repo.users == {} and repo.identities == {} and repo.sessions == {}


async def test_reencrypted_same_token_is_not_revoked_twice(
    deletion: AccountDeletionService,
    runtime: AuthRuntime,
    service: AuthService,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    from dataclasses import replace

    user_id = await _sign_in(service, tokens, new_nonce())
    original = apple.revoke_refresh_token

    async def revoke(token: str) -> None:
        await original(token)
        stored = repo.identities[KEY]
        repo.identities[KEY] = replace(
            stored, apple_refresh_token_enc=runtime.cipher.encrypt(token)
        )

    apple.revoke_refresh_token = revoke  # type: ignore[method-assign]
    await deletion.delete_account(user_id)
    assert apple.revoked == [apple.refresh_token]
    assert repo.users == {}


async def test_credential_churn_is_bounded(
    deletion: AccountDeletionService,
    runtime: AuthRuntime,
    service: AuthService,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    from dataclasses import replace

    user_id = await _sign_in(service, tokens, new_nonce())
    original = apple.revoke_refresh_token
    counter = iter(range(100))

    async def revoke(token: str) -> None:
        await original(token)
        fresh = f"churned-token-{next(counter)}"
        repo.identities[KEY] = replace(
            repo.identities[KEY], apple_refresh_token_enc=runtime.cipher.encrypt(fresh)
        )

    apple.revoke_refresh_token = revoke  # type: ignore[method-assign]
    with pytest.raises(AppleRevocationFailedError) as info:
        await deletion.delete_account(user_id)
    assert info.value.status_code == 503
    assert info.value.details == {"reason": "apple_error"}
    assert len(apple.revoked) == 3  # initial pass + 2 extra passes
    assert user_id in repo.users  # nothing deleted
    assert repo.open_transactions == 0 and not repo.is_user_locked(user_id)


async def test_delete_route_authenticates_in_its_own_committed_transaction(
    client,  # noqa: ANN001
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    from tests.auth.conftest import TZ_HEADERS

    nonce = new_nonce()
    signed = (
        await client.post(
            "/auth/apple",
            json={
                "identity_token": tokens.make(raw_nonce=nonce),
                "authorization_code": "c",
                "nonce": nonce,
            },
        )
    ).json()
    observed: list[int] = []
    original = apple.revoke_refresh_token

    async def revoke(token: str) -> None:
        observed.append(repo.open_transactions)
        await original(token)

    apple.revoke_refresh_token = revoke  # type: ignore[method-assign]
    response = await client.delete(
        "/me", headers={"Authorization": f"Bearer {signed['token']}", **TZ_HEADERS}
    )
    assert response.status_code == 204
    assert observed == [0]
    assert repo.users == {}
