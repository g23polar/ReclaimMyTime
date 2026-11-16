"""Auth use cases: sign in with Apple or Google, authenticate a bearer token, sign out, delete
account.

Pure orchestration over injected collaborators (repository, verifier, Apple client,
cipher, clock). Never reads the wall clock or env directly.

`AuthService` runs inside one caller-owned transaction. `AccountDeletionService` opens its
own short transactions so no connection or lock is held while Apple is called.
"""

from __future__ import annotations

import asyncio
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
from app.gcal.client import GoogleRevocationError, GoogleRevoker
from app.gcal.errors import GcalRevocationFailedError

logger = logging.getLogger(__name__)

APPLE = "apple"
GOOGLE = "google"  # T6.0 (D26): identities(provider='google', subject=Google `sub`)
MAX_EXTRA_REVOCATION_PASSES = 2
MARK_REVOKED_TIMEOUT_S = 5.0
# Strong refs to shielded mark tasks so a cancelled caller cannot let them be GC'd.
_BACKGROUND: set[asyncio.Task[None]] = set()
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

        return await self._issue_session(user, now, is_new=is_new)

    async def sign_in_with_google(self, subject: str, email: str) -> SignInResult:
        """Find or create the user for `('google', subject)` and issue a session (T6.0).

        The caller has already exchanged the code and checked the id_token (`email` is
        Google-verified); nothing here calls Google. Runs in the caller's transaction, under
        the same per-user lock as Apple sign-in for an existing identity. No account
        merging: a new Google identity always gets a new user, whose email stays null when
        another user already holds it (exactly as for Apple).
        """
        now = self._clock.now()
        identity = await self._locked_identity(subject, provider=GOOGLE)
        if identity is None:
            user = await self._create_user(email, now)
            owner_id = await self._repo.insert_identity(
                IdentityRecord(
                    provider=GOOGLE,
                    subject=subject,
                    user_id=user.id,
                    email=email,
                    email_verified=True,
                    is_private_email=False,
                    apple_refresh_token_enc=None,
                    created_at=now,
                )
            )
            is_new = owner_id is not None
            if not is_new:
                # A concurrent first sign-in for this subject won; converge on its owner.
                logger.info("concurrent first google sign-in; converging on the persisted owner")
                await self._repo.delete_user(user.id)
                identity = await self._locked_identity(subject, provider=GOOGLE)
                if identity is None:  # owner deleted meanwhile; client retries
                    raise UnauthenticatedError()
                user = await self._returning_google(identity, email)
        else:
            user = await self._returning_google(identity, email)
            is_new = False
        return await self._issue_session(user, now, is_new=is_new)

    async def _returning_google(self, identity: IdentityRecord, email: str) -> UserRecord:
        """Caller holds the owner's lock (`_locked_identity`)."""
        existing = await self._repo.get_user(identity.user_id)
        if existing is None:  # FK makes this impossible in Postgres; defensive.
            raise UnauthenticatedError()
        user = await self._apply_linkable_email(existing, email)
        await self._repo.update_identity(
            GOOGLE,
            identity.subject,
            apple_refresh_token_enc=None,
            email=email,
            email_verified=True,
            is_private_email=False,
        )
        return user

    async def _issue_session(
        self, user: UserRecord, now: datetime, *, is_new: bool
    ) -> SignInResult:
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

    async def _locked_identity(
        self, subject: str, *, provider: str = APPLE
    ) -> IdentityRecord | None:
        """The identity for `subject`, read under its owner's per-user lock.

        Serializes against `AccountDeletionService` for that user: once the lock is held
        the identity is re-read, so a deletion that committed meanwhile yields None
        (sign-in then creates a fresh account). A brand-new identity takes no lock; its
        row is created by `insert_identity`, which never writes over an existing one.
        """
        identity = await self._repo.get_identity(provider, subject)
        locked: set[uuid.UUID] = set()
        for _ in range(_LOCK_ATTEMPTS):
            if identity is None or identity.user_id in locked:
                return identity
            await self._repo.lock_user(identity.user_id)
            locked.add(identity.user_id)
            identity = await self._repo.get_identity(provider, subject)
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
        return await self._apply_linkable_email(user, linkable_email(claims))

    async def _apply_linkable_email(self, user: UserRecord, desired: str | None) -> UserRecord:
        """Set users.email to `desired` unless another user holds it (no merging)."""
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


class _RevokedGoogle:
    """Google grants revoked during one deletion: plaintext tokens and (casefolded) emails."""

    def __init__(self) -> None:
        self.tokens: set[str] = set()
        self.emails: set[str] = set()

    def add(self, token: str, email: str) -> None:
        self.tokens.add(token)
        self.emails.add(email.casefold())

    def __bool__(self) -> bool:
        return bool(self.tokens)


class AccountDeletionService:
    """DELETE /me: revoke the Google grant (if connected) and every stored Apple refresh
    token, then delete the user (identities, sessions and google_connections cascade).

    1. tx A, under the per-user lock: read and decrypt every stored credential (Google and
       Apple) before anything is revoked; commit.
    2. Revoke Google first, then each Apple token, outside any transaction.
    3. tx B, under the per-user lock: re-read. A credential not yet revoked (stored by a
       concurrent sign-in or Google reconnect) is revoked in another pass, at most
       `MAX_EXTRA_REVOCATION_PASSES` times, then 503 (`gcal_revocation_failed` or
       `apple_revocation_failed`, reason google_error / apple_error). Otherwise delete the
       user in tx B.

    Fail closed: an undecryptable credential blocks deletion (503, reason
    undecryptable_credential) before anything is revoked; a Google revoke failure is 503
    `gcal_revocation_failed` and an Apple one propagates (503 apple_unavailable). Nothing is
    deleted either way. If Google was revoked but a later step fails or the request is
    cancelled, the Google row is marked `revoked` (revoked_at = now; bounded and shielded)
    before the error propagates, so its status never claims a grant that no longer exists
    (decision T4.1 #8). That includes a row whose token changed but whose account is one we
    revoked, since Google revocation is grant-wide.

    Credentials are compared by plaintext, so re-encryption under a rotated key is not a
    change.
    """

    def __init__(
        self,
        provider: RepositoryProvider,
        *,
        apple: AppleClient,
        cipher: TokenCipher,
        google: GoogleRevoker,
        clock: Clock,
    ) -> None:
        self._provider = provider
        self._apple = apple
        self._cipher = cipher
        self._google = google
        self._clock = clock

    async def delete_account(self, user_id: uuid.UUID) -> None:
        async with self._provider() as repo:  # tx A
            await repo.lock_user(user_id)
            google = await self._google_credential(repo, user_id)
            pending = await self._credentials(repo, user_id)
        revoked_google = _RevokedGoogle()
        revoked: set[str] = set()
        try:
            for _ in range(1 + MAX_EXTRA_REVOCATION_PASSES):
                if google is not None:
                    await self._revoke_google(google[0])
                    revoked_google.add(*google)
                for refresh_token in pending:
                    await self._apple.revoke_refresh_token(refresh_token)
                    revoked.add(refresh_token)
                async with self._provider() as repo:  # tx B
                    await repo.lock_user(user_id)
                    current = await self._google_credential(repo, user_id)
                    google = (
                        current
                        if current is not None and current[0] not in revoked_google.tokens
                        else None
                    )
                    pending = [
                        t for t in await self._credentials(repo, user_id) if t not in revoked
                    ]
                    if google is None and not pending:
                        await repo.delete_user(user_id)
                        return
                logger.info("credential changed during account deletion; revoking again")
            logger.error("credential kept changing during account deletion; deletion blocked")
            if google is not None:
                raise GcalRevocationFailedError("google_error")
            raise AppleRevocationFailedError("apple_error")
        except BaseException:
            # Includes CancelledError: once Google confirmed revocation, the row must say so
            # even if the request is abandoned (T4.1 review).
            if revoked_google:
                await self._mark_google_revoked_shielded(user_id, revoked_google)
            raise

    async def _revoke_google(self, token: str) -> None:
        try:
            await self._google.revoke_token(token)
        except GoogleRevocationError:
            raise GcalRevocationFailedError("google_error") from None

    async def _mark_google_revoked_shielded(
        self, user_id: uuid.UUID, revoked: _RevokedGoogle
    ) -> None:
        """Run the mark in its own task, bounded by MARK_REVOKED_TIMEOUT_S and shielded, so
        a cancellation (the first, which got us here, or a second one) cannot abort it."""
        task = asyncio.ensure_future(
            asyncio.wait_for(self._mark_google_revoked(user_id, revoked), MARK_REVOKED_TIMEOUT_S)
        )
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        try:
            await asyncio.shield(task)
        except TimeoutError:
            logger.error("marking the google connection revoked timed out")
        # A second CancelledError propagates; the shielded task keeps running.

    async def _mark_google_revoked(self, user_id: uuid.UUID, revoked: _RevokedGoogle) -> None:
        """Best effort: the original error is what the client must see.

        Marks the row when its token is one we revoked, or when its account is one whose
        grant we revoked: Google revocation is grant-wide, so a same-account token stored
        by a concurrent reconnect died with it.
        """
        try:
            async with self._provider() as repo:
                await repo.lock_user(user_id)
                stored = await repo.get_google_credential(user_id)
                if stored is None or stored.status != "active":
                    return
                if stored.google_email.casefold() not in revoked.emails:
                    try:
                        plaintext = self._cipher.decrypt(stored.refresh_token_enc)
                    except DecryptionError:
                        return
                    if plaintext not in revoked.tokens:
                        return
                await repo.mark_google_revoked(user_id, self._clock.now())
        except Exception as exc:
            logger.error(
                "could not mark revoked google connection after failed deletion: %s",
                type(exc).__name__,
            )

    async def _google_credential(
        self, repo: AuthRepository, user_id: uuid.UUID
    ) -> tuple[str, str] | None:
        """(plaintext refresh token, google_email), or None when not connected."""
        stored = await repo.get_google_credential(user_id)
        if stored is None:
            return None
        try:
            return self._cipher.decrypt(stored.refresh_token_enc), stored.google_email
        except DecryptionError:
            logger.error(
                "google refresh token undecryptable at deletion (key_id=%s); deletion blocked",
                stored.key_id,
            )
            raise GcalRevocationFailedError(
                "undecryptable_credential", key_id=stored.key_id
            ) from None

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
