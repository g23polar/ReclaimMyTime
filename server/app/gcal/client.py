"""Google OAuth REST client: code exchange (PKCE), refresh → access token, revocation, and
the id_token claim checks that yield `google_email`.

Never logged: tokens, codes, id_tokens, or Google's error bodies. Log lines carry only the
HTTP status, the exception type, or a whitelisted OAuth `error` code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
import jwt

from app.gcal.settings import GOOGLE_ISSUERS, GcalSettings

logger = logging.getLogger(__name__)

_OAUTH_ERROR_RE = re.compile(r"^[a-z_]{1,64}$")
# Google's revoke endpoint answers 400 for a token that is already invalid/revoked.
_ALREADY_REVOKED = frozenset({"invalid_token", "invalid_grant"})


class GoogleUnavailableError(Exception):
    """Network failure, timeout, 5xx, or an unparseable/unexpected answer."""


class GoogleRejectedError(Exception):
    """A 4xx OAuth error answer. `error` is the whitelisted OAuth error code or None."""

    def __init__(self, status: int, error: str | None) -> None:
        super().__init__(f"google rejected request: {status} {error or 'unknown'}")
        self.status = status
        self.error = error


class GoogleRevocationError(Exception):
    """Revocation did not succeed (anything but 200 or an "already revoked" 400)."""


class IdTokenInvalidError(Exception):
    """The id_token from the token endpoint is missing or fails a claim check."""


@dataclass(frozen=True)
class GoogleTokens:
    access_token: str | None = field(repr=False)
    refresh_token: str | None = field(repr=False)
    id_token: str | None = field(repr=False)
    scopes: tuple[str, ...]  # granted, as reported by the token endpoint


@dataclass(frozen=True)
class GoogleAccessToken:
    """Minted on demand, held in memory only (ARCH §3 Tokens). Never persisted or logged."""

    token: str = field(repr=False)
    expires_at: datetime


class GoogleRevoker(Protocol):
    async def revoke_token(self, token: str) -> None:
        """Revoke the grant behind `token` (refresh or access). Returns on 200 and on an
        "already revoked" 400; GoogleRevocationError otherwise."""
        ...


class GoogleOAuthClient(GoogleRevoker, Protocol):
    async def exchange_code(self, code: str, code_verifier: str) -> GoogleTokens:
        """GoogleRejectedError (4xx) | GoogleUnavailableError."""
        ...

    async def refresh(self, refresh_token: str, now: datetime) -> GoogleAccessToken:
        """GoogleRejectedError (4xx, e.g. invalid_grant) | GoogleUnavailableError."""
        ...


def _oauth_error(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    value = body.get("error") if isinstance(body, dict) else None
    return value if isinstance(value, str) and _OAUTH_ERROR_RE.match(value) else None


async def _post_form(
    url: str, data: dict[str, str], timeout_s: float, transport: httpx.AsyncBaseTransport | None
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            return await client.post(url, data=data)
    except httpx.HTTPError as exc:
        logger.warning("google request failed: %s", type(exc).__name__)
        raise GoogleUnavailableError() from None


class HttpGoogleRevoker:
    """https://developers.google.com/identity/protocols/oauth2/native-app#tokenrevoke"""

    def __init__(
        self,
        revoke_url: str = GcalSettings.revoke_url,
        timeout_s: float = GcalSettings.http_timeout_s,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = revoke_url
        self._timeout_s = timeout_s
        self._transport = transport

    @classmethod
    def from_settings(
        cls, settings: GcalSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> HttpGoogleRevoker:
        return cls(settings.revoke_url, settings.http_timeout_s, transport)

    async def revoke_token(self, token: str) -> None:
        try:
            response = await _post_form(
                self._url, {"token": token}, self._timeout_s, self._transport
            )
        except GoogleUnavailableError:
            raise GoogleRevocationError() from None
        if response.status_code == 200:
            return
        if response.status_code == 400 and _oauth_error(response) in _ALREADY_REVOKED:
            logger.info("google grant already revoked")
            return
        logger.warning("google revoke endpoint returned %s", response.status_code)
        raise GoogleRevocationError()


class HttpGoogleOAuthClient:
    def __init__(
        self, settings: GcalSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._revoker = HttpGoogleRevoker.from_settings(settings, transport)

    async def _token_endpoint(self, data: dict[str, str]) -> dict[str, Any]:
        s = self._settings
        form = {"client_id": s.client_id, "client_secret": s.client_secret, **data}
        response = await _post_form(s.token_url, form, s.http_timeout_s, self._transport)
        if 400 <= response.status_code < 500:
            error = _oauth_error(response)
            logger.info(
                "google token endpoint rejected %s: %s %s",
                data["grant_type"],
                response.status_code,
                error or "unknown",
            )
            raise GoogleRejectedError(response.status_code, error)
        if response.status_code != 200:
            logger.warning("google token endpoint returned %s", response.status_code)
            raise GoogleUnavailableError()
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            logger.warning("google token endpoint returned an unparseable body")
            raise GoogleUnavailableError()
        return body

    async def exchange_code(self, code: str, code_verifier: str) -> GoogleTokens:
        body = await self._token_endpoint(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": self._settings.redirect_uri,
            }
        )
        scope = body.get("scope")
        return GoogleTokens(
            access_token=_nonempty_str(body.get("access_token")),
            refresh_token=_nonempty_str(body.get("refresh_token")),
            id_token=_nonempty_str(body.get("id_token")),
            scopes=tuple(dict.fromkeys(scope.split())) if isinstance(scope, str) else (),
        )

    async def refresh(self, refresh_token: str, now: datetime) -> GoogleAccessToken:
        body = await self._token_endpoint(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        token = _nonempty_str(body.get("access_token"))
        expires_in = body.get("expires_in")
        if token is None or not isinstance(expires_in, int) or isinstance(expires_in, bool):
            logger.warning("google refresh answer lacks access_token/expires_in")
            raise GoogleUnavailableError()
        return GoogleAccessToken(token=token, expires_at=now + timedelta(seconds=expires_in))

    async def revoke_token(self, token: str) -> None:
        await self._revoker.revoke_token(token)


def _nonempty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def google_email_from_id_token(
    id_token: str | None, *, client_id: str, now: datetime, leeway: timedelta
) -> str:
    """The verified email from the id_token returned by Google's token endpoint.

    No signature check, deliberately (decision T4.1 #2): this id_token comes straight from
    Google's token endpoint in the TLS response to our client-authenticated code exchange,
    so OIDC Core §3.1.3.7 (item 6) lets the TLS server validation stand in for the
    signature. We still check iss, aud, exp and email_verified, and never accept an
    id_token from any other channel this way. Avoids google-auth + requests as dependencies.
    """
    if not id_token:
        raise IdTokenInvalidError("missing")
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise IdTokenInvalidError("undecodable") from None
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise IdTokenInvalidError("issuer")
    aud = claims.get("aud")
    if isinstance(aud, list):
        # OIDC: with several audiences, azp must be us.
        if client_id not in aud or (len(aud) > 1 and claims.get("azp") != client_id):
            raise IdTokenInvalidError("audience")
    elif aud != client_id:
        raise IdTokenInvalidError("audience")
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or isinstance(exp, bool):
        raise IdTokenInvalidError("exp")
    if now.timestamp() >= exp + leeway.total_seconds():
        raise IdTokenInvalidError("expired")
    email = claims.get("email")
    verified = claims.get("email_verified")
    if not isinstance(email, str) or "@" not in email:
        raise IdTokenInvalidError("email")
    if verified is not True and verified != "true":
        raise IdTokenInvalidError("email_verified")
    return email
