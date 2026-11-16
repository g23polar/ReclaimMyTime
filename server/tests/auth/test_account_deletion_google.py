"""DELETE /me with a Google connection (T4.1 #8): Google is revoked first, fail closed;
if Google succeeded but Apple then fails, the Google row is marked revoked and nothing is
deleted. In-memory repository; the Postgres path is in tests/api/test_gcal.py."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from app.auth.dependencies import AuthRuntime
from app.auth.errors import AppleUnavailableError
from app.auth.repository import GoogleCredentialRecord, InMemoryAuthRepository
from app.gcal.client import GoogleRevocationError
from app.gcal.errors import GcalRevocationFailedError
from tests.api.contract import assert_conforms
from tests.auth.conftest import TZ_HEADERS
from tests.auth.fixtures import FakeAppleClient, FakeGoogleRevoker, FrozenClock, TokenFactory

G_TOKEN = "1//0g-google-refresh-0001"


async def _sign_in(
    client: httpx.AsyncClient, tokens: TokenFactory, nonce: str
) -> tuple[uuid.UUID, dict[str, str]]:
    body = (
        await client.post(
            "/auth/apple",
            json={
                "identity_token": tokens.make(raw_nonce=nonce),
                "authorization_code": "c",
                "nonce": nonce,
            },
        )
    ).json()
    return uuid.UUID(body["user"]["id"]), {"Authorization": f"Bearer {body['token']}", **TZ_HEADERS}


G_EMAIL = "reclaim.tester@gmail.com"


def _connect_google(
    repo: InMemoryAuthRepository,
    runtime: AuthRuntime,
    user_id: uuid.UUID,
    token: str = G_TOKEN,
    email: str = G_EMAIL,
) -> None:
    repo.google[user_id] = GoogleCredentialRecord(
        refresh_token_enc=runtime.cipher.encrypt(token),
        key_id=runtime.cipher.primary_key_id,
        status="active",
        google_email=email,
    )


@pytest.fixture
async def signed_in(
    client: httpx.AsyncClient,
    tokens: TokenFactory,
    repo: InMemoryAuthRepository,
    runtime: AuthRuntime,
    new_nonce: Callable[[], str],
) -> tuple[uuid.UUID, dict[str, str]]:
    user_id, headers = await _sign_in(client, tokens, new_nonce())
    _connect_google(repo, runtime, user_id)
    return user_id, headers


async def test_google_revoked_first_then_apple_then_cascade(
    client: httpx.AsyncClient,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    google: FakeGoogleRevoker,
) -> None:
    user_id, headers = signed_in
    original = repo.delete_user

    async def spy_delete(uid: uuid.UUID) -> None:
        apple.events.append("delete")
        await original(uid)

    repo.delete_user = spy_delete  # type: ignore[method-assign]
    resp = await client.delete("/me", headers=headers)
    assert resp.status_code == 204
    assert apple.events == ["google", "revoke", "delete"]
    assert google.revoked == [G_TOKEN] and apple.revoked == [apple.refresh_token]
    assert repo.users == {} and repo.google == {}


async def test_google_failure_touches_nothing(
    client: httpx.AsyncClient,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    google: FakeGoogleRevoker,
) -> None:
    user_id, headers = signed_in
    google.revoke_error = GoogleRevocationError()
    resp = await client.delete("/me", headers=headers)
    assert resp.status_code == 503
    body = resp.json()
    assert_conforms("deleteMe", 503, body)
    assert body["error"]["code"] == "gcal_revocation_failed"
    assert body["error"]["details"] == {"reason": "google_error"}
    assert apple.events == ["google"] and apple.revoked == []
    assert user_id in repo.users and len(repo.identities) == 1 and len(repo.sessions) == 1
    assert repo.google[user_id].status == "active" and user_id not in repo.google_revoked_at


async def test_google_ok_then_apple_fails_marks_google_revoked(
    client: httpx.AsyncClient,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    google: FakeGoogleRevoker,
    clock: FrozenClock,
) -> None:
    user_id, headers = signed_in
    apple.revoke_error = AppleUnavailableError()
    resp = await client.delete("/me", headers=headers)
    assert resp.status_code == 503
    assert_conforms("deleteMe", 503, resp.json())
    assert resp.json()["error"]["code"] == "apple_unavailable"  # Apple's existing 503
    assert google.revoked == [G_TOKEN]
    assert repo.google[user_id].status == "revoked"
    assert repo.google_revoked_at[user_id] == clock.now()
    assert user_id in repo.users and len(repo.identities) == 1 and len(repo.sessions) == 1
    assert repo.open_transactions == 0 and not repo.is_user_locked(user_id)


async def test_undecryptable_google_credential_blocks_before_any_revocation(
    client: httpx.AsyncClient,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
) -> None:
    user_id, headers = signed_in
    repo.google[user_id] = replace(repo.google[user_id], refresh_token_enc=b"garbage", key_id=4)
    resp = await client.delete("/me", headers=headers)
    assert resp.status_code == 503
    assert_conforms("deleteMe", 503, resp.json())
    assert resp.json()["error"]["code"] == "gcal_revocation_failed"
    assert resp.json()["error"]["details"] == {"reason": "undecryptable_credential", "key_id": 4}
    assert apple.events == []
    assert user_id in repo.users and repo.google[user_id].status == "active"


async def test_undecryptable_apple_credential_blocks_before_google_is_revoked(
    client: httpx.AsyncClient,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
) -> None:
    user_id, headers = signed_in
    key = next(iter(repo.identities))
    repo.identities[key] = replace(repo.identities[key], apple_refresh_token_enc=b"garbage")
    resp = await client.delete("/me", headers=headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "apple_revocation_failed"
    assert apple.events == []  # Google untouched too
    assert repo.google[user_id].status == "active"


async def test_without_google_connection_google_is_not_called(
    client: httpx.AsyncClient,
    tokens: TokenFactory,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    _, headers = await _sign_in(client, tokens, new_nonce())
    assert (await client.delete("/me", headers=headers)).status_code == 204
    assert apple.events == ["revoke"]


async def test_no_transaction_or_lock_held_while_revoking_google(
    runtime: AuthRuntime,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    google: FakeGoogleRevoker,
) -> None:
    user_id, _ = signed_in
    observed: list[tuple[int, bool]] = []
    original = google.revoke_token

    async def revoke(token: str) -> None:
        observed.append((repo.open_transactions, repo.is_user_locked(user_id)))
        await original(token)

    google.revoke_token = revoke  # type: ignore[method-assign]
    await runtime.deletion_service().delete_account(user_id)
    assert observed == [(0, False)]
    assert repo.users == {}


async def test_google_reconnect_during_deletion_is_revoked_too(
    runtime: AuthRuntime,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    google: FakeGoogleRevoker,
) -> None:
    user_id, _ = signed_in
    original = google.revoke_token

    async def revoke(token: str) -> None:
        await original(token)
        if len(google.revoked) == 1:  # a gcal callback lands while Google is being called
            _connect_google(repo, runtime, user_id, "1//0g-google-refresh-0002")

    google.revoke_token = revoke  # type: ignore[method-assign]
    await asyncio.wait_for(runtime.deletion_service().delete_account(user_id), timeout=1)
    assert google.revoked == [G_TOKEN, "1//0g-google-refresh-0002"]
    assert repo.users == {} and repo.google == {}


async def test_google_credential_churn_is_bounded(
    runtime: AuthRuntime,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    google: FakeGoogleRevoker,
) -> None:
    user_id, _ = signed_in
    original = google.revoke_token
    counter = iter(range(100))

    async def revoke(token: str) -> None:
        await original(token)
        _connect_google(repo, runtime, user_id, f"churned-{next(counter)}")

    google.revoke_token = revoke  # type: ignore[method-assign]
    with pytest.raises(GcalRevocationFailedError) as info:
        await runtime.deletion_service().delete_account(user_id)
    assert info.value.details == {"reason": "google_error"}
    assert len(google.revoked) == 3
    assert user_id in repo.users
    # Review rule (T4.1 round 1): same account as a grant we revoked → marked revoked, even
    # though this last churned token was issued after our final revoke. Accepted false
    # positive: the user is prompted to reconnect rather than shown a dead grant as live.
    assert repo.google[user_id].status == "revoked"


async def test_mark_revoked_failure_does_not_mask_apple_error(
    runtime: AuthRuntime,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
) -> None:
    user_id, _ = signed_in
    apple.revoke_error = AppleUnavailableError()

    async def broken(*_: Any) -> None:
        raise RuntimeError("db down")

    repo.mark_google_revoked = broken  # type: ignore[method-assign]
    with pytest.raises(AppleUnavailableError):
        await runtime.deletion_service().delete_account(user_id)
    assert user_id in repo.users


async def test_cancelled_after_google_revoke_still_marks_row_revoked(
    runtime: AuthRuntime,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    google: FakeGoogleRevoker,
    clock: FrozenClock,
) -> None:
    user_id, _ = signed_in
    apple_called = asyncio.Event()

    async def hang(token: str) -> None:
        apple_called.set()
        await asyncio.Event().wait()  # Apple never answers; the client gives up

    apple.revoke_refresh_token = hang  # type: ignore[method-assign]
    task = asyncio.create_task(runtime.deletion_service().delete_account(user_id))
    await asyncio.wait_for(apple_called.wait(), timeout=1)
    assert google.revoked == [G_TOKEN]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.google[user_id].status == "revoked"
    assert repo.google_revoked_at[user_id] == clock.now()
    assert user_id in repo.users
    assert repo.open_transactions == 0 and not repo.is_user_locked(user_id)


async def test_cancelled_before_google_revoke_marks_nothing(
    runtime: AuthRuntime,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    google: FakeGoogleRevoker,
) -> None:
    user_id, _ = signed_in
    entered = asyncio.Event()

    async def hang(token: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    google.revoke_token = hang  # type: ignore[method-assign]
    task = asyncio.create_task(runtime.deletion_service().delete_account(user_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.google[user_id].status == "active"


async def test_same_account_token_replaced_mid_deletion_is_marked_revoked_on_failure(
    runtime: AuthRuntime,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    google: FakeGoogleRevoker,
) -> None:
    """Grant-wide revocation: a same-account token stored after our revoke started died
    with it, so recovery marks the row even though the token differs."""
    user_id, _ = signed_in
    original = google.revoke_token

    async def revoke(token: str) -> None:
        await original(token)
        _connect_google(repo, runtime, user_id, "1//0g-same-account-0002", G_EMAIL.upper())

    google.revoke_token = revoke  # type: ignore[method-assign]
    apple.revoke_error = AppleUnavailableError()
    with pytest.raises(AppleUnavailableError):
        await runtime.deletion_service().delete_account(user_id)
    assert google.revoked == [G_TOKEN]
    assert repo.google[user_id].status == "revoked"


async def test_other_account_token_replaced_mid_deletion_is_not_marked(
    runtime: AuthRuntime,
    signed_in: tuple[uuid.UUID, dict[str, str]],
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    google: FakeGoogleRevoker,
) -> None:
    user_id, _ = signed_in
    original = google.revoke_token

    async def revoke(token: str) -> None:
        await original(token)
        _connect_google(repo, runtime, user_id, "1//0g-other-account", "other@gmail.com")

    google.revoke_token = revoke  # type: ignore[method-assign]
    apple.revoke_error = AppleUnavailableError()
    with pytest.raises(AppleUnavailableError):
        await runtime.deletion_service().delete_account(user_id)
    assert repo.google[user_id].status == "active"  # that grant is alive
