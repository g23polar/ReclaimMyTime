"""Sign in with Google (T6.0, contracts/NOTES.md D26): one combined consent creates or finds
the user, issues a session, and stores the calendar connection.

Reuses the T4.1 web flow: the consent URL, PKCE, the Fernet `state` (purpose `login`, bound
to the hash of an app-held nonce instead of a bearer user), the shared GET /gcal/callback,
the code exchange, the id_token checks, and the connection persist path.

Ordering (same discipline as T4.1 and account deletion): every Google call happens outside
any DB transaction and without the per-user lock. The code is exchanged first; then ONE
transaction takes the lock (for an existing identity), finds or creates the user, issues
the session and, when the calendar scopes and a refresh token came back, stores the
connection; after commit, grants we will not keep are revoked best-effort.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.repository import AuthRepository, SqlAlchemyAuthRepository
from app.auth.service import AuthService, SignInResult
from app.gcal import repository as gcal_repo
from app.gcal.client import (
    GoogleRejectedError,
    GoogleUnavailableError,
    IdTokenInvalidError,
    google_identity_from_id_token,
)
from app.gcal.errors import GcalExchangeFailedError, GcalStateInvalidError
from app.gcal.runtime import GcalRuntime
from app.gcal.service import GcalService, persist_connection
from app.gcal.settings import CALENDAR_SCOPES
from app.gcal.state import LOGIN, StateInvalidError, nonce_hash

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoogleSignInResult:
    sign_in: SignInResult
    # The user has an active calendar connection after this sign-in: the one just stored,
    # or (calendar scopes unticked) one that already existed.
    gcal_connected: bool


class GoogleSignInService:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        auth_service: Callable[[AuthRepository], AuthService],
        gcal: GcalRuntime,
    ) -> None:
        self._maker = maker
        self._auth_service = auth_service
        self._gcal = gcal

    def _gcal_service(self) -> GcalService:
        return self._gcal.service(self._maker)

    async def complete(self, *, auth_code: str, state: str, nonce: str) -> GoogleSignInResult:
        """POST /auth/google/complete.

        400 GcalStateInvalidError: state undecryptable, malformed, expired, not a `login`
        state, or `nonce` does not hash to the state's nonce_hash (not distinguished).
        502 GcalExchangeFailedError: Google rejected or did not answer the exchange, or the
        id_token is unusable. Missing calendar scopes or refresh token are NOT errors: the
        user is signed in without a connection and the partial grant is revoked best-effort.
        """
        gcal = self._gcal
        try:
            oauth = gcal.state.open(state, gcal.clock.now(), purpose=LOGIN)
        except StateInvalidError as exc:
            logger.info("google sign-in: state rejected: %s", exc)
            raise GcalStateInvalidError() from None
        assert oauth.nonce_hash is not None
        # Login-CSRF binding (D26): only the app that started this flow holds the nonce.
        if not hmac.compare_digest(nonce_hash(nonce), oauth.nonce_hash):
            logger.warning("google sign-in: nonce does not match the state")
            raise GcalStateInvalidError()

        try:
            tokens = await gcal.client.exchange_code(auth_code, oauth.code_verifier)
        except (GoogleRejectedError, GoogleUnavailableError):
            raise GcalExchangeFailedError() from None
        try:
            identity = google_identity_from_id_token(
                tokens.id_token,
                client_id=gcal.settings.client_id,
                now=gcal.clock.now(),
                leeway=gcal.settings.id_token_leeway,
            )
        except IdTokenInvalidError as exc:
            # Without a verified account we cannot tell whether revoking would kill a stored
            # connection of the same account (grant-wide), so the grant is left alone.
            logger.warning("google sign-in: id_token rejected: %s", exc)
            raise GcalExchangeFailedError() from None

        keep_calendar = set(CALENDAR_SCOPES) <= set(tokens.scopes)
        if not keep_calendar:
            logger.info("google sign-in without calendar scopes; no connection stored")
        elif tokens.refresh_token is None:
            logger.warning("google sign-in returned no refresh token; no connection stored")
            keep_calendar = False

        user_id: uuid.UUID | None = None
        replaced: tuple[str, str] | None = None
        try:
            async with self._maker() as s, s.begin():
                result = await self._auth_service(SqlAlchemyAuthRepository(s)).sign_in_with_google(
                    identity.subject, identity.email
                )
                user_id = result.user.id
                if keep_calendar:
                    _, replaced = await persist_connection(
                        s,
                        user_id,
                        identity.email,
                        tokens,
                        cipher=gcal.cipher,
                        now=gcal.clock.now(),
                    )
                row = await gcal_repo.get_connection(s, user_id)
                connected = row is not None and row.status == "active"
        except Exception:
            if user_id is not None:  # rolled back: nothing of this grant was kept
                await self._gcal_service().discard(user_id, tokens, identity.email)
            raise

        # After commit, outside any transaction: best-effort revokes (never fail sign-in).
        if replaced is not None:
            await self._gcal_service().revoke_replaced_account(user_id, *replaced)
        if not keep_calendar:
            # Same-account rule (T4.1 `_discard`): a stored connection of this same Google
            # account shares the grant, so it is not revoked then.
            await self._gcal_service().discard(user_id, tokens, identity.email)
        return GoogleSignInResult(sign_in=result, gcal_connected=connected)
