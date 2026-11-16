"""Google connection lifecycle (T4.1): connect URL, callback redirect, bearer-bound
completion (D24), status, disconnect, and on-demand access tokens for T4.2/T4.3. The
callback is shared with Google sign-in (T6.0, D26): a `login` state is handed back to the
app's sign-in callback instead; app.auth.google_signin completes it and reuses
`persist_connection` and the best-effort revoke helpers below.

Transactions are short and never held across a Google call (same discipline as
app.auth.service.AccountDeletionService). Credential writes run under the per-user lock.
Never logged: tokens, codes, state, Google error bodies.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote, urlencode

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.clock import Clock
from app.auth.crypto import DecryptionError, TokenCipher
from app.gcal import repository as repo
from app.gcal.client import (
    GoogleAccessToken,
    GoogleOAuthClient,
    GoogleRejectedError,
    GoogleRevocationError,
    GoogleRevoker,
    GoogleTokens,
    GoogleUnavailableError,
    IdTokenInvalidError,
    google_email_from_id_token,
)
from app.gcal.errors import (
    ACCESS_DENIED,
    EXCHANGE_FAILED,
    STATE_INVALID,
    GcalConnectionInactiveError,
    GcalCredentialUndecryptableError,
    GcalExchangeFailedError,
    GcalGrantRevokedError,
    GcalNotConnectedError,
    GcalRevocationFailedError,
    GcalScopeInsufficientError,
    GcalStateInvalidError,
    GcalTransientError,
)
from app.gcal.settings import CALENDAR_SCOPES, REQUESTED_SCOPES, GcalSettings
from app.gcal.state import CONNECT, LOGIN, StateCodec, StateInvalidError, s256_challenge

logger = logging.getLogger(__name__)

# Same bound as Apple's deletion: a credential replaced mid-revocation gets this many
# extra revocation passes, then the request fails closed.
MAX_EXTRA_REVOCATION_PASSES = 2


class GcalService:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        settings: GcalSettings,
        client: GoogleOAuthClient,
        cipher: TokenCipher,
        state: StateCodec,
        clock: Clock,
    ) -> None:
        self._maker = maker
        self._settings = settings
        self._client = client
        self._cipher = cipher
        self._state = state
        self._clock = clock

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self._maker() as s, s.begin():
            yield s

    # ----------------------------------------------------------------- connect

    def authorization_url(self, user_id: uuid.UUID) -> str:
        """GET /gcal/connect: a `connect` state bound to the bearer user (D24)."""
        token, issued = self._state.issue(user_id, self._clock.now())
        return self._consent_url(token, issued.code_verifier)

    def login_authorization_url(self, nonce_hash: str) -> str:
        """POST /auth/google/start (T6.0, D26): the same consent (same scopes, offline,
        prompt=consent, PKCE) under a `login` state bound to the app nonce's hash."""
        token, issued = self._state.issue_login(nonce_hash, self._clock.now())
        return self._consent_url(token, issued.code_verifier)

    def _consent_url(self, state: str, code_verifier: str) -> str:
        s = self._settings
        params = {
            "client_id": s.client_id,
            "redirect_uri": s.redirect_uri,
            "response_type": "code",
            "scope": " ".join(REQUESTED_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": s256_challenge(code_verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{s.auth_url}?{urlencode(params, quote_via=quote)}"

    def callback_redirect(self, *, state: str | None, code: str | None, error: str | None) -> str:
        """GET /gcal/callback (D24, D26): hand Google's code back to the app, which completes
        with its bearer via POST /gcal/complete (`connect`) or with its nonce via POST
        /auth/google/complete (`login`). Nothing is exchanged or persisted here, and nothing
        touches the DB: the state is only decrypted and its expiry checked so an obviously
        dead attempt fails fast in the app.

        The app URL follows the state's purpose: `login` → the sign-in callback; anything
        else (connect, a pre-T6.0 state, or a state too broken to read) → the connect
        callback, exactly as before T6.0.
        """
        purpose = self._state.peek_purpose(state)
        base = (
            self._settings.login_app_callback_url
            if purpose == LOGIN
            else self._settings.app_callback_url
        )
        if error is not None:
            if error == "access_denied":
                logger.info("google callback: consent denied")
                return f"{base}?result=error&code={ACCESS_DENIED}"
            logger.info("google callback carried a non-denial error parameter")
            return f"{base}?result=error&code={EXCHANGE_FAILED}"
        try:
            self._state.open(state, self._clock.now())
        except StateInvalidError as exc:
            logger.info("gcal callback state rejected: %s", exc)
            return f"{base}?result=error&code={STATE_INVALID}"
        if not code:
            logger.info("gcal callback without code or error")
            return f"{base}?result=error&code={EXCHANGE_FAILED}"
        assert state is not None
        return (
            f"{base}?result=authorized&auth_code={quote(code, safe='')}"
            f"&state={quote(state, safe='')}"
        )

    async def complete(
        self, user_id: uuid.UUID, *, auth_code: str, state: str
    ) -> repo.ConnectionRecord:
        """POST /gcal/complete: the bearer holder finishes the connect flow.

        The state must decrypt, be unexpired, and have been issued to this same user, so a
        forwarded connect URL cannot land a victim's grant on someone else's account.
        Raises GcalStateInvalidError (400) | GcalScopeInsufficientError (422) |
        GcalExchangeFailedError (502). Grants we will not keep are revoked best-effort,
        under `_discard`'s same-account rule.
        """
        try:
            # A sign-in (`login`) state is invalid here (D26).
            oauth = self._state.open(state, self._clock.now(), purpose=CONNECT)
        except StateInvalidError as exc:
            logger.info("gcal complete: state rejected: %s", exc)
            raise GcalStateInvalidError() from None
        if oauth.user_id != user_id:
            logger.warning("gcal complete: state issued to a different user")
            raise GcalStateInvalidError()
        try:
            tokens = await self._client.exchange_code(auth_code, oauth.code_verifier)
        except (GoogleRejectedError, GoogleUnavailableError):
            raise GcalExchangeFailedError() from None

        # Learn the account first (if the id_token verifies) so a discard can tell
        # whether the grant belongs to the account we already store (see `_discard`).
        email: str | None
        id_token_error: IdTokenInvalidError | None = None
        try:
            email = google_email_from_id_token(
                tokens.id_token,
                client_id=self._settings.client_id,
                now=self._clock.now(),
                leeway=self._settings.id_token_leeway,
            )
        except IdTokenInvalidError as exc:
            email, id_token_error = None, exc

        if not set(CALENDAR_SCOPES) <= set(tokens.scopes):
            logger.info("google grant lacks a calendar scope")
            await self._discard(user_id, tokens, email)
            raise GcalScopeInsufficientError()
        if tokens.refresh_token is None:
            logger.warning("google code exchange returned no refresh token")
            await self._discard(user_id, tokens, email)
            raise GcalExchangeFailedError()
        if email is None:
            logger.warning("google id_token rejected: %s", id_token_error)
            await self._discard(user_id, tokens, None)
            raise GcalExchangeFailedError()
        try:
            stored, replaced = await self._persist(user_id, email, tokens)
        except Exception:
            await self._discard(user_id, tokens, email)
            raise
        if replaced is not None:
            await self._revoke_replaced_account(user_id, *replaced)
        return stored

    async def _persist(
        self, user_id: uuid.UUID, email: str, tokens: GoogleTokens
    ) -> tuple[repo.ConnectionRecord, tuple[str, str] | None]:
        """Upsert in a transaction of its own (see `persist_connection`)."""
        async with self._tx() as s:
            return await persist_connection(
                s, user_id, email, tokens, cipher=self._cipher, now=self._clock.now()
            )

    async def revoke_replaced_account(self, user_id: uuid.UUID, token: str, email: str) -> None:
        """Public seam for Google sign-in (T6.0); see `_revoke_replaced_account`."""
        await self._revoke_replaced_account(user_id, token, email)

    async def discard(self, user_id: uuid.UUID, tokens: GoogleTokens, email: str | None) -> None:
        """Public seam for Google sign-in (T6.0); see `_discard`."""
        await self._discard(user_id, tokens, email)

    async def _revoke_replaced_account(self, user_id: uuid.UUID, token: str, email: str) -> None:
        """After switching Google accounts: best-effort revoke of the old account's grant.
        Only ever called for a *different* account (revocation is grant-wide, so doing this
        for the same account would kill the connection just stored), and skipped when any
        other user still has that old account connected (T6.0 review)."""
        if not await self._safe_to_revoke(email):
            return
        try:
            await self._client.revoke_token(token)
        except Exception as exc:  # best effort by design
            logger.warning(
                "best-effort revoke of the replaced google account failed: %s",
                type(exc).__name__,
            )
            return
        await self._reconcile_after_revoke(user_id, token=token, email=email)

    async def _discard(self, user_id: uuid.UUID, tokens: GoogleTokens, email: str | None) -> None:
        """Best-effort revoke of a grant we will not store (failures ignored).

        Google revocation is grant-wide per account, so revoking a token from an account
        that ANY user has connected would kill that connection too (T6.0 review: not only
        the caller's own row). Revoke only when the discarded grant's email is known
        (verified id_token) and no stored connection, of any user, is for that account.
        Unknown email → skip: we cannot tell whose account it is. The snapshot can be stale
        by the time Google acts, so every confirmed revoke is followed by
        `_reconcile_after_revoke` (which covers the caller's own row).
        """
        token = tokens.refresh_token or tokens.access_token
        if token is None:
            return
        if email is None:
            logger.info("discarded grant's account unknown; not revoked")
            return
        if not await self._safe_to_revoke(email):
            return
        try:
            await self._client.revoke_token(token)
        except Exception as exc:  # best effort by design
            logger.info("best-effort revoke of discarded grant failed: %s", type(exc).__name__)
            return
        await self._reconcile_after_revoke(user_id, token=token, email=email)

    async def _safe_to_revoke(self, email: str) -> bool:
        """No stored connection of any user is for this Google account. Read in a short
        transaction of its own, without the per-user lock and before Google is called."""
        try:
            async with self._tx() as s:
                held = await repo.account_has_connection(s, email)
        except Exception as exc:
            logger.info("cleanup revoke skipped, connections unreadable: %s", type(exc).__name__)
            return False
        if held:
            logger.info("google account has a stored connection; cleanup grant not revoked")
        return not held

    async def _reconcile_after_revoke(
        self, user_id: uuid.UUID, *, token: str, email: str | None
    ) -> None:
        """Google confirmed revoking a grant. A concurrent completion may have stored that
        same account meanwhile; revocation is grant-wide, so its token is dead too. Under the
        per-user lock, re-read the row and, if it is active and belongs to the revoked
        account (by email when known from a verified id_token, else by token), mark it
        revoked (compare-and-set on the ciphertext). Never fails the request.

        Only after a *confirmed* revoke: a failed/timed-out revoke leaves the row alone, and
        if Google did revoke after all, the next access-token mint sees invalid_grant and
        marks it then.
        """
        try:
            async with self._tx() as s:
                await repo.lock_user(s, user_id)
                row = await repo.get_connection(s, user_id)
                if row is None or row.status != "active":
                    return
                if email is not None:
                    match = repo.same_account(row.google_email, email)
                else:
                    try:
                        match = self._cipher.decrypt(row.refresh_token_enc) == token
                    except DecryptionError:
                        match = False
                if not match:
                    return
                marked = await repo.mark_revoked(
                    s, user_id, refresh_token_enc=row.refresh_token_enc, at=self._clock.now()
                )
            if marked:
                logger.warning("stored google connection died with a cleanup revoke; marked")
        except Exception as exc:
            logger.error("reconcile after google revoke failed: %s", type(exc).__name__)

    # ----------------------------------------------------------------- status

    async def connection(self, user_id: uuid.UUID) -> repo.ConnectionRecord | None:
        async with self._tx() as s:
            return await repo.get_connection(s, user_id)

    # ----------------------------------------------------------------- disconnect

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """DELETE /gcal, fail closed: revoke at Google, then delete the row. A revoke
        failure (5xx, timeout, unexpected answer) or an undecryptable credential is 503
        `gcal_revocation_failed` with nothing changed. A credential replaced by a
        concurrent reconnect while Google was being called is revoked in another pass."""
        async with self._tx() as s:
            row = await repo.get_connection(s, user_id)
        if row is None:
            raise GcalNotConnectedError()
        token = self._decrypt_for_revocation(row)
        revoked: set[str] = set()
        for _ in range(1 + MAX_EXTRA_REVOCATION_PASSES):
            await revoke_or_fail(self._client, token)
            revoked.add(token)
            async with self._tx() as s:
                await repo.lock_user(s, user_id)
                row = await repo.get_connection(s, user_id)
                if row is None:
                    return  # removed concurrently (another disconnect / account deletion)
                current = self._decrypt_for_revocation(row)
                if current in revoked:
                    # If this DB step fails after a confirmed revoke, the row self-heals: the
                    # next access-token mint gets invalid_grant and marks it revoked.
                    await repo.delete_connection(s, user_id)
                    return
                token = current
            logger.info("google credential changed during disconnect; revoking again")
        logger.error("google credential kept changing during disconnect; disconnect blocked")
        raise GcalRevocationFailedError("google_error")

    def _decrypt_for_revocation(self, row: repo.ConnectionRecord) -> str:
        try:
            return self._cipher.decrypt(row.refresh_token_enc)
        except DecryptionError:
            logger.error("google refresh token undecryptable (key_id=%s)", row.key_id)
            raise GcalRevocationFailedError("undecryptable_credential", key_id=row.key_id) from None

    # ----------------------------------------------------------------- access tokens

    async def access_token(self, user_id: uuid.UUID) -> GoogleAccessToken:
        """Mint an access token from the stored refresh token; in memory only.

        GcalConnectionInactiveError: no row, or already revoked.
        GcalGrantRevokedError (a GcalConnectionInactiveError): Google said invalid_grant;
          the row is now `revoked` with revoked_at = now.
        GcalCredentialUndecryptableError: no configured key decrypts the row.
        GcalTransientError: anything else; status untouched.
        """
        async with self._tx() as s:
            row = await repo.get_connection(s, user_id)
        if row is None or row.status != "active":
            raise GcalConnectionInactiveError()
        try:
            refresh_token = self._cipher.decrypt(row.refresh_token_enc)
        except DecryptionError:
            logger.error("google refresh token undecryptable (key_id=%s)", row.key_id)
            raise GcalCredentialUndecryptableError(row.key_id) from None
        try:
            return await self._client.refresh(refresh_token, self._clock.now())
        except GoogleRejectedError as exc:
            if exc.error != "invalid_grant":
                raise GcalTransientError() from None
        except GoogleUnavailableError:
            raise GcalTransientError() from None
        async with self._tx() as s:
            await repo.lock_user(s, user_id)
            marked = await repo.mark_revoked(
                s, user_id, refresh_token_enc=row.refresh_token_enc, at=self._clock.now()
            )
        logger.info("google grant revoked (invalid_grant); connection marked revoked=%s", marked)
        raise GcalGrantRevokedError()


async def persist_connection(
    s: AsyncSession,
    user_id: uuid.UUID,
    email: str,
    tokens: GoogleTokens,
    *,
    cipher: TokenCipher,
    now: datetime,
) -> tuple[repo.ConnectionRecord, tuple[str, str] | None]:
    """The T4.1 persist path inside the caller's transaction `s`: upsert under the per-user
    lock (re-entrant, so a caller that already holds it is fine). Returns the stored row and,
    when this replaced a *different* Google account, that account's (plaintext refresh
    token, google_email) (decrypted here, before it is overwritten) so the caller can revoke
    it after commit. GcalStateInvalidError if the user no longer exists (a concurrent
    DELETE /me)."""
    assert tokens.refresh_token is not None
    enc = cipher.encrypt(tokens.refresh_token)
    replaced: tuple[str, str] | None = None
    await repo.lock_user(s, user_id)
    if not await repo.user_exists(s, user_id):
        # The bearer was valid moments ago; only a concurrent DELETE /me gets here.
        raise GcalStateInvalidError()
    previous = await repo.get_connection(s, user_id)
    if previous is not None and not repo.same_account(previous.google_email, email):
        try:
            replaced = (cipher.decrypt(previous.refresh_token_enc), previous.google_email)
        except DecryptionError:
            logger.warning(
                "replaced google credential undecryptable (key_id=%s); not revoked",
                previous.key_id,
            )
    await repo.upsert_connection(
        s,
        user_id,
        previous=previous,
        google_email=email,
        refresh_token_enc=enc,
        key_id=cipher.primary_key_id,
        scopes=sorted(tokens.scopes),
        now=now,
    )
    stored = await repo.get_connection(s, user_id)
    assert stored is not None
    return stored, replaced


async def revoke_or_fail(revoker: GoogleRevoker, token: str) -> None:
    """Revoke; any failure → 503 gcal_revocation_failed (google_error)."""
    try:
        await revoker.revoke_token(token)
    except GoogleRevocationError:
        raise GcalRevocationFailedError("google_error") from None
