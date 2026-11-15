"""Auth use cases: sign in with Apple, authenticate a bearer token, sign out, delete account.

Pure orchestration over injected collaborators (repository, verifier, Apple client,
cipher, clock). Never reads the wall clock or env directly.

`AuthService` runs inside one caller-owned transaction. `AccountDeletionService` opens its
own short transactions so no connection or lock is held while Apple is called.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.auth.apple import (
    AppleClaims,
    AppleClient,
    AppleIdentityVerifier,
    AppleTokenResponse,
    AppleUnreachableError,
    NonceGuard,
    hash_nonce,
)
from app.auth.clock import Clock
from app.auth.crypto import DecryptionError, TokenCipher
from app.auth.errors import AppleRevocationFailedError, AppleTokenInvalidError, UnauthenticatedError
from app.auth.repository import (
    AuthRepository,
    EmailConflictError,
    IdentityRecord,
    RepositoryProvider,
    SessionRecord,
    UserRecord,
)
from app.auth.sessions import SessionPolicy, hash_token, mint_token

logger = logging.getLogger(__name__)

APPLE = "apple"
MAX_EXTRA_REVOCATION_PASSES = 2
_LOCK_ATTEMPTS = 3


@dataclass(frozen=True)
class SignInResult:
    token: str  # plaintext bearer — returned to the client once, never stored
    session: SessionRecord
    user: UserRecord
    is_new_user: bool


@dataclass(frozen=True)
class Authenticated:
    session: SessionRecord
    user: UserRecord


def linkable_email(claims: AppleClaims) -> str | None:
    """ARCH §6: users.email only when Apple says verified AND it is not a private relay."""
    if claims.email and claims.email_verified and not claims.is_private_email:
        return claims.email
    return None


class AuthService:
    def __init__(
        self,
        repo: AuthRepository,
        *,
        verifier: AppleIdentityVerifier,
        nonce_guard: NonceGuard,
        apple: AppleClient,
        cipher: TokenCipher,
        policy: SessionPolicy,
        clock: Clock,
    ) -> None:
        self._repo = repo
        self._verifier = verifier
        self._nonce_guard = nonce_guard
        self._apple = apple
        self._cipher = cipher
        self._policy = policy
        self._clock = clock

    # ----------------------------------------------------------------- sign-in

    async def sign_in_with_apple(
        self, identity_token: str, authorization_code: str, raw_nonce: str
    ) -> SignInResult:
        claims = await self._verifier.verify(identity_token, raw_nonce)
        apple_tokens = await self._exchange_code_once(authorization_code, raw_nonce, claims)
        # Bind the one-time code to the verified identity: the id_token Apple returns with
        # the refresh token must be for the same subject as the submitted identity token.
        code_subject = await self._verifier.verify_code_subject(apple_tokens.id_token, raw_nonce)
        if code_subject != claims.subject:
            logger.info("apple authorization code subject does not match identity token")
            raise AppleTokenInvalidError("authorization_code")

        refresh_enc = (
            self._cipher.encrypt(apple_tokens.refresh_token) if apple_tokens.refresh_token else None
        )
        now = self._clock.now()
        identity = await self._locked_identity(claims.subject)
        if refresh_enc is None and (identity is None or identity.apple_refresh_token_enc is None):
            # No account (or session) DELETE /me could not revoke. Nothing has been
            # written yet; the nonce stays consumed because Apple answered.
            logger.info("apple code exchange returned no refresh token for a new identity")
            raise AppleTokenInvalidError("authorization_code")
        if identity is None:
            user = await self._create_user(linkable_email(claims), now)
            owner_id = await self._repo.insert_identity(
                IdentityRecord(
                    provider=APPLE,
                    subject=claims.subject,
                    user_id=user.id,
                    email=claims.email,
                    email_verified=claims.email_verified,
                    is_private_email=claims.is_private_email,
                    apple_refresh_token_enc=refresh_enc,
                    created_at=now,
                )
            )
            is_new = owner_id is not None
            if not is_new:
                # A concurrent first sign-in for this subject persisted its identity first;
                # our insert wrote nothing. Drop our provisional user (same transaction),
                # then converge on the owner under its lock like any returning sign-in.
                logger.info("concurrent first sign-in; converging on the persisted owner")
                await self._repo.delete_user(user.id)
                identity = await self._locked_identity(claims.subject)
                if identity is None:  # owner deleted meanwhile; client retries
                    raise UnauthenticatedError()
                user = await self._returning_sign_in(identity, claims, refresh_enc)
        else:
            user = await self._returning_sign_in(identity, claims, refresh_enc)
            is_new = False

        token = mint_token()
        session = SessionRecord(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=hash_token(token),
            created_at=now,
            expires_at=self._policy.expiry(created_at=now, now=now),
            last_seen_at=now,
        )
        await self._repo.create_session(session)
        return SignInResult(token=token, session=session, user=user, is_new_user=is_new)

    async def _returning_sign_in(
        self, identity: IdentityRecord, claims: AppleClaims, refresh_enc: bytes | None
    ) -> UserRecord:
        """Refresh an existing identity and its user. Caller holds the owner's lock
        (`_locked_identity`), so the credential write is serialized against deletion."""
        existing = await self._repo.get_user(identity.user_id)
        if existing is None:  # FK makes this impossible in Postgres; defensive.
            raise UnauthenticatedError()
        user = await self._refresh_user_email(existing, claims)
        # No email claim: keep the stored email and its metadata together.
        await self._repo.update_identity(
            identity.provider,
            identity.subject,
            apple_refresh_token_enc=refresh_enc,  # None keeps the stored one
            email=claims.email,
            email_verified=claims.email_verified,
            is_private_email=claims.is_private_email,
        )
        return user

    async def _locked_identity(self, subject: str) -> IdentityRecord | None:
        """The identity for `subject`, read under its owner's per-user lock.

        Serializes against `AccountDeletionService` for that user: once the lock is held
        the identity is re-read, so a deletion that committed meanwhile yields None
        (sign-in then creates a fresh account). A brand-new identity takes no lock; its
        row is created by `insert_identity`, which never writes over an existing one.
        """
        identity = await self._repo.get_identity(APPLE, subject)
        locked: set[uuid.UUID] = set()
        for _ in range(_LOCK_ATTEMPTS):
            if identity is None or identity.user_id in locked:
                return identity
            await self._repo.lock_user(identity.user_id)
            locked.add(identity.user_id)
            identity = await self._repo.get_identity(APPLE, subject)
        if identity is not None and identity.user_id not in locked:
            raise UnauthenticatedError()  # owner kept changing under us; client retries
        return identity

    async def _exchange_code_once(
        self, authorization_code: str, raw_nonce: str, claims: AppleClaims
    ) -> AppleTokenResponse:
        """Exchange the code under a nonce reservation.

        Released only if the request cannot have reached Apple (so a retry with the same
        credentials works); committed on any Apple response or unknown outcome.
        """
        nonce_hash = hash_nonce(raw_nonce)
        if not await self._nonce_guard.reserve(nonce_hash, claims.expires_at):
            # TODO(contract): NOTES.md has no distinct reason for a replayed nonce;
            # reported as nonce_mismatch (the nonce is not a fresh one for this request).
            raise AppleTokenInvalidError("nonce_mismatch")
        try:
            apple_tokens = await self._apple.exchange_code(authorization_code)
        except AppleUnreachableError:
            await self._nonce_guard.release(nonce_hash)
            raise
        except BaseException:
            await self._nonce_guard.commit(nonce_hash)
            raise
        await self._nonce_guard.commit(nonce_hash)
        return apple_tokens

    async def _create_user(self, email: str | None, now: datetime) -> UserRecord:
        # No cross-account merge in v1: a second Apple subject with an already-linked email
        # gets its own account with email NULL (users.email is UNIQUE). Linking rules for
        # a second provider land with magic links.
        if email is not None and await self._repo.get_user_by_email(email) is not None:
            logger.info("verified email already linked to another user; leaving email null")
            email = None
        user = UserRecord(id=uuid.uuid4(), email=email, created_at=now)
        try:
            await self._repo.create_user(user)
        except EmailConflictError:
            # Lost a race for the email; the repository rolled back to its savepoint.
            logger.info("verified email linked concurrently; leaving email null")
            user = UserRecord(id=user.id, email=None, created_at=now)
            await self._repo.create_user(user)
        return user

    async def _refresh_user_email(self, user: UserRecord, claims: AppleClaims) -> UserRecord:
        if claims.email is None:
            return user  # Apple sent no email claim this time; keep what we have.
        desired = linkable_email(claims)
        if desired == user.email:
            return user
        if desired is not None:
            other = await self._repo.get_user_by_email(desired)
            if other is not None and other.id != user.id:
                return user
        try:
            await self._repo.set_user_email(user.id, desired)
        except EmailConflictError:
            # Lost a race for the email: the returning user keeps its existing email.
            logger.info("verified email linked concurrently; keeping existing email")
            return user
        return UserRecord(id=user.id, email=desired, created_at=user.created_at)

    # ----------------------------------------------------------------- sessions

    async def authenticate(self, token: str | None) -> Authenticated:
        if not token:
            raise UnauthenticatedError()
        session = await self._repo.get_session_by_token_hash(hash_token(token))
        if session is None:
            raise UnauthenticatedError()
        now = self._clock.now()
        if self._policy.is_expired(session.expires_at, now):
            raise UnauthenticatedError()
        if self._policy.needs_touch(session.last_seen_at, now):
            expires_at = self._policy.expiry(session.created_at, now)
            # Re-checked against the persisted row: concurrent overdue readers write once.
            await self._repo.touch_session(
                session.id,
                last_seen_at=now,
                expires_at=expires_at,
                threshold=self._policy.touch_threshold(now),
            )
            session = SessionRecord(
                id=session.id,
                user_id=session.user_id,
                token_hash=session.token_hash,
                created_at=session.created_at,
                expires_at=expires_at,
                last_seen_at=now,
            )
        user = await self._repo.get_user(session.user_id)
        if user is None:
            raise UnauthenticatedError()
        return Authenticated(session=session, user=user)

    async def revoke_session(self, session_id: uuid.UUID) -> None:
        await self._repo.delete_session(session_id)

    async def revoke_all_sessions(self, user_id: uuid.UUID) -> None:
        await self._repo.delete_sessions_for_user(user_id)


# --------------------------------------------------------------------------- account deletion


class AccountDeletionService:
    """DELETE /me: revoke every stored Apple refresh token, then delete the user
    (identities and sessions cascade).

    1. tx A, under the per-user lock: read and decrypt every stored credential; commit.
    2. Revoke each with Apple, outside any transaction.
    3. tx B, under the per-user lock: re-read. A credential not yet revoked (stored by a
       concurrent sign-in) is revoked in another pass, at most
       `MAX_EXTRA_REVOCATION_PASSES` times, then 503 apple_revocation_failed
       (reason apple_error). Otherwise delete the user in tx B.

    Credentials are compared by plaintext, so re-encryption under a rotated key is not a
    change. An undecryptable credential blocks deletion (reason undecryptable_credential)
    and a revoke failure propagates (503 apple_unavailable); nothing is deleted either way.
    TODO(T4.1): also revoke the Google connection here.
    """

    def __init__(
        self, provider: RepositoryProvider, *, apple: AppleClient, cipher: TokenCipher
    ) -> None:
        self._provider = provider
        self._apple = apple
        self._cipher = cipher

    async def delete_account(self, user_id: uuid.UUID) -> None:
        async with self._provider() as repo:  # tx A
            await repo.lock_user(user_id)
            pending = await self._credentials(repo, user_id)
        revoked: set[str] = set()
        for _ in range(1 + MAX_EXTRA_REVOCATION_PASSES):
            for refresh_token in pending:
                await self._apple.revoke_refresh_token(refresh_token)
                revoked.add(refresh_token)
            async with self._provider() as repo:  # tx B
                await repo.lock_user(user_id)
                pending = [t for t in await self._credentials(repo, user_id) if t not in revoked]
                if not pending:
                    await repo.delete_user(user_id)
                    return
            logger.info("apple credential changed during account deletion; revoking again")
        logger.error("apple credential kept changing during account deletion; deletion blocked")
        raise AppleRevocationFailedError("apple_error")

    async def _credentials(self, repo: AuthRepository, user_id: uuid.UUID) -> list[str]:
        """Plaintext Apple refresh tokens, deduplicated, in identity order."""
        tokens: list[str] = []
        for identity in await repo.list_identities(user_id):
            if identity.provider != APPLE or identity.apple_refresh_token_enc is None:
                continue
            try:
                token = self._cipher.decrypt(identity.apple_refresh_token_enc)
            except DecryptionError as exc:
                key_id = self._cipher.primary_key_id
                logger.error(
                    "apple refresh token undecryptable at deletion (%s, key_id=%s); "
                    "deletion blocked",
                    type(exc).__name__,
                    key_id,
                )
                raise AppleRevocationFailedError(
                    "undecryptable_credential", key_id=key_id
                ) from None
            if token not in tokens:
                tokens.append(token)
        return tokens
