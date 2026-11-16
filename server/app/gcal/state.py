"""PKCE (RFC 7636, S256) and the stateless OAuth `state` (decision T4.1 #1).

`state` is a Fernet token (AES-CBC + HMAC, so encrypted and authenticated) under
GCAL_STATE_KEY carrying `{u: user_id, v: code_verifier, n: nonce, exp}`. The verifier
never leaves the server in clear: Google only sees the S256 challenge, and the browser only
sees the opaque state. No DB table, no single-use tracking: replay within the TTL is
bounded by Google's own single-use authorization codes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken


def new_code_verifier() -> str:
    # 64 random bytes → 86 chars of [A-Za-z0-9_-], inside RFC 7636's 43..128 unreserved set.
    return secrets.token_urlsafe(64)


def s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class StateInvalidError(Exception):
    """Undecryptable (tampered / wrong key), malformed, or expired state."""


@dataclass(frozen=True)
class OAuthState:
    user_id: uuid.UUID
    code_verifier: str = field(repr=False)
    nonce: str
    expires_at: int  # unix seconds


class StateCodec:
    def __init__(self, key: str, ttl: timedelta) -> None:
        self._fernet = Fernet(key.encode())
        self._ttl = ttl

    def issue(self, user_id: uuid.UUID, now: datetime) -> tuple[str, OAuthState]:
        state = OAuthState(
            user_id=user_id,
            code_verifier=new_code_verifier(),
            nonce=secrets.token_urlsafe(16),
            expires_at=int((now + self._ttl).timestamp()),
        )
        payload: dict[str, object] = {
            "u": str(user_id),
            "v": state.code_verifier,
            "n": state.nonce,
            "exp": state.expires_at,
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        token = self._fernet.encrypt_at_time(raw, int(now.timestamp())).decode("ascii")
        return token, state

    def open(self, token: str | None, now: datetime) -> OAuthState:
        if not token:
            raise StateInvalidError("missing")
        try:
            raw = self._fernet.decrypt(token.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError):
            raise StateInvalidError("undecryptable") from None
        try:
            payload = json.loads(raw)
            user_id = uuid.UUID(payload["u"])
            verifier, nonce, exp = payload["v"], payload["n"], payload["exp"]
        except (ValueError, KeyError, TypeError):
            raise StateInvalidError("malformed") from None
        if not (isinstance(verifier, str) and 43 <= len(verifier) <= 128):
            raise StateInvalidError("malformed")
        if not isinstance(nonce, str) or not isinstance(exp, int) or isinstance(exp, bool):
            raise StateInvalidError("malformed")
        if now.timestamp() >= exp:
            raise StateInvalidError("expired")
        return OAuthState(user_id=user_id, code_verifier=verifier, nonce=nonce, expires_at=exp)
