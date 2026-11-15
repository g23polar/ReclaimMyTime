from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest

from app.auth.apple import (
    AppleCodeRejectedError,
    AppleUnreachableError,
    InMemoryNonceGuard,
    hash_nonce,
)
from app.auth.errors import AppleTokenInvalidError, AppleUnavailableError
from app.auth.repository import (
    EmailConflictError,
    IdentityRecord,
    InMemoryAuthRepository,
    UserRecord,
)
from app.auth.service import AuthService
from app.auth.sessions import hash_token
from tests.auth.conftest import T0
from tests.auth.fixtures import DEFAULT_SUB, FakeAppleClient, FrozenClock, SigningKey, TokenFactory


async def test_new_user_private_relay_email_is_null(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    result = await service.sign_in_with_apple(
        tokens.make(raw_nonce=nonce, email="xyz@privaterelay.appleid.com"), "code-1", nonce
    )
    assert result.is_new_user
    assert result.user.email is None
    assert repo.users[result.user.id].email is None
    identity = repo.identities[("apple", "001234.apple-subject.0001")]
    assert identity.is_private_email is True
    assert identity.email == "xyz@privaterelay.appleid.com"
    assert identity.user_id == result.user.id


async def test_verified_non_relay_email_sets_users_email(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    token = tokens.make(raw_nonce=nonce, email="Ada@Example.com", is_private_email="false")
    result = await service.sign_in_with_apple(token, "code-1", nonce)
    assert repo.users[result.user.id].email == "Ada@Example.com"
    assert repo.identities[("apple", "001234.apple-subject.0001")].is_private_email is False


async def test_unverified_email_leaves_users_email_null(
    service: AuthService, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    nonce = new_nonce()
    token = tokens.make(
        raw_nonce=nonce, email="a@example.com", email_verified="false", is_private_email=False
    )
    assert (await service.sign_in_with_apple(token, "c", nonce)).user.email is None


async def test_replayed_nonce_rejected(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    token = tokens.make(raw_nonce=nonce)
    await service.sign_in_with_apple(token, "code-1", nonce)
    with pytest.raises(AppleTokenInvalidError) as info:
        await service.sign_in_with_apple(token, "code-2", nonce)
    assert info.value.details == {"reason": "nonce_mismatch"}
    assert len(repo.sessions) == 1


async def test_returning_user_reuses_account_and_updates_identity(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    n1, n2 = new_nonce(), new_nonce()
    first = await service.sign_in_with_apple(tokens.make(raw_nonce=n1), "code-1", n1)
    created_at = repo.identities[("apple", "001234.apple-subject.0001")].created_at
    # Apple only sends email on first authorization; later tokens may omit it.
    second = await service.sign_in_with_apple(tokens.make(raw_nonce=n2, email=None), "code-2", n2)
    assert second.user.id == first.user.id
    assert not second.is_new_user
    assert len(repo.users) == 1 and len(repo.identities) == 1 and len(repo.sessions) == 2
    identity = repo.identities[("apple", "001234.apple-subject.0001")]
    assert identity.created_at == created_at
    assert identity.email == "private@privaterelay.appleid.com"
    assert apple.exchanged == ["code-1", "code-2"]


async def test_returning_user_email_becomes_linkable(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    n1, n2 = new_nonce(), new_nonce()
    first = await service.sign_in_with_apple(tokens.make(raw_nonce=n1), "c1", n1)
    token = tokens.make(raw_nonce=n2, email="real@example.com", is_private_email="false")
    await service.sign_in_with_apple(token, "c2", n2)
    assert repo.users[first.user.id].email == "real@example.com"


async def test_email_already_linked_to_other_user_is_not_duplicated(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    n1, n2 = new_nonce(), new_nonce()
    apple.code_subjects.update({"c1": "sub-a", "c2": "sub-b"})
    t1 = tokens.make(raw_nonce=n1, sub="sub-a", email="same@example.com", is_private_email=False)
    t2 = tokens.make(raw_nonce=n2, sub="sub-b", email="SAME@example.com", is_private_email=False)
    a = await service.sign_in_with_apple(t1, "c1", n1)
    b = await service.sign_in_with_apple(t2, "c2", n2)
    assert a.user.id != b.user.id
    assert repo.users[a.user.id].email == "same@example.com"
    assert repo.users[b.user.id].email is None


async def test_refresh_token_stored_encrypted_and_session_hash_only(
    service: AuthService,
    runtime,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    result = await service.sign_in_with_apple(tokens.make(raw_nonce=nonce), "code-1", nonce)
    identity = repo.identities[("apple", "001234.apple-subject.0001")]
    assert identity.apple_refresh_token_enc is not None
    assert apple.refresh_token.encode() not in identity.apple_refresh_token_enc
    assert runtime.cipher.decrypt(identity.apple_refresh_token_enc) == apple.refresh_token

    stored = repo.sessions[result.session.id]
    assert stored.token_hash == hash_token(result.token)
    # No plaintext secret anywhere in persisted rows.
    rows = [*repo.users.values(), *repo.identities.values(), *repo.sessions.values()]
    dumped = repr(rows)
    assert result.token not in dumped
    assert apple.refresh_token not in dumped
    assert "code-1" not in dumped


async def test_returning_user_keeps_refresh_token_when_apple_issues_none(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    n1, n2 = new_nonce(), new_nonce()
    await service.sign_in_with_apple(tokens.make(raw_nonce=n1), "c1", n1)
    enc = repo.identities[("apple", "001234.apple-subject.0001")].apple_refresh_token_enc
    apple.refresh_token = None
    await service.sign_in_with_apple(tokens.make(raw_nonce=n2), "c2", n2)
    assert repo.identities[("apple", "001234.apple-subject.0001")].apple_refresh_token_enc == enc


async def test_invalid_token_does_not_exchange_code(
    service: AuthService,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    with pytest.raises(AppleTokenInvalidError):
        await service.sign_in_with_apple(tokens.make(raw_nonce=nonce, aud="x"), "c1", nonce)
    assert apple.exchanged == []


# ----------------------------------------------------------------- 1. code ↔ identity binding


async def _rejected_as_code(service: AuthService, token: str, code: str, nonce: str) -> None:
    with pytest.raises(AppleTokenInvalidError) as info:
        await service.sign_in_with_apple(token, code, nonce)
    assert info.value.status_code == 401
    assert info.value.details == {"reason": "authorization_code"}


async def test_code_for_other_subject_is_rejected(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    apple.code_subjects["code-for-b"] = "subject-b"
    await _rejected_as_code(
        service, tokens.make(raw_nonce=nonce, sub="subject-a"), "code-for-b", nonce
    )
    assert repo.users == {} and repo.identities == {} and repo.sessions == {}


@pytest.mark.parametrize("variant", ["missing", "garbage", "wrong_aud", "wrong_iss", "forged"])
async def test_missing_or_unverifiable_code_id_token_is_rejected(
    variant: str,
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    apple.id_token = {
        "missing": None,
        "garbage": "not-a-jwt",
        "wrong_aud": apple.id_token_for(DEFAULT_SUB, aud="com.evil.app"),
        "wrong_iss": apple.id_token_for(DEFAULT_SUB, iss="https://evil.example.com"),
        "forged": apple.id_token_for(DEFAULT_SUB, key=SigningKey(kid=tokens.key.kid)),
    }[variant]
    nonce = new_nonce()
    await _rejected_as_code(service, tokens.make(raw_nonce=nonce), "c1", nonce)
    assert repo.identities == {} and repo.sessions == {}


async def test_code_id_token_nonce_checked_when_present(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    n1, n2 = new_nonce(), new_nonce()
    apple.id_token = apple.id_token_for(DEFAULT_SUB, include_nonce=True, nonce=hash_nonce(n1))
    assert (await service.sign_in_with_apple(tokens.make(raw_nonce=n1), "c1", n1)).user
    apple.id_token = apple.id_token_for(DEFAULT_SUB, include_nonce=True, nonce=hash_nonce(n1))
    await _rejected_as_code(service, tokens.make(raw_nonce=n2), "c2", n2)
    assert len(repo.sessions) == 1


# ----------------------------------------------------------------- 2. first sign-in race


class _LosingFirstSignInRepo(InMemoryAuthRepository):
    """Another first sign-in for the same subject commits its identity between our
    lookup and our insert; records lock and identity-write events in order."""

    def __init__(self) -> None:
        super().__init__()
        self.winner = UserRecord(id=uuid.uuid4(), email=None, created_at=T0)
        self.events: list[str] = []
        self.locked_at_update: list[bool] = []

    async def insert_identity(self, identity: IdentityRecord) -> uuid.UUID | None:
        key = (identity.provider, identity.subject)
        if key not in self.identities:
            await self.create_user(self.winner)
            await super().insert_identity(
                replace(identity, user_id=self.winner.id, apple_refresh_token_enc=b"winner")
            )
        owner = await super().insert_identity(identity)
        self.events.append(f"insert:{'won' if owner else 'lost'}")
        return owner

    async def update_identity(self, provider, subject, **kw) -> None:  # noqa: ANN001
        stored = self.identities[(provider, subject)]
        self.events.append("update")
        self.locked_at_update.append(self.is_user_locked(stored.user_id))
        await super().update_identity(provider, subject, **kw)


async def test_concurrent_first_sign_in_writes_credential_under_owner_lock(
    runtime, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    repo = _LosingFirstSignInRepo()

    nonce = new_nonce()
    async with repo.transaction() as tx:
        inner_lock = tx.lock_user

        async def lock_spy(user_id: uuid.UUID) -> None:
            repo.events.append(f"lock:{user_id}")
            await inner_lock(user_id)

        tx.lock_user = lock_spy  # type: ignore[method-assign]
        result = await runtime.service(tx).sign_in_with_apple(
            tokens.make(raw_nonce=nonce), "c1", nonce
        )
        stored = repo.identities[("apple", DEFAULT_SUB)]
    winner = repo.winner
    # The losing insert wrote nothing; the credential is written only after the owner lock.
    assert repo.events == ["insert:lost", f"lock:{winner.id}", "update"]
    assert repo.locked_at_update == [True]
    assert stored.user_id == winner.id
    assert runtime.cipher.decrypt(stored.apple_refresh_token_enc) == "apple-refresh-token-0001"
    assert result.user.id == winner.id and result.session.user_id == winner.id
    assert not result.is_new_user
    assert set(repo.users) == {winner.id}  # provisional user deleted


async def test_in_memory_identity_update_requires_owner_lock() -> None:
    repo = InMemoryAuthRepository()
    user = UserRecord(id=uuid.uuid4(), email=None, created_at=T0)
    await repo.create_user(user)
    identity = IdentityRecord(
        provider="apple",
        subject="s",
        user_id=user.id,
        email=None,
        email_verified=False,
        is_private_email=False,
        apple_refresh_token_enc=b"x",
        created_at=T0,
    )
    assert await repo.insert_identity(identity) == user.id
    assert await repo.insert_identity(identity) is None  # DO NOTHING on conflict
    async with repo.transaction() as tx:
        with pytest.raises(AssertionError):
            await tx.update_identity("apple", "s", email=None, apple_refresh_token_enc=b"y")
        await tx.lock_user(user.id)
        await tx.update_identity("apple", "s", email=None, apple_refresh_token_enc=b"y")
    assert repo.identities[("apple", "s")].apple_refresh_token_enc == b"y"


# ----------------------------------------------------------------- email metadata retention


async def test_missing_email_claim_keeps_identity_email_metadata(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    n1, n2 = new_nonce(), new_nonce()
    t1 = tokens.make(raw_nonce=n1, email="ada@example.com", is_private_email="false")
    await service.sign_in_with_apple(t1, "c1", n1)
    await service.sign_in_with_apple(tokens.make(raw_nonce=n2, email=None), "c2", n2)
    stored = repo.identities[("apple", DEFAULT_SUB)]
    assert (stored.email, stored.email_verified, stored.is_private_email) == (
        "ada@example.com",
        True,
        False,
    )


async def test_supplied_email_claim_refreshes_identity_email_metadata(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    n1, n2 = new_nonce(), new_nonce()
    t1 = tokens.make(raw_nonce=n1, email="ada@example.com", is_private_email="false")
    await service.sign_in_with_apple(t1, "c1", n1)
    t2 = tokens.make(
        raw_nonce=n2,
        email="relay@privaterelay.appleid.com",
        email_verified="false",
        is_private_email="true",
    )
    await service.sign_in_with_apple(t2, "c2", n2)
    stored = repo.identities[("apple", DEFAULT_SUB)]
    assert (stored.email, stored.email_verified, stored.is_private_email) == (
        "relay@privaterelay.appleid.com",
        False,
        True,
    )


# ----------------------------------------------------------------- 5. email unique conflict


class _ConcurrentEmailRepo(InMemoryAuthRepository):
    """Another transaction commits `email` on another user just before our write."""

    def __init__(self, email: str) -> None:
        super().__init__()
        self.email = email
        self.armed = False

    def _race(self, email: str | None) -> None:
        if self.armed and email and email.casefold() == self.email.casefold():
            self.armed = False
            other = UserRecord(id=uuid.uuid4(), email=self.email, created_at=T0)
            self.users[other.id] = other

    async def create_user(self, user: UserRecord) -> None:
        self._race(user.email)
        await super().create_user(user)

    async def set_user_email(self, user_id: uuid.UUID, email: str | None) -> None:
        self._race(email)
        await super().set_user_email(user_id, email)


async def test_new_user_email_conflict_on_write_keeps_null(
    runtime, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    repo = _ConcurrentEmailRepo("race@example.com")
    repo.armed = True
    service = runtime.service(repo)
    nonce = new_nonce()
    token = tokens.make(raw_nonce=nonce, email="race@example.com", is_private_email=False)
    result = await service.sign_in_with_apple(token, "c1", nonce)
    assert result.is_new_user and result.user.email is None
    assert repo.users[result.user.id].email is None
    assert result.session.id in repo.sessions


async def test_returning_user_email_conflict_on_write_keeps_existing(
    runtime, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    repo = _ConcurrentEmailRepo("race@example.com")
    service = runtime.service(repo)
    n1, n2 = new_nonce(), new_nonce()
    t1 = tokens.make(raw_nonce=n1, email="old@example.com", is_private_email=False)
    first = await service.sign_in_with_apple(t1, "c1", n1)
    repo.armed = True
    t2 = tokens.make(raw_nonce=n2, email="race@example.com", is_private_email=False)
    second = await service.sign_in_with_apple(t2, "c2", n2)
    assert second.user.id == first.user.id
    assert second.user.email == "old@example.com"
    assert repo.users[first.user.id].email == "old@example.com"


async def test_in_memory_repo_raises_email_conflict() -> None:
    repo = InMemoryAuthRepository()
    await repo.create_user(UserRecord(id=uuid.uuid4(), email="a@x.com", created_at=T0))
    with pytest.raises(EmailConflictError):
        await repo.create_user(UserRecord(id=uuid.uuid4(), email="A@X.com", created_at=T0))
    other = UserRecord(id=uuid.uuid4(), email=None, created_at=T0)
    await repo.create_user(other)
    with pytest.raises(EmailConflictError):
        await repo.set_user_email(other.id, "a@X.COM")


# ----------------------------------------------------------------- 6. nonce reservation


async def test_unreachable_apple_releases_nonce_for_retry(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    token = tokens.make(raw_nonce=nonce)
    apple.exchange_error = AppleUnreachableError()
    with pytest.raises(AppleUnavailableError):
        await service.sign_in_with_apple(token, "c1", nonce)
    apple.exchange_error = None
    result = await service.sign_in_with_apple(token, "c1", nonce)
    assert result.session.id in repo.sessions


@pytest.mark.parametrize("error", [AppleCodeRejectedError, AppleUnavailableError])
async def test_apple_response_commits_nonce(
    error: type[Exception],
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    token = tokens.make(raw_nonce=nonce)
    apple.exchange_error = error()
    with pytest.raises(error):
        await service.sign_in_with_apple(token, "c1", nonce)
    apple.exchange_error = None
    with pytest.raises(AppleTokenInvalidError) as info:
        await service.sign_in_with_apple(token, "c1", nonce)
    assert info.value.details == {"reason": "nonce_mismatch"}
    assert repo.sessions == {}


async def test_nonce_guard_reserve_release_commit(clock: FrozenClock) -> None:
    guard = InMemoryNonceGuard(clock, retention_leeway=timedelta(seconds=30))
    exp = T0 + timedelta(minutes=10)
    assert await guard.reserve("n", exp)
    assert not await guard.reserve("n", exp)  # in flight
    await guard.release("n")
    assert await guard.reserve("n", exp)
    await guard.commit("n")
    assert not await guard.reserve("n", exp)


# ----------------------------------------------------------------- 8. nonce retention


async def test_replay_inside_leeway_window_rejected(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    clock: FrozenClock,
    new_nonce: Callable[[], str],
) -> None:
    nonce = new_nonce()
    token = tokens.make(raw_nonce=nonce, ttl=timedelta(minutes=5))
    await service.sign_in_with_apple(token, "c1", nonce)
    clock.advance(timedelta(minutes=5, seconds=10))  # past exp, inside the 30 s leeway
    with pytest.raises(AppleTokenInvalidError) as info:
        await service.sign_in_with_apple(token, "c2", nonce)
    assert info.value.details == {"reason": "nonce_mismatch"}
    assert len(repo.sessions) == 1


# ----------------------------------------------------------------- no account without a credential


async def test_new_identity_without_refresh_token_is_rejected(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    apple.refresh_token = None
    nonce = new_nonce()
    token = tokens.make(raw_nonce=nonce)
    await _rejected_as_code(service, token, "c1", nonce)
    assert repo.users == {} and repo.identities == {} and repo.sessions == {}
    # Apple answered, so the nonce stays consumed.
    apple.refresh_token = "apple-refresh-token-0002"
    with pytest.raises(AppleTokenInvalidError) as info:
        await service.sign_in_with_apple(token, "c2", nonce)
    assert info.value.details == {"reason": "nonce_mismatch"}
    assert repo.users == {}


# ----------------------------------------------------------------- per-user lock vs deletion


async def test_existing_identity_sign_in_locks_user_before_identity_write(
    runtime, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    repo = InMemoryAuthRepository()
    events: list[str] = []
    original_lock = repo.lock_user
    original_insert, original_update = repo.insert_identity, repo.update_identity

    async def lock_spy(user_id: uuid.UUID) -> None:
        events.append(f"lock:{user_id}")
        await original_lock(user_id)

    async def insert_spy(identity: IdentityRecord) -> uuid.UUID | None:
        events.append("insert")
        return await original_insert(identity)

    async def update_spy(*args, **kw) -> None:  # noqa: ANN002, ANN003
        events.append("update")
        await original_update(*args, **kw)

    repo.lock_user = lock_spy  # type: ignore[method-assign]
    repo.insert_identity = insert_spy  # type: ignore[method-assign]
    repo.update_identity = update_spy  # type: ignore[method-assign]
    service = runtime.service(repo)
    n1, n2 = new_nonce(), new_nonce()
    first = await service.sign_in_with_apple(tokens.make(raw_nonce=n1), "c1", n1)
    assert events == ["insert"]  # brand-new identity: nothing to serialize against
    events.clear()
    await service.sign_in_with_apple(tokens.make(raw_nonce=n2), "c2", n2)
    assert events == [f"lock:{first.user.id}", "update"]


async def test_sign_in_waits_for_deletion_lock_then_creates_fresh_account(
    runtime, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    import asyncio

    repo = InMemoryAuthRepository()
    service = runtime.service(repo)
    n1, n2 = new_nonce(), new_nonce()
    first = await service.sign_in_with_apple(tokens.make(raw_nonce=n1), "c1", n1)

    async with repo.transaction() as deletion_tx:  # a deletion holding the user lock
        await deletion_tx.lock_user(first.user.id)

        async def sign_in():  # noqa: ANN202
            async with repo.transaction() as tx:
                return await runtime.service(tx).sign_in_with_apple(
                    tokens.make(raw_nonce=n2), "c2", n2
                )

        task = asyncio.create_task(sign_in())
        await asyncio.sleep(0.01)
        assert not task.done()  # blocked on the lock
        await deletion_tx.delete_user(first.user.id)
    second = await asyncio.wait_for(task, timeout=1)
    assert second.is_new_user and second.user.id != first.user.id
    assert set(repo.users) == {second.user.id}
    assert repo.identities[("apple", DEFAULT_SUB)].user_id == second.user.id


async def test_identity_without_stored_credential_needs_refresh_token(
    service: AuthService,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    n1, n2 = new_nonce(), new_nonce()
    first = await service.sign_in_with_apple(tokens.make(raw_nonce=n1), "c1", n1)
    repo.identities[("apple", DEFAULT_SUB)] = replace(
        repo.identities[("apple", DEFAULT_SUB)], apple_refresh_token_enc=None
    )
    apple.refresh_token = None
    await _rejected_as_code(service, tokens.make(raw_nonce=n2), "c2", n2)
    assert len(repo.sessions) == 1 and set(repo.users) == {first.user.id}
