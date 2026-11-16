"""Test doubles for auth: RSA-backed fake Apple JWKS + identity-token factory, frozen
clock, fake Apple REST client."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.auth.apple import AppleTokenResponse, hash_nonce

BUNDLE_ID = "com.reclaimmytime.app"
APPLE_ISS = "https://appleid.apple.com"
DEFAULT_SUB = "001234.apple-subject.0001"


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, now: datetime) -> None:
        self._now = now


@dataclass
class SigningKey:
    kid: str
    private: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )

    def public_jwk(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
        return jwk


class FakeJwksFetcher:
    """Serves the public halves of `keys`; counts fetches; can be made to fail.

    If `gate` is set, each fetch blocks until the event is set (to hold a fetch in flight).
    """

    def __init__(self, keys: list[SigningKey]) -> None:
        self.keys = keys
        self.fetch_count = 0
        self.fail = False
        self.gate: asyncio.Event | None = None
        self.document: Any = None  # if set, served verbatim instead of `keys`

    async def fetch(self) -> dict[str, Any]:
        self.fetch_count += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise ConnectionError("jwks down")
        if self.document is not None:
            return self.document
        return {"keys": [k.public_jwk() for k in self.keys]}


class TokenFactory:
    """Mints Apple-shaped identity tokens signed with a fixture key."""

    def __init__(self, key: SigningKey, clock: FrozenClock) -> None:
        self.key = key
        self.clock = clock

    def make(
        self,
        *,
        raw_nonce: str,
        sub: str = DEFAULT_SUB,
        email: str | None = "private@privaterelay.appleid.com",
        email_verified: Any = "true",
        is_private_email: Any = "true",
        aud: str | list[str] = BUNDLE_ID,
        iss: str = APPLE_ISS,
        ttl: timedelta = timedelta(minutes=10),
        nonce: str | None = None,
        include_nonce: bool = True,
        key: SigningKey | None = None,
        kid: str | None = None,
        alg: str = "RS256",
        extra: dict[str, Any] | None = None,
    ) -> str:
        key = key or self.key
        now = self.clock.now()
        claims: dict[str, Any] = {
            "iss": iss,
            "aud": aud,
            "sub": sub,
            "iat": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
            "auth_time": int(now.timestamp()),
        }
        if include_nonce:
            claims["nonce"] = nonce if nonce is not None else hash_nonce(raw_nonce)
            claims["nonce_supported"] = True
        if email is not None:
            claims["email"] = email
            claims["email_verified"] = email_verified
            claims["is_private_email"] = is_private_email
        claims.update(extra or {})
        return jwt.encode(claims, key.private, algorithm=alg, headers={"kid": kid or key.kid})


_UNSET: Any = object()


class FakeAppleClient:
    """Records calls in order; `events` is shared with the repo spy in DELETE /me tests.

    `exchange_code` returns an Apple-shaped id_token signed by `tokens`, for the subject
    in `code_subjects[code]` (default `DEFAULT_SUB`) and without a nonce claim. Set
    `id_token` to override it (None = Apple sent none), `exchange_error` to raise.
    """

    def __init__(
        self, tokens: TokenFactory, refresh_token: str | None = "apple-refresh-token-0001"
    ) -> None:
        self.tokens = tokens
        self.refresh_token = refresh_token
        self.code_subjects: dict[str, str] = {}
        self.id_token: Any = _UNSET
        self.exchange_error: Exception | None = None
        self.exchanged: list[str] = []
        self.revoked: list[str] = []
        self.events: list[str] = []
        self.revoke_error: Exception | None = None

    def id_token_for(self, sub: str, **claims: Any) -> str:
        claims.setdefault("include_nonce", False)
        return self.tokens.make(raw_nonce="", sub=sub, email=None, **claims)

    async def exchange_code(self, authorization_code: str) -> AppleTokenResponse:
        self.exchanged.append(authorization_code)
        if self.exchange_error is not None:
            raise self.exchange_error
        id_token = self.id_token
        if id_token is _UNSET:
            id_token = self.id_token_for(self.code_subjects.get(authorization_code, DEFAULT_SUB))
        return AppleTokenResponse(refresh_token=self.refresh_token, id_token=id_token)

    async def revoke_refresh_token(self, refresh_token: str) -> None:
        self.events.append("revoke")
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked.append(refresh_token)


class FakeGoogleRevoker:
    """GoogleRevoker double for DELETE /me. Appends "google" to the shared `events` list
    (pass FakeAppleClient.events) so tests can assert Google-before-Apple ordering."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.revoked: list[str] = []
        self.revoke_error: Exception | None = None

    async def revoke_token(self, token: str) -> None:
        self.events.append("google")
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked.append(token)
