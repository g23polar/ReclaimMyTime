"""PKCE (RFC 7636, S256) and the stateless OAuth `state` (decision T4.1 #1; T6.0 / D26).

`state` is a Fernet token (AES-CBC + HMAC, so encrypted and authenticated) under
GCAL_STATE_KEY. Two purposes share Google's single redirect URI (GET /gcal/callback):

- `connect` (GET /gcal/connect, T4.1): `{p: "connect", u: user_id, v: code_verifier,
  n: nonce, exp}`; completed by the bearer holder whose id is `u` (D24).
- `login` (POST /auth/google/start, T6.0): `{p: "login", h: nonce_hash, v, n, exp}`;
  completed by whoever presents the raw app nonce whose base64url(SHA-256) is `h` (D26).

A state minted before T6.0 carries no `p`; it decodes as `connect` (it must then carry `u`).
A state presented to an endpoint of the other purpose is invalid. The verifier never leaves
the server in clear: Google only sees the S256 challenge, and the browser only sees the
opaque state. No DB table, no single-use tracking: replay within the TTL is bounded by
Google's own single-use authorization codes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken

Purpose = Literal["connect", "login"]
CONNECT: Purpose = "connect"
LOGIN: Purpose = "login"

# base64url(SHA-256(nonce)) without padding: exactly 43 chars of the URL-safe alphabet,
# the last one restricted to the 16 values a 256-bit digest can end in.
NONCE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")


def new_code_verifier() -> str:
    # 64 random bytes → 86 chars of [A-Za-z0-9_-], inside RFC 7636's 43..128 unreserved set.
    return secrets.token_urlsafe(64)


def _b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def s256_challenge(verifier: str) -> str:
    return _b64url_sha256(verifier)


def nonce_hash(raw_nonce: str) -> str:
    """base64url(SHA-256(raw_nonce)), unpadded — what the app sends to /auth/google/start."""
    return _b64url_sha256(raw_nonce)


class StateInvalidError(Exception):
    """Undecryptable (tampered / wrong key), malformed, expired, or the wrong purpose."""


@dataclass(frozen=True)
class OAuthState:
    purpose: Purpose
    user_id: uuid.UUID | None  # connect only
    code_verifier: str = field(repr=False)
    nonce: str
    expires_at: int  # unix seconds
    nonce_hash: str | None = None  # login only


class StateCodec:
    def __init__(self, key: str, ttl: timedelta) -> None:
        self._fernet = Fernet(key.encode())
        self._ttl = ttl

    def _seal(self, payload: dict[str, object], now: datetime) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return self._fernet.encrypt_at_time(raw, int(now.timestamp())).decode("ascii")

    def issue(self, user_id: uuid.UUID, now: datetime) -> tuple[str, OAuthState]:
        """A `connect` state bound to the bearer user (GET /gcal/connect)."""
        state = OAuthState(
            purpose=CONNECT,
            user_id=user_id,
            code_verifier=new_code_verifier(),
            nonce=secrets.token_urlsafe(16),
            expires_at=int((now + self._ttl).timestamp()),
        )
        payload: dict[str, object] = {
            "p": CONNECT,
            "u": str(user_id),
            "v": state.code_verifier,
            "n": state.nonce,
            "exp": state.expires_at,
        }
        return self._seal(payload, now), state

    def issue_login(self, nonce_hash_value: str, now: datetime) -> tuple[str, OAuthState]:
        """A `login` state bound to the app nonce's hash (POST /auth/google/start)."""
        if not NONCE_HASH_RE.match(nonce_hash_value):
            raise ValueError("nonce_hash must be base64url(SHA-256(nonce)), unpadded")
        state = OAuthState(
            purpose=LOGIN,
            user_id=None,
            code_verifier=new_code_verifier(),
            nonce=secrets.token_urlsafe(16),
            expires_at=int((now + self._ttl).timestamp()),
            nonce_hash=nonce_hash_value,
        )
        payload: dict[str, object] = {
            "p": LOGIN,
            "h": nonce_hash_value,
            "v": state.code_verifier,
            "n": state.nonce,
            "exp": state.expires_at,
        }
        return self._seal(payload, now), state

    def _payload(self, token: str | None) -> dict[str, Any]:
        if not token:
            raise StateInvalidError("missing")
        try:
            raw = self._fernet.decrypt(token.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError):
            raise StateInvalidError("undecryptable") from None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise StateInvalidError("malformed") from None
        if not isinstance(payload, dict):
            raise StateInvalidError("malformed")
        return payload

    def peek_purpose(self, token: str | None) -> Purpose | None:
        """The purpose of an authentic state, ignoring expiry and every other field; None
        when it is missing, undecryptable or names no known purpose. Only used to pick the
        callback's app URL, never to accept a state."""
        try:
            payload = self._payload(token)
        except StateInvalidError:
            return None
        purpose = payload.get("p", CONNECT)  # pre-T6.0 states carry no purpose
        return purpose if purpose in (CONNECT, LOGIN) else None

    def open(
        self, token: str | None, now: datetime, *, purpose: Purpose | None = None
    ) -> OAuthState:
        """Decrypt and check a state. With `purpose`, a state of the other purpose is
        invalid (StateInvalidError("purpose"))."""
        payload = self._payload(token)
        kind = payload.get("p", CONNECT)
        try:
            verifier, nonce, exp = payload["v"], payload["n"], payload["exp"]
            if kind == CONNECT:
                user_id: uuid.UUID | None = uuid.UUID(payload["u"])
                hashed = None
            elif kind == LOGIN:
                user_id, hashed = None, payload["h"]
            else:
                raise StateInvalidError("malformed")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise StateInvalidError("malformed") from None
        if not (isinstance(verifier, str) and 43 <= len(verifier) <= 128):
            raise StateInvalidError("malformed")
        if not isinstance(nonce, str) or not isinstance(exp, int) or isinstance(exp, bool):
            raise StateInvalidError("malformed")
        if kind == LOGIN and not (isinstance(hashed, str) and NONCE_HASH_RE.match(hashed)):
            raise StateInvalidError("malformed")
        if now.timestamp() >= exp:
            raise StateInvalidError("expired")
        if purpose is not None and kind != purpose:
            raise StateInvalidError("purpose")
        return OAuthState(
            purpose=kind,
            user_id=user_id,
            code_verifier=verifier,
            nonce=nonce,
            expires_at=exp,
            nonce_hash=hashed,
        )
