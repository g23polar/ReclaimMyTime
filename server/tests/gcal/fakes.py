"""Recorded-HTTP Google for T4.1 tests: an httpx MockTransport handler with hand-written
token/revoke responses. Nothing here talks to Google."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.fernet import Fernet

from app.gcal.settings import CALENDAR_SCOPES, GcalSettings

CLIENT_ID = "1234567890-abc.apps.googleusercontent.com"
CLIENT_SECRET = "gcal-client-secret-xyz"
REDIRECT_URI = "https://api.reclaimmytime.example/gcal/callback"
TOKEN_URL = "https://oauth2.test/token"
REVOKE_URL = "https://oauth2.test/revoke"
AUTH_URL = "https://accounts.test/o/oauth2/v2/auth"
EMAIL = "reclaim.tester@gmail.com"
REFRESH_TOKEN = "1//0gREFRESH-token-0001"
ACCESS_TOKEN = "ya29.ACCESS-token-0001"
GRANTED = " ".join((*CALENDAR_SCOPES, "openid", "https://www.googleapis.com/auth/userinfo.email"))


def settings(**over: Any) -> GcalSettings:
    base: dict[str, Any] = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "state_key": Fernet.generate_key().decode(),
        "auth_url": AUTH_URL,
        "token_url": TOKEN_URL,
        "revoke_url": REVOKE_URL,
    }
    return GcalSettings(**(base | over))


def id_token(now: datetime, **claims: Any) -> str:
    """Google-shaped id_token. Signed with a throwaway HS256 key: the server never verifies
    the signature of a token-endpoint id_token (decision T4.1 #2)."""
    body: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": "110169484474386276334",
        "email": EMAIL,
        "email_verified": True,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not _DROP}
    return jwt.encode(body, "unverified-test-key-0123456789abcdef", algorithm="HS256")


_DROP = object()
DROP: Any = _DROP  # pass as a claim value to remove the claim


@dataclass
class Reply:
    status: int = 200
    json: Any = None
    raise_exc: Exception | None = None


@dataclass
class FakeGoogle:
    now: datetime
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    exchange: Reply | None = None  # None → a successful exchange
    refresh: Reply = field(default_factory=lambda: Reply(json=_refresh_ok()))
    revoke: Reply = field(default_factory=lambda: Reply(json={}))
    refresh_token: str | None = REFRESH_TOKEN
    email: str = EMAIL
    scope: str = GRANTED

    def exchange_ok(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "access_token": ACCESS_TOKEN,
            "expires_in": 3599,
            "scope": self.scope,
            "token_type": "Bearer",
            "id_token": id_token(self.now, email=self.email),
        }
        if self.refresh_token is not None:
            body["refresh_token"] = self.refresh_token
        return body

    def forms(self, url: str) -> list[dict[str, str]]:
        return [form for u, form in self.requests if u == url]

    @property
    def revoked(self) -> list[str]:
        return [form["token"] for form in self.forms(REVOKE_URL)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((url, form))
        if url == REVOKE_URL:
            reply = self.revoke
        elif url == TOKEN_URL and form.get("grant_type") == "authorization_code":
            reply = self.exchange or Reply(json=self.exchange_ok())
        elif url == TOKEN_URL and form.get("grant_type") == "refresh_token":
            reply = self.refresh
        else:
            return httpx.Response(404)
        if reply.raise_exc is not None:
            raise reply.raise_exc
        return httpx.Response(reply.status, json=reply.json)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _refresh_ok() -> dict[str, Any]:
    return {"access_token": ACCESS_TOKEN, "expires_in": 3599, "token_type": "Bearer"}
