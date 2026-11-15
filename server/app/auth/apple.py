"""Sign in with Apple: JWKS fetch + cache, identity-token verification, and the Apple
REST client (authorization-code exchange, token revocation).

Tokens and codes are never logged; log lines carry only kid/status/reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.auth.clock import Clock
from app.auth.errors import AppleTokenInvalidError, AppleUnavailableError
from app.auth.settings import AuthSettings

logger = logging.getLogger(__name__)

PRIVATE_RELAY_DOMAIN = "privaterelay.appleid.com"
_ALGORITHMS = ["RS256"]


# --------------------------------------------------------------------------- JWKS


class JwksFetcher(Protocol):
    async def fetch(self) -> Mapping[str, Any]:
        """Return the JWKS document (`{"keys": [...]}`)."""
        ...


class HttpJwksFetcher:
    def __init__(
        self,
        url: str,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._timeout_s = timeout_s
        self._transport = transport

    async def fetch(self) -> Mapping[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
            response = await client.get(self._url)
            response.raise_for_status()
            return response.json()


class JwksUnavailableError(Exception):
    """JWKS could not be fetched and no cached copy is usable."""


class UnusableJwksError(Exception):
    """The JWKS document parsed but holds no usable RS256 signing key."""


DEFAULT_REFRESH_BACKOFF = timedelta(seconds=60)


class JwksCache:
    """Caches Apple's public keys by `kid` with a TTL.

    - A fresh hit is served with no await on any lock or fetch.
    - Stale keys, or an unknown `kid` (Apple rotates keys), trigger a refetch. A kid-miss
      refetch is rate-limited by `min_refresh_interval` so garbage tokens cannot hammer
      Apple.
    - Every attempt's time is recorded. After a failed attempt nothing is fetched for
      `refresh_backoff`; callers get stale keys if any, else `JwksUnavailableError`.
      A document with no usable RS256 signing key (`{}`, `{"keys": []}`, only
      unsupported kty/alg/use) is a failed attempt: the previous keys are kept.
    - Single-flight: at most one fetch runs; concurrent callers await its outcome.
    """

    def __init__(
        self,
        fetcher: JwksFetcher,
        clock: Clock,
        ttl: timedelta,
        min_refresh_interval: timedelta,
        refresh_backoff: timedelta = DEFAULT_REFRESH_BACKOFF,
    ) -> None:
        self._fetcher = fetcher
        self._clock = clock
        self._ttl = ttl
        self._min_refresh_interval = min_refresh_interval
        self._refresh_backoff = refresh_backoff
        self._keys: dict[str, Any] = {}
        self._fetched_at: datetime | None = None  # last successful attempt
        self._attempted_at: datetime | None = None  # last attempt, success or failure
        self._last_attempt_failed = False
        self._inflight: asyncio.Task[None] | None = None

    async def get_key(self, kid: str) -> Any | None:
        now = self._clock.now()
        if self._is_fresh(now) and kid in self._keys:
            return self._keys[kid]
        # No await between the check and starting the task, so only one task is created.
        if self._inflight is None and self._should_fetch(now, kid):
            self._attempted_at = now
            self._inflight = asyncio.create_task(self._fetch(now))
        if self._inflight is not None:
            # shield: a cancelled caller must not cancel the fetch other callers await.
            await asyncio.shield(self._inflight)
        return self._serve(kid)

    def _is_fresh(self, now: datetime) -> bool:
        return self._fetched_at is not None and now - self._fetched_at < self._ttl

    def _should_fetch(self, now: datetime, kid: str) -> bool:
        if self._attempted_at is not None:
            since_attempt = now - self._attempted_at
            if self._last_attempt_failed and since_attempt < self._refresh_backoff:
                return False
        if not self._is_fresh(now):
            return True
        return self._attempted_at is None or now - self._attempted_at >= self._min_refresh_interval

    def _serve(self, kid: str) -> Any | None:
        if kid in self._keys:
            return self._keys[kid]
        if not self._keys and self._last_attempt_failed:
            raise JwksUnavailableError("apple jwks unavailable")
        return None

    async def _fetch(self, started_at: datetime) -> None:
        try:
            document = await self._fetcher.fetch()
            keys = _parse_jwks(document)
        except Exception as exc:
            self._last_attempt_failed = True
            logger.warning(
                "apple jwks refresh failed (%s); %s",
                type(exc).__name__,
                "serving cached keys" if self._keys else "no cached keys",
            )
        else:
            self._keys = keys
            self._fetched_at = started_at
            self._last_attempt_failed = False
        finally:
            self._inflight = None


def _parse_jwks(document: Any) -> dict[str, Any]:
    """RS256 signing keys by kid; UnusableJwksError if there are none."""
    entries = document.get("keys") if isinstance(document, Mapping) else None
    keys: dict[str, Any] = {}
    for jwk in entries if isinstance(entries, list) else []:
        if not isinstance(jwk, Mapping) or jwk.get("kty") != "RSA":
            continue
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if jwk.get("alg") not in (None, "RS256") or jwk.get("use") not in (None, "sig"):
            continue
        try:
            keys[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            continue
    if not keys:
        raise UnusableJwksError("no usable RS256 signing keys")
    return keys


# --------------------------------------------------------------------------- verification


@dataclass(frozen=True)
class AppleClaims:
    subject: str
    email: str | None
    email_verified: bool
    is_private_email: bool
    expires_at: datetime


def hash_nonce(raw_nonce: str) -> str:
    """SHA-256 hex digest — what the iOS app passes to Apple as the request nonce."""
    return hashlib.sha256(raw_nonce.encode()).hexdigest()


def _truthy(value: Any) -> bool:
    # Apple sends some booleans as the strings "true"/"false".
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return False


class AppleIdentityVerifier:
    def __init__(self, jwks: JwksCache, settings: AuthSettings, clock: Clock) -> None:
        self._jwks = jwks
        self._settings = settings
        self._clock = clock

    async def verify(self, identity_token: str, raw_nonce: str) -> AppleClaims:
        """Verify signature, iss, aud, exp and nonce; raise AppleTokenInvalidError(reason)."""
        claims, expires_at = await self._verified_claims(identity_token)
        if not self._nonce_matches(claims.get("nonce"), raw_nonce):
            raise AppleTokenInvalidError("nonce_mismatch")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AppleTokenInvalidError("signature")

        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        is_private = _truthy(claims.get("is_private_email")) or (
            email is not None and email.lower().endswith("@" + PRIVATE_RELAY_DOMAIN)
        )
        return AppleClaims(
            subject=subject,
            email=email,
            email_verified=_truthy(claims.get("email_verified")),
            is_private_email=is_private,
            expires_at=expires_at,
        )

    async def verify_code_subject(self, id_token: str | None, raw_nonce: str) -> str:
        """Verify the id_token Apple returned from the code exchange; return its `sub`.

        Same checks as `verify` except the nonce, which is checked only when present.
        Any failure (including a missing id_token) is AppleTokenInvalidError with
        reason `authorization_code`; a JWKS outage is still AppleUnavailableError.
        """
        if not id_token:
            raise AppleTokenInvalidError("authorization_code")
        try:
            claims, _ = await self._verified_claims(id_token)
        except AppleTokenInvalidError:
            raise AppleTokenInvalidError("authorization_code") from None
        if "nonce" in claims and not self._nonce_matches(claims["nonce"], raw_nonce):
            raise AppleTokenInvalidError("authorization_code")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AppleTokenInvalidError("authorization_code")
        return subject

    @staticmethod
    def _nonce_matches(token_nonce: Any, raw_nonce: str) -> bool:
        return isinstance(token_nonce, str) and hmac.compare_digest(
            token_nonce, hash_nonce(raw_nonce)
        )

    async def _verified_claims(self, token: str) -> tuple[dict[str, Any], datetime]:
        """Signature, iss, aud and exp (with leeway); returns the claims and exp."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AppleTokenInvalidError("signature") from None
        kid = header.get("kid")
        if header.get("alg") not in _ALGORITHMS or not isinstance(kid, str):
            raise AppleTokenInvalidError("signature")
        try:
            key = await self._jwks.get_key(kid)
        except JwksUnavailableError:
            raise AppleUnavailableError() from None
        if key is None:
            logger.info("apple token with unknown kid=%s", kid)
            raise AppleTokenInvalidError("signature")

        try:
            # Signature only here; claims are checked below against the injected clock.
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=_ALGORITHMS,
                options={
                    "verify_signature": True,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )
        except jwt.PyJWTError:
            raise AppleTokenInvalidError("signature") from None

        if claims.get("iss") != self._settings.apple_issuer:
            raise AppleTokenInvalidError("issuer")
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self._settings.apple_bundle_id not in audiences:
            raise AppleTokenInvalidError("audience")
        exp = claims.get("exp")
        if not isinstance(exp, int | float) or isinstance(exp, bool):
            raise AppleTokenInvalidError("expired")
        expires_at = datetime.fromtimestamp(exp, UTC)
        if self._clock.now() >= expires_at + self._settings.token_leeway:
            raise AppleTokenInvalidError("expired")
        return claims, expires_at


# --------------------------------------------------------------------------- nonce replay


class NonceGuard(Protocol):
    """Replay guard with a reservation step around the authorization-code exchange."""

    async def reserve(self, nonce_hash: str, expires_at: datetime) -> bool:
        """Mark the nonce in flight; False if it is in flight or already consumed.

        `expires_at` is the identity token's exp; the entry is retained until
        `expires_at + token_leeway` (the verifier's acceptance window).
        """
        ...

    async def release(self, nonce_hash: str) -> None:
        """Drop a reservation (the request never reached Apple); a retry may reserve it."""
        ...

    async def commit(self, nonce_hash: str) -> None:
        """Keep the nonce consumed for the rest of its retention window."""
        ...


class InMemoryNonceGuard:
    """Process-local replay guard.

    TODO(T2.1): process-local only — with several Fly machines a replay could land on
    another instance. Cross-instance protection currently comes from Apple's single-use
    authorization code (exchanged on every sign-in). A shared store (a `used_nonces`
    table, not in ARCH §3) would close the gap; needs a main-session decision.
    """

    def __init__(self, clock: Clock, *, retention_leeway: timedelta) -> None:
        self._clock = clock
        self._retention_leeway = retention_leeway
        self._entries: dict[str, datetime] = {}  # nonce hash -> retain until
        self._lock = asyncio.Lock()

    async def reserve(self, nonce_hash: str, expires_at: datetime) -> bool:
        async with self._lock:
            now = self._clock.now()
            self._entries = {n: until for n, until in self._entries.items() if until > now}
            if nonce_hash in self._entries:
                return False
            self._entries[nonce_hash] = expires_at + self._retention_leeway
            return True

    async def release(self, nonce_hash: str) -> None:
        async with self._lock:
            self._entries.pop(nonce_hash, None)

    async def commit(self, nonce_hash: str) -> None:
        # The reservation already holds the entry until its retention deadline.
        return None


# --------------------------------------------------------------------------- Apple REST


class AppleCodeRejectedError(AppleTokenInvalidError):
    """Apple refused the authorization code (invalid_grant etc.)."""

    def __init__(self) -> None:
        super().__init__("authorization_code")


class AppleUnreachableError(AppleUnavailableError):
    """The request cannot have reached Apple (connect failure/timeout, no pool slot)."""


# Raised before the request is written, so Apple never saw the code.
_UNREACHABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


@dataclass(frozen=True)
class AppleTokenResponse:
    refresh_token: str | None = field(repr=False)
    id_token: str | None = field(repr=False)


class AppleClient(Protocol):
    async def exchange_code(self, authorization_code: str) -> AppleTokenResponse:
        """Exchange the one-time code for Apple's refresh token and id_token.

        AppleUnreachableError: the request cannot have reached Apple. AppleCodeRejectedError
        or AppleUnavailableError otherwise.
        """
        ...

    async def revoke_refresh_token(self, refresh_token: str) -> None:
        """Revoke a refresh token. Already-invalid tokens count as revoked."""
        ...


class HttpAppleClient:
    """https://developer.apple.com/documentation/sign_in_with_apple/rest_api"""

    def __init__(
        self,
        settings: AuthSettings,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._transport = transport

    def _client_secret(self) -> str:
        s = self._settings
        if not (s.apple_team_id and s.apple_key_id and s.apple_private_key_pem):
            raise RuntimeError("APPLE_TEAM_ID, APPLE_KEY_ID and APPLE_PRIVATE_KEY must be set")
        now = self._clock.now()
        return jwt.encode(
            {
                "iss": s.apple_team_id,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "aud": s.apple_issuer,
                "sub": s.apple_bundle_id,
            },
            s.apple_private_key_pem,
            algorithm="ES256",
            headers={"kid": s.apple_key_id},
        )

    async def _post(self, url: str, data: dict[str, str]) -> httpx.Response:
        form = {"client_id": self._settings.apple_bundle_id, "client_secret": self._client_secret()}
        form.update(data)
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.apple_http_timeout_s, transport=self._transport
            ) as client:
                return await client.post(url, data=form)
        except _UNREACHABLE as exc:
            logger.warning("apple unreachable: %s", type(exc).__name__)
            raise AppleUnreachableError() from None
        except httpx.HTTPError as exc:
            logger.warning("apple request failed: %s", type(exc).__name__)
            raise AppleUnavailableError() from None

    async def exchange_code(self, authorization_code: str) -> AppleTokenResponse:
        response = await self._post(
            self._settings.apple_token_url,
            {"grant_type": "authorization_code", "code": authorization_code},
        )
        if response.status_code == 400:
            logger.info("apple rejected authorization code: %s", _error_code(response) or "unknown")
            raise AppleCodeRejectedError()
        if response.status_code != 200:
            logger.warning("apple token endpoint returned %s", response.status_code)
            raise AppleUnavailableError()
        try:
            body = response.json()
        except ValueError:
            logger.warning("apple token endpoint returned unparseable body")
            raise AppleUnavailableError() from None
        if not isinstance(body, dict):
            body = {}
        return AppleTokenResponse(
            refresh_token=_nonempty_str(body.get("refresh_token")),
            id_token=_nonempty_str(body.get("id_token")),
        )

    async def revoke_refresh_token(self, refresh_token: str) -> None:
        response = await self._post(
            self._settings.apple_revoke_url,
            {"token": refresh_token, "token_type_hint": "refresh_token"},
        )
        if response.status_code == 200:
            return
        if response.status_code == 400 and _error_code(response) == "invalid_grant":
            return  # already revoked (e.g. user removed the app in Apple ID settings)
        logger.warning("apple revoke endpoint returned %s", response.status_code)
        raise AppleUnavailableError()


def _nonempty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _error_code(response: httpx.Response) -> str | None:
    """Apple's OAuth `error` string, or None for any other body shape (null, list,
    string, number, non-JSON, undecodable)."""
    try:
        body = response.json()
    except ValueError:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        return None
    value = body.get("error") if isinstance(body, dict) else None
    return value if isinstance(value, str) else None
