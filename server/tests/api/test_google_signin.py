"""T6.0 Sign in with Google (contracts/NOTES.md D26) on Postgres, with recorded Google HTTP
(MockTransport; no live Google). Covers POST /auth/google/start, the login leg of the shared
GET /gcal/callback, POST /auth/google/complete (identity, session, connection, partial
grants, errors, lock/transaction discipline), and the auth routes with Apple unconfigured
(/auth/apple → 503, DELETE /me with and without an Apple identity)."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.apple import UnconfiguredAppleClient
from app.auth.crypto import TokenCipher
from app.auth.dependencies import AuthRuntime, build_runtime, sqlalchemy_repository_provider
from app.auth.router import router as auth_router
from app.auth.settings import AuthSettings
from app.db.models import GoogleConnection, Identity, User, UserSession
from app.gcal.client import HttpGoogleOAuthClient, HttpGoogleRevoker
from app.gcal.runtime import GcalRuntime, build_gcal_runtime
from app.gcal.settings import GcalSettings
from app.gcal.state import LOGIN, nonce_hash, s256_challenge
from tests.api.conftest import SettableClock
from tests.api.contract import SPEC, assert_conforms
from tests.db.conftest import DATABASE_URL, requires_db
from tests.gcal.fakes import (
    ACCESS_TOKEN,
    CLIENT_ID,
    EMAIL,
    REFRESH_TOKEN,
    REVOKE_URL,
    TOKEN_URL,
    FakeGoogle,
    Reply,
    id_token,
    settings,
)

pytestmark = requires_db

FREEBUSY = "https://www.googleapis.com/auth/calendar.freebusy"
APP_CREATED = "https://www.googleapis.com/auth/calendar.app.created"
ALL_SCOPES = f"{FREEBUSY} {APP_CREATED} openid https://www.googleapis.com/auth/userinfo.email"
NO_CALENDAR = "openid https://www.googleapis.com/auth/userinfo.email"
LOGIN_CB = "reclaimmytime://auth/google/callback"
CONNECT_CB = "reclaimmytime://gcal/callback"
SUB = "110169484474386276334"
NONCE = "app-held-nonce-0123456789abcdef"
LOCATION_PATTERN = SPEC["paths"]["/gcal/callback"]["get"]["responses"]["302"]["headers"][
    "Location"
]["schema"]["pattern"]


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def google(clock: SettableClock) -> FakeGoogle:
    return FakeGoogle(now=clock.current)


@pytest.fixture
def gsettings() -> GcalSettings:
    return settings()


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher([(3, Fernet.generate_key().decode())])


@pytest.fixture
def gcal_rt(
    app: FastAPI,
    google: FakeGoogle,
    gsettings: GcalSettings,
    cipher: TokenCipher,
    clock: SettableClock,
) -> GcalRuntime:
    rt = build_gcal_runtime(
        gsettings,
        cipher=cipher,
        client=HttpGoogleOAuthClient(gsettings, transport=google.transport()),
        clock=clock,
    )
    app.state.gcal_runtime = rt
    return rt


@pytest.fixture
def auth_rt(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    google: FakeGoogle,
    cipher: TokenCipher,
    clock: SettableClock,
) -> AuthRuntime:
    """Production-shaped auth runtime with Apple NOT configured (T6.0)."""
    keys = ((3, Fernet.generate_key().decode()),)
    rt = build_runtime(
        AuthSettings(apple_bundle_id="", fernet_keys=keys),
        repository_provider=sqlalchemy_repository_provider(maker),
        clock=clock,
        google_revoker=HttpGoogleRevoker(REVOKE_URL, transport=google.transport()),
    )
    # DELETE /me decrypts the connection the gcal runtime stored: share its cipher.
    rt.cipher = cipher
    app.state.auth_runtime = rt
    app.include_router(auth_router)
    return rt


@pytest.fixture
async def anon(app: FastAPI, gcal_rt: GcalRuntime, auth_rt: AuthRuntime) -> AsyncIterator[Any]:
    """A client with no bearer and no X-Timezone, as the sign-in screen has."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------- helpers


def exchange(google: FakeGoogle, *, sub: str = SUB, email: str = EMAIL, **over: Any) -> None:
    """Script the next code exchange's answer."""
    body: dict[str, Any] = {
        "access_token": ACCESS_TOKEN,
        "expires_in": 3599,
        "scope": ALL_SCOPES,
        "token_type": "Bearer",
        "id_token": id_token(google.now, sub=sub, email=email),
        "refresh_token": REFRESH_TOKEN,
    }
    body.update(over)
    google.exchange = Reply(json={k: v for k, v in body.items() if v is not None})


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


async def start(c: httpx.AsyncClient, nonce: str = NONCE) -> dict[str, str]:
    resp = await c.post("/auth/google/start", json={"nonce_hash": nonce_hash(nonce)})
    assert resp.status_code == 200, resp.text
    assert_conforms("startGoogleSignIn", 200, resp.json())
    return query(resp.json()["authorization_url"])


async def callback(c: httpx.AsyncClient, **params: str) -> str:
    resp = await c.get("/gcal/callback", params=params)
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert re.match(LOCATION_PATTERN, location), location
    assert resp.headers["cache-control"] == "no-store"
    return location


async def authorize(c: httpx.AsyncClient, nonce: str = NONCE, code: str = "4/0A-login") -> dict:
    state = (await start(c, nonce))["state"]
    location = await callback(c, state=state, code=code, scope=ALL_SCOPES)
    assert location.startswith(f"{LOGIN_CB}?result=authorized"), location
    q = query(location)
    return {"auth_code": q["auth_code"], "state": q["state"], "nonce": nonce}


async def complete(c: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    resp = await c.post("/auth/google/complete", json=body)
    if resp.status_code != 422:
        assert_conforms("completeGoogleSignIn", resp.status_code, resp.json())
    return resp


async def sign_in(c: httpx.AsyncClient, nonce: str = NONCE, **kw: Any) -> httpx.Response:
    return await complete(c, await authorize(c, nonce, **kw))


async def rows(maker: async_sessionmaker[AsyncSession], model: Any, **where: Any) -> list[Any]:
    async with maker() as s:
        stmt = select(model).filter_by(**where)
        return list((await s.execute(stmt)).scalars())


async def count(maker: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with maker() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


def bearer(resp: httpx.Response) -> dict[str, str]:
    return {"Authorization": f"Bearer {resp.json()['token']}", "X-Timezone": "Europe/Paris"}


def exchanges(google: FakeGoogle) -> list[dict[str, str]]:
    return [f for f in google.forms(TOKEN_URL) if f.get("grant_type") == "authorization_code"]


# ---------------------------------------------------------------- POST /auth/google/start


async def test_start_builds_combined_consent_with_login_state(
    anon: httpx.AsyncClient, gcal_rt: GcalRuntime, gsettings: GcalSettings, clock: SettableClock
) -> None:
    q = await start(anon)
    state = gcal_rt.state.open(q.pop("state"), clock.current, purpose=LOGIN)
    challenge = q.pop("code_challenge")
    assert q == {
        "client_id": CLIENT_ID,
        "redirect_uri": gsettings.redirect_uri,
        "response_type": "code",
        "scope": f"{FREEBUSY} {APP_CREATED} openid email",
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge_method": "S256",
    }
    assert state.nonce_hash == nonce_hash(NONCE) and state.user_id is None
    assert challenge == s256_challenge(state.code_verifier)
    assert state.expires_at == int((clock.current + timedelta(minutes=10)).timestamp())


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"nonce_hash": ""},
        {"nonce_hash": nonce_hash(NONCE) + "="},  # padded
        {"nonce_hash": nonce_hash(NONCE)[:-1]},
        {"nonce_hash": "a" * 64},  # hex digest, not base64url
        {"nonce_hash": nonce_hash(NONCE).replace(nonce_hash(NONCE)[0], "+", 1)},
        {"nonce_hash": nonce_hash(NONCE), "extra": 1},
        {"nonce_hash": 12},
        [nonce_hash(NONCE)],
    ],
)
async def test_start_validates_nonce_hash(anon: httpx.AsyncClient, body: Any) -> None:
    resp = await anon.post("/auth/google/start", json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_failed"
    assert_conforms("startGoogleSignIn", 422, resp.json())


# ---------------------------------------------------------------- GET /gcal/callback (login)


async def test_login_callback_hands_code_to_signin_url_without_exchanging(
    anon: httpx.AsyncClient, google: FakeGoogle
) -> None:
    state = (await start(anon))["state"]
    location = await callback(anon, state=state, code="4/0A+code/x", scope=ALL_SCOPES)
    assert location.startswith(f"{LOGIN_CB}?result=authorized&auth_code=4%2F0A%2Bcode%2Fx&state=")
    assert query(location) == {"result": "authorized", "auth_code": "4/0A+code/x", "state": state}
    assert google.requests == []


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"error": "access_denied"}, "gcal_access_denied"),
        ({"error": "server_error"}, "gcal_exchange_failed"),
        ({}, "gcal_exchange_failed"),  # neither code nor error
    ],
)
async def test_login_callback_errors_go_to_signin_url(
    anon: httpx.AsyncClient, google: FakeGoogle, params: dict[str, str], code: str
) -> None:
    state = (await start(anon))["state"]
    location = await callback(anon, state=state, **params)
    assert location == f"{LOGIN_CB}?result=error&code={code}"
    assert google.requests == []


async def test_expired_login_state_at_callback_goes_to_signin_url(
    anon: httpx.AsyncClient, clock: SettableClock
) -> None:
    state = (await start(anon))["state"]
    clock.current += timedelta(minutes=10)
    location = await callback(anon, state=state, code="4/0A-late")
    assert location == f"{LOGIN_CB}?result=error&code=gcal_state_invalid"


@pytest.mark.parametrize("state", [None, "garbage", "gAAAAAB" + "x" * 80])
async def test_unreadable_state_falls_back_to_connect_url(
    anon: httpx.AsyncClient, state: str | None
) -> None:
    params = {"code": "4/0A-x"} if state is None else {"code": "4/0A-x", "state": state}
    location = await callback(anon, **params)
    assert location == f"{CONNECT_CB}?result=error&code=gcal_state_invalid"


async def test_connect_state_still_goes_to_connect_url(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, google: FakeGoogle
) -> None:
    resp = await client.get("/gcal/connect")
    state = query(resp.json()["authorization_url"])["state"]
    location = await callback(anon, state=state, code="4/0A-connect")
    assert location.startswith(f"{CONNECT_CB}?result=authorized&auth_code=4%2F0A-connect&state=")
    denied = await callback(anon, state=state, error="access_denied")
    assert denied == f"{CONNECT_CB}?result=error&code=gcal_access_denied"
    assert google.requests == []


# ---------------------------------------------------------------- POST /auth/google/complete


async def test_first_sign_in_with_calendar_creates_user_session_and_connection(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
    gcal_rt: GcalRuntime,
    clock: SettableClock,
) -> None:
    exchange(google)
    body = await authorize(anon)
    resp = await complete(anon, body)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["is_new_user"] is True and out["gcal_connected"] is True
    assert set(out) == {"token", "session", "user", "is_new_user", "gcal_connected"}
    uid = uuid.UUID(out["user"]["id"])
    assert out["user"]["email"] == EMAIL

    (identity,) = await rows(maker, Identity, provider="google")
    assert (identity.subject, identity.user_id, identity.email) == (SUB, uid, EMAIL)
    assert identity.email_verified is True and identity.apple_refresh_token_enc is None
    (conn,) = await rows(maker, GoogleConnection, user_id=uid)
    assert (conn.google_email, conn.status) == (EMAIL, "active")
    assert sorted(conn.scopes) == sorted([FREEBUSY, APP_CREATED, *NO_CALENDAR.split()])
    assert cipher.decrypt(conn.refresh_token_enc) == REFRESH_TOKEN
    assert REFRESH_TOKEN.encode() not in conn.refresh_token_enc
    (session,) = await rows(maker, UserSession, user_id=uid)
    assert str(session.id) == out["session"]["id"]

    # PKCE: the verifier sent to Google matches the challenge in the consent URL.
    (form,) = exchanges(google)
    verifier = gcal_rt.state.open(body["state"], clock.current).code_verifier
    assert form["code"] == body["auth_code"] and form["code_verifier"] == verifier
    assert google.revoked == []

    # The token is a working bearer.
    me = await anon.get("/me", headers=bearer(resp))
    assert me.status_code == 200 and me.json()["id"] == str(uid)


async def test_calendar_unticked_signs_in_without_connection_and_revokes_partial_grant(
    anon: httpx.AsyncClient, google: FakeGoogle, maker: async_sessionmaker[AsyncSession]
) -> None:
    exchange(google, scope=NO_CALENDAR)
    resp = await sign_in(anon)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["is_new_user"] is True and out["gcal_connected"] is False
    uid = uuid.UUID(out["user"]["id"])
    assert await rows(maker, GoogleConnection, user_id=uid) == []
    assert google.revoked == [REFRESH_TOKEN]
    assert (await anon.get("/me", headers=bearer(resp))).status_code == 200


async def test_only_one_calendar_scope_is_the_same_as_none(
    anon: httpx.AsyncClient, google: FakeGoogle, maker: async_sessionmaker[AsyncSession]
) -> None:
    exchange(google, scope=f"{FREEBUSY} {NO_CALENDAR}")
    resp = await sign_in(anon)
    assert resp.status_code == 200 and resp.json()["gcal_connected"] is False
    assert await count(maker, GoogleConnection) == 0
    assert google.revoked == [REFRESH_TOKEN]


async def test_partial_grant_revoke_failure_never_fails_sign_in(
    anon: httpx.AsyncClient, google: FakeGoogle
) -> None:
    exchange(google, scope=NO_CALENDAR)
    google.revoke = Reply(503, {"error": "backend"})
    resp = await sign_in(anon)
    assert resp.status_code == 200 and resp.json()["gcal_connected"] is False


async def test_no_refresh_token_signs_in_without_connection(
    anon: httpx.AsyncClient, google: FakeGoogle, maker: async_sessionmaker[AsyncSession]
) -> None:
    exchange(google, refresh_token=None)
    resp = await sign_in(anon)
    assert resp.status_code == 200, resp.text
    assert resp.json()["gcal_connected"] is False
    assert await count(maker, GoogleConnection) == 0
    assert google.revoked == [ACCESS_TOKEN]  # the only token we got


async def test_returning_google_identity_is_the_same_user(
    anon: httpx.AsyncClient, google: FakeGoogle, maker: async_sessionmaker[AsyncSession]
) -> None:
    exchange(google)
    first = (await sign_in(anon, code="4/0A-one")).json()
    async with maker() as s, s.begin():
        await s.execute(
            GoogleConnection.__table__.update().values(app_calendar_id="c_app@group.calendar")
        )
    exchange(google, email="Reclaim.Tester@gmail.com", refresh_token="1//0g-second")
    resp = await sign_in(anon, nonce="another-nonce-0123456789", code="4/0A-two")
    assert resp.status_code == 200, resp.text
    second = resp.json()
    assert second["user"]["id"] == first["user"]["id"]
    assert second["is_new_user"] is False and second["gcal_connected"] is True
    assert second["token"] != first["token"]
    uid = uuid.UUID(first["user"]["id"])
    assert len(await rows(maker, UserSession, user_id=uid)) == 2
    assert await count(maker, User) == 2  # the fixture's user + this one
    (conn,) = await rows(maker, GoogleConnection, user_id=uid)
    assert conn.app_calendar_id == "c_app@group.calendar"  # same account keeps it (T4.1)
    assert google.revoked == []


async def test_returning_user_unticking_calendar_keeps_existing_connection(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
) -> None:
    exchange(google)
    first = (await sign_in(anon)).json()
    exchange(google, scope=NO_CALENDAR, refresh_token="1//0g-partial")
    resp = await sign_in(anon, nonce="another-nonce-0123456789", code="4/0A-two")
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["id"] == first["user"]["id"]
    # The stored connection survives and is reported; the partial grant shares its
    # account's grant, so it is NOT revoked (that would kill the stored one).
    assert resp.json()["gcal_connected"] is True
    (conn,) = await rows(maker, GoogleConnection)
    assert cipher.decrypt(conn.refresh_token_enc) == REFRESH_TOKEN and conn.status == "active"
    assert google.revoked == []


async def test_sign_in_with_another_calendar_account_replaces_and_revokes_old(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
) -> None:
    """A user who connected another Google account via /gcal/connect, then signs in with
    calendar scopes: the T4.1 persist path replaces it and revokes the old account."""
    exchange(google)
    uid = uuid.UUID((await sign_in(anon)).json()["user"]["id"])
    async with maker() as s, s.begin():
        await s.execute(
            GoogleConnection.__table__.update().values(
                google_email="other@gmail.com",
                refresh_token_enc=cipher.encrypt("1//0g-other"),
                app_calendar_id="c_other",
            )
        )
    exchange(google, refresh_token="1//0g-new")
    resp = await sign_in(anon, nonce="another-nonce-0123456789", code="4/0A-two")
    assert resp.status_code == 200 and resp.json()["gcal_connected"] is True
    (conn,) = await rows(maker, GoogleConnection, user_id=uid)
    assert (conn.google_email, conn.app_calendar_id) == (EMAIL, None)
    assert cipher.decrypt(conn.refresh_token_enc) == "1//0g-new"
    assert google.revoked == ["1//0g-other"]


async def test_no_account_merging_by_email(
    anon: httpx.AsyncClient, google: FakeGoogle, maker: async_sessionmaker[AsyncSession]
) -> None:
    exchange(google, sub="sub-one")
    one = (await sign_in(anon)).json()
    exchange(google, sub="sub-two")
    two = (await sign_in(anon, nonce="another-nonce-0123456789", code="4/0A-two")).json()
    assert one["user"]["id"] != two["user"]["id"]
    assert two["is_new_user"] is True
    assert (one["user"]["email"], two["user"]["email"]) == (EMAIL, None)  # users.email UNIQUE
    assert {i.subject for i in await rows(maker, Identity, provider="google")} == {
        "sub-one",
        "sub-two",
    }


async def test_concurrent_first_sign_ins_converge_on_one_user(
    anon: httpx.AsyncClient, google: FakeGoogle, maker: async_sessionmaker[AsyncSession]
) -> None:
    exchange(google)
    a = await authorize(anon, "nonce-a-0123456789abcdef", "4/0A-a")
    b = await authorize(anon, "nonce-b-0123456789abcdef", "4/0A-b")
    ra, rb = await asyncio.wait_for(asyncio.gather(complete(anon, a), complete(anon, b)), 30)
    assert ra.status_code == rb.status_code == 200, (ra.text, rb.text)
    assert ra.json()["user"]["id"] == rb.json()["user"]["id"]
    assert sorted([ra.json()["is_new_user"], rb.json()["is_new_user"]]) == [False, True]
    assert len(await rows(maker, Identity, provider="google")) == 1
    assert len(await rows(maker, GoogleConnection)) == 1


# ---------------------------------------------------------------- cross-user same account (review)


async def _other_user_connected(
    maker: async_sessionmaker[AsyncSession], cipher: TokenCipher, email: str
) -> uuid.UUID:
    other = uuid.uuid4()
    async with maker() as s, s.begin():
        await s.execute(insert(User).values(id=other))
        await s.execute(
            insert(GoogleConnection).values(
                user_id=other,
                google_email=email,
                refresh_token_enc=cipher.encrypt("1//0g-other-user"),
                key_id=cipher.primary_key_id,
                scopes=[FREEBUSY, APP_CREATED],
                status="active",
            )
        )
    return other


async def test_unticked_sign_in_with_an_account_another_user_connected_is_not_revoked(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
) -> None:
    """User A connected Google account G via /gcal/connect; B signs in with G (a new Google
    identity → a new user) and unticks the calendar scopes. Revoking B's partial grant would
    kill A's connection (grant-wide), so it is left alone."""
    other = await _other_user_connected(maker, cipher, EMAIL.upper())  # citext
    exchange(google, scope=NO_CALENDAR)
    resp = await sign_in(anon)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["id"] != str(other)
    assert resp.json()["gcal_connected"] is False
    assert google.revoked == []
    (held,) = await rows(maker, GoogleConnection, user_id=other)
    assert held.status == "active"


async def test_sign_in_without_refresh_token_for_another_users_account_is_not_revoked(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
) -> None:
    await _other_user_connected(maker, cipher, EMAIL)
    exchange(google, refresh_token=None)
    resp = await sign_in(anon)
    assert resp.status_code == 200 and resp.json()["gcal_connected"] is False
    assert google.revoked == []


async def test_unticked_sign_in_revokes_when_other_users_hold_different_accounts(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
) -> None:
    await _other_user_connected(maker, cipher, "someone.else@gmail.com")
    exchange(google, scope=NO_CALENDAR)
    resp = await sign_in(anon)
    assert resp.status_code == 200 and resp.json()["gcal_connected"] is False
    assert google.revoked == [REFRESH_TOKEN]


async def test_sign_in_replacing_an_account_another_user_still_holds_does_not_revoke_it(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
) -> None:
    exchange(google)
    uid = uuid.UUID((await sign_in(anon)).json()["user"]["id"])
    async with maker() as s, s.begin():
        await s.execute(
            GoogleConnection.__table__.update()
            .where(GoogleConnection.user_id == uid)
            .values(google_email="x@gmail.com", refresh_token_enc=cipher.encrypt("1//0g-x"))
        )
    await _other_user_connected(maker, cipher, "x@gmail.com")
    exchange(google, refresh_token="1//0g-new")
    resp = await sign_in(anon, nonce="another-nonce-0123456789", code="4/0A-two")
    assert resp.status_code == 200 and resp.json()["gcal_connected"] is True
    assert google.revoked == []


# ---------------------------------------------------------------- complete: rejections


async def test_nonce_mismatch_is_state_invalid_without_calling_google(
    anon: httpx.AsyncClient, google: FakeGoogle, maker: async_sessionmaker[AsyncSession]
) -> None:
    body = await authorize(anon)
    resp = await complete(anon, body | {"nonce": "some-other-nonce-0123456789"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "gcal_state_invalid"
    assert google.requests == [] and await count(maker, Identity) == 0


async def test_connect_state_at_signin_is_state_invalid(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, google: FakeGoogle
) -> None:
    resp = await client.get("/gcal/connect")
    state = query(resp.json()["authorization_url"])["state"]
    resp = await complete(anon, {"auth_code": "4/0A-x", "state": state, "nonce": NONCE})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "gcal_state_invalid"
    assert google.requests == []


async def test_login_state_at_gcal_complete_is_state_invalid(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, google: FakeGoogle
) -> None:
    body = await authorize(anon)
    resp = await client.post(
        "/gcal/complete", json={"auth_code": body["auth_code"], "state": body["state"]}
    )
    assert resp.status_code == 400
    assert_conforms("completeGcalConnect", 400, resp.json())
    assert resp.json()["error"]["code"] == "gcal_state_invalid"
    assert google.requests == []


async def test_expired_and_tampered_states_are_state_invalid(
    anon: httpx.AsyncClient, google: FakeGoogle, clock: SettableClock
) -> None:
    body = await authorize(anon)
    tampered = body["state"][:-6] + ("A" if body["state"][-6] != "A" else "B") + body["state"][-5:]
    for state in (tampered, "garbage"):
        resp = await complete(anon, body | {"state": state})
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "gcal_state_invalid"
    clock.current += timedelta(minutes=10)
    resp = await complete(anon, body)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "gcal_state_invalid"
    assert google.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"auth_code": "c", "state": "s"},
        {"auth_code": "c", "state": "s", "nonce": "short"},
        {"auth_code": "", "state": "s", "nonce": NONCE},
        {"auth_code": "c", "state": "s", "nonce": NONCE, "extra": True},
    ],
)
async def test_complete_body_validation(anon: httpx.AsyncClient, body: dict[str, Any]) -> None:
    resp = await anon.post("/auth/google/complete", json=body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_failed"
    assert_conforms("completeGoogleSignIn", 422, resp.json())


@pytest.mark.parametrize(
    "reply",
    [
        Reply(400, {"error": "invalid_grant"}),
        Reply(500, None),
        Reply(200, "not-an-object"),
        Reply(raise_exc=httpx.ReadTimeout("t")),
    ],
)
async def test_exchange_failure_is_502_and_creates_nothing(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    reply: Reply,
) -> None:
    body = await authorize(anon)
    google.exchange = reply
    resp = await complete(anon, body)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "gcal_exchange_failed"
    assert await count(maker, Identity) == 0 and await count(maker, UserSession) == 0


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"sub": None},
        {"sub": ""},
        {"email_verified": False},
        {"iss": "https://evil.example"},
    ],
)
async def test_unusable_id_token_is_502_and_grant_left_alone(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    claims: dict[str, Any],
) -> None:
    from tests.gcal.fakes import DROP

    exchange(google)
    google.exchange.json["id_token"] = id_token(  # type: ignore[union-attr]
        google.now, **{k: (DROP if v is None else v) for k, v in claims.items()}
    )
    resp = await sign_in(anon)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "gcal_exchange_failed"
    assert await count(maker, User) == 1 and await count(maker, GoogleConnection) == 0
    assert google.revoked == []  # account unknown: revoking could kill a stored connection


# ---------------------------------------------------------------- lock / transaction discipline

POOL_TIMEOUT = 2.0


@pytest.fixture
async def one_connection(schema: str, engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    assert DATABASE_URL
    tiny = create_async_engine(
        DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    try:
        yield tiny
    finally:
        await tiny.dispose()


async def test_google_is_never_called_under_the_lock_or_inside_a_transaction(
    app: FastAPI,
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    gcal_rt: GcalRuntime,
    gsettings: GcalSettings,
    auth_rt: AuthRuntime,
    maker: async_sessionmaker[AsyncSession],
    one_connection: AsyncEngine,
) -> None:
    """Exactly ONE pooled connection with a short checkout timeout serves the sign-in. Every
    Google call (the exchange and the after-commit revoke) checks out that connection itself
    and tries the user's advisory lock: a request holding a session or the lock across Google
    would make that probe time out (or the lock attempt fail)."""
    tiny = async_sessionmaker(one_connection, expire_on_commit=False)
    app.state.sessionmaker = tiny
    auth_rt.repository_provider = sqlalchemy_repository_provider(tiny)

    # First sign-in without calendar: creates the identity (known user id below), no row.
    exchange(google, scope=NO_CALENDAR)
    uid = uuid.UUID((await sign_in(anon)).json()["user"]["id"])
    probes: list[str] = []

    async def probe(label: str) -> None:
        async with tiny() as s, s.begin():
            got = await s.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(CAST(:u AS uuid)::text))"),
                {"u": str(uid)},
            )
            assert got.scalar_one() is True, f"user lock held during google {label}"
        probes.append(label)

    async def handler(request: httpx.Request) -> httpx.Response:
        label = "revoke" if str(request.url) == REVOKE_URL else "exchange"
        # A held pooled connection makes this checkout time out (POOL_TIMEOUT) → 500.
        await asyncio.wait_for(probe(label), 5)
        return google.handler(request)

    gcal_rt.client = HttpGoogleOAuthClient(gsettings, transport=httpx.MockTransport(handler))

    async def flow() -> None:
        # Returning user, calendar unticked again: exchange before the tx, revoke after it.
        exchange(google, scope=NO_CALENDAR)
        body = await authorize(anon, "nonce-pool-a-0123456789ab", "4/0A-pool-a")
        resp = await complete(anon, body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["user"]["id"] == str(uid)
        assert probes == ["exchange", "revoke"], probes
        # Returning user granting the calendar: the connection is written in the tx, after
        # the exchange.
        exchange(google)
        body = await authorize(anon, "nonce-pool-b-0123456789ab", "4/0A-pool-b")
        resp = await complete(anon, body)
        assert resp.status_code == 200 and resp.json()["gcal_connected"] is True, resp.text
        assert probes == ["exchange", "revoke", "exchange"], probes

    # A held lock or connection must fail the test, never hang the suite.
    await asyncio.wait_for(flow(), 30)


# ---------------------------------------------------------------- Apple unconfigured


async def test_apple_sign_in_is_503_when_unconfigured(
    anon: httpx.AsyncClient, auth_rt: AuthRuntime
) -> None:
    assert auth_rt.apple_enabled is False and isinstance(auth_rt.apple, UnconfiguredAppleClient)
    for body in ({}, {"identity_token": "x", "authorization_code": "y", "nonce": NONCE}):
        resp = await anon.post("/auth/apple", json=body)
        assert resp.status_code == 503, resp.text
        assert_conforms("signInWithApple", 503, resp.json())
        assert resp.json()["error"]["code"] == "apple_unavailable"


async def test_delete_me_google_only_user_revokes_google_and_never_apple(
    anon: httpx.AsyncClient, google: FakeGoogle, maker: async_sessionmaker[AsyncSession]
) -> None:
    exchange(google)
    resp = await sign_in(anon)
    uid = uuid.UUID(resp.json()["user"]["id"])
    deleted = await anon.delete("/me", headers=bearer(resp))
    assert deleted.status_code == 204, deleted.text
    assert google.revoked == [REFRESH_TOKEN]
    assert await rows(maker, User, id=uid) == []
    assert await rows(maker, Identity, user_id=uid) == []
    assert await rows(maker, GoogleConnection, user_id=uid) == []


async def test_delete_me_with_apple_identity_but_apple_unconfigured_fails_closed(
    anon: httpx.AsyncClient,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
    auth_rt: AuthRuntime,
    clock: SettableClock,
) -> None:
    exchange(google, scope=NO_CALENDAR, refresh_token=None)
    resp = await sign_in(anon)
    uid = uuid.UUID(resp.json()["user"]["id"])
    async with maker() as s, s.begin():
        await s.execute(
            insert(Identity).values(
                provider="apple",
                subject="apple-sub-1",
                user_id=uid,
                apple_refresh_token_enc=auth_rt.cipher.encrypt("apple-rt"),
                created_at=clock.current,
            )
        )
    deleted = await anon.delete("/me", headers=bearer(resp))
    assert deleted.status_code == 503, deleted.text
    assert_conforms("deleteMe", 503, deleted.json())
    assert deleted.json()["error"]["code"] == "apple_unavailable"
    assert len(await rows(maker, User, id=uid)) == 1
    assert len(await rows(maker, Identity, user_id=uid)) == 2


# ---------------------------------------------------------------- X-Timezone tolerated


@pytest.mark.parametrize("tz", ["America/Los_Angeles", "Mars/Olympus", "not a zone"])
async def test_signin_routes_ignore_x_timezone(
    anon: httpx.AsyncClient, google: FakeGoogle, tz: str
) -> None:
    """The app sends X-Timezone on every request; the sign-in routes neither require nor
    validate it (D26), so any value is ignored."""
    headers = {"X-Timezone": tz}
    resp = await anon.post(
        "/auth/google/start", json={"nonce_hash": nonce_hash(NONCE)}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    state = query(resp.json()["authorization_url"])["state"]
    q = query(await callback(anon, state=state, code="4/0A-tz"))
    exchange(google)
    resp = await anon.post(
        "/auth/google/complete",
        json={"auth_code": q["auth_code"], "state": q["state"], "nonce": NONCE},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert_conforms("completeGoogleSignIn", 200, resp.json())
