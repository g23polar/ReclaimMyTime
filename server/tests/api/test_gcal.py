"""T4.1 Google OAuth + connection lifecycle on Postgres, with recorded Google HTTP
(MockTransport; no live Google). Covers GET /gcal/connect, GET /gcal/callback,
GET /gcal/status, DELETE /gcal, access-token minting, and DELETE /me's Google leg on the
real repository."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.crypto import TokenCipher
from app.auth.repository import IdentityRecord
from app.db.models import GoogleConnection, User
from app.gcal import repository as gcal_repo
from app.gcal.client import HttpGoogleOAuthClient
from app.gcal.errors import (
    GcalConnectionInactiveError,
    GcalCredentialUndecryptableError,
    GcalGrantRevokedError,
    GcalTransientError,
)
from app.gcal.runtime import GcalRuntime, build_gcal_runtime
from app.gcal.settings import GcalSettings
from app.gcal.state import StateCodec, s256_challenge
from tests.api.conftest import NOW, SettableClock, new_user
from tests.api.contract import SPEC, assert_conforms
from tests.db.conftest import requires_db
from tests.gcal.fakes import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    EMAIL,
    REDIRECT_URI,
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
APP_CB = "reclaimmytime://gcal/callback"
LOCATION_RE = re.compile(
    SPEC["paths"]["/gcal/callback"]["get"]["responses"]["302"]["headers"]["Location"]["schema"][
        "pattern"
    ]
)


@pytest.fixture
def google(clock: SettableClock) -> FakeGoogle:
    return FakeGoogle(now=clock.current)


@pytest.fixture
def gsettings() -> GcalSettings:
    return settings()


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher([(3, Fernet.generate_key().decode()), (1, Fernet.generate_key().decode())])


@pytest.fixture
def runtime(
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


# ---------------------------------------------------------------- helpers


async def connect_url(client: httpx.AsyncClient) -> dict[str, str]:
    resp = await client.get("/gcal/connect")
    assert resp.status_code == 200, resp.text
    assert_conforms("getGcalConnectUrl", 200, resp.json())
    parts = urlsplit(resp.json()["authorization_url"])
    params = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    params["_base"] = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return params


async def callback(client: httpx.AsyncClient, **params: str) -> httpx.Response:
    resp = await client.get("/gcal/callback", params=params, headers={"X-Timezone": ""})
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert LOCATION_RE.match(location), location
    assert resp.headers["cache-control"] == "no-store"
    return resp


def redirect(resp: httpx.Response) -> dict[str, str]:
    """The app's view of the callback redirect: query params, URL-decoded."""
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}


def error_code(resp: httpx.Response) -> str:
    q = redirect(resp)
    assert q["result"] == "error", q
    return q["code"]


async def authorize(client: httpx.AsyncClient, code: str = "4/0Aean-code") -> dict[str, str]:
    """connect → Google consent → callback; returns the POST /gcal/complete body."""
    state = (await connect_url(client))["state"]
    q = redirect(await callback(client, state=state, code=code, scope=f"{FREEBUSY} {APP_CREATED}"))
    assert q["result"] == "authorized", q
    return {"auth_code": q["auth_code"], "state": q["state"]}


async def complete(client: httpx.AsyncClient, body: Any) -> httpx.Response:
    resp = await client.post("/gcal/complete", json=body)
    if resp.status_code != 422 or resp.json()["error"]["code"] != "validation_failed":
        assert_conforms("completeGcalConnect", resp.status_code, resp.json())
    return resp


async def connect(client: httpx.AsyncClient, code: str = "4/0Aean-code") -> str:
    """Full flow; "connected" or the JSON error code from POST /gcal/complete."""
    resp = await complete(client, await authorize(client, code))
    return "connected" if resp.status_code == 200 else resp.json()["error"]["code"]


async def row(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> GoogleConnection | None:
    async with maker() as s:
        return (
            await s.execute(select(GoogleConnection).where(GoogleConnection.user_id == uid))
        ).scalar_one_or_none()


async def set_row(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, **values: Any) -> None:
    async with maker() as s, s.begin():
        await s.execute(
            update(GoogleConnection).where(GoogleConnection.user_id == uid).values(**values)
        )


# ---------------------------------------------------------------- GET /gcal/connect


async def test_connect_url_carries_narrow_scopes_pkce_offline_consent_and_state(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    gsettings: GcalSettings,
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    p = await connect_url(client)
    assert p.pop("_base") == gsettings.auth_url
    state = runtime.state.open(p.pop("state"), clock.current)
    challenge = p.pop("code_challenge")
    assert p == {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": f"{FREEBUSY} {APP_CREATED} openid email",
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge_method": "S256",
    }  # and no include_granted_scopes
    assert state.user_id == user_id
    assert challenge == s256_challenge(state.code_verifier)
    assert state.expires_at == int((clock.current + timedelta(minutes=10)).timestamp())


# ---------------------------------------------------------------- GET /gcal/callback


async def test_callback_hands_code_and_state_back_without_exchanging(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    state = (await connect_url(client))["state"]
    code = "4/0Aean S0+a/Example=&code"  # needs encoding
    resp = await callback(client, state=state, code=code)
    location = resp.headers["location"]
    assert location.startswith(f"{APP_CB}?result=authorized&auth_code=4%2F0Aean%20S0%2Ba%2F")
    assert redirect(resp) == {"result": "authorized", "auth_code": code, "state": state}
    assert google.requests == []  # nothing exchanged
    assert await row(maker, user_id) is None  # nothing stored


async def test_callback_google_errors(
    client: httpx.AsyncClient, runtime: GcalRuntime, google: FakeGoogle
) -> None:
    state = (await connect_url(client))["state"]
    resp = await callback(client, state=state, error="access_denied")
    assert resp.headers["location"] == f"{APP_CB}?result=error&code=gcal_access_denied"
    for other in ("server_error", "invalid_scope", "temporarily_unavailable", ""):
        assert error_code(await callback(client, state=state, error=other)) == (
            "gcal_exchange_failed"
        ), other
    assert error_code(await callback(client, state="garbage", error="access_denied")) == (
        "gcal_access_denied"
    )
    assert error_code(await callback(client, state=state)) == "gcal_exchange_failed"  # no code
    assert google.requests == []


async def test_callback_invalid_states(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    clock: SettableClock,
    user_id: uuid.UUID,
) -> None:
    good = (await connect_url(client))["state"]
    tampered = good[:30] + ("A" if good[30] != "A" else "B") + good[31:]
    foreign, _ = StateCodec(Fernet.generate_key().decode(), timedelta(minutes=10)).issue(
        user_id, clock.current
    )
    for params in (
        {"code": "c"},  # no state at all
        {"state": "", "code": "c"},
        {"state": tampered, "code": "c"},
        {"state": foreign, "code": "c"},
        {"state": "not-even-base64!", "code": "c"},
    ):
        assert error_code(await callback(client, **params)) == "gcal_state_invalid", params
    clock.current += timedelta(minutes=10)
    assert error_code(await callback(client, state=good, code="c")) == "gcal_state_invalid"
    assert google.requests == []  # nothing reached Google


# ---------------------------------------------------------------- POST /gcal/complete


async def test_successful_connect_stores_encrypted_refresh_token(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    p = await connect_url(client)
    q = redirect(await callback(client, state=p["state"], code="4/0Aean-code"))
    resp = await complete(client, {"auth_code": q["auth_code"], "state": q["state"]})
    assert resp.status_code == 200
    conn = resp.json()["connection"]
    assert (conn["google_email"], conn["status"], conn["revoked_at"]) == (EMAIL, "active", None)

    (form,) = google.forms(TOKEN_URL)
    assert form["code"] == "4/0Aean-code" and form["redirect_uri"] == REDIRECT_URI
    assert s256_challenge(form["code_verifier"]) == p["code_challenge"]

    stored = await row(maker, user_id)
    assert stored is not None
    assert REFRESH_TOKEN.encode() not in stored.refresh_token_enc
    assert cipher.decrypt(stored.refresh_token_enc) == REFRESH_TOKEN
    assert stored.key_id == cipher.primary_key_id == 3
    assert stored.google_email == EMAIL
    assert stored.scopes == sorted(
        [APP_CREATED, FREEBUSY, "openid", "https://www.googleapis.com/auth/userinfo.email"]
    )
    assert conn["scopes"] == stored.scopes
    assert (stored.status, stored.revoked_at, stored.app_calendar_id) == ("active", None, None)
    assert google.revoked == []


async def test_status_reports_contract_shape(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    resp = await client.get("/gcal/status")
    assert resp.json() == {"connection": None}
    assert_conforms("getGcalStatus", 200, resp.json())

    assert await connect(client) == "connected"
    body = (await client.get("/gcal/status")).json()
    assert_conforms("getGcalStatus", 200, body)
    conn = body["connection"]
    assert conn["google_email"] == EMAIL and conn["status"] == "active"
    assert conn["revoked_at"] is None and conn["app_calendar_id"] is None
    assert conn["created_at"].endswith("Z")
    assert set(conn) == {
        "google_email",
        "scopes",
        "app_calendar_id",
        "status",
        "revoked_at",
        "created_at",
    }


async def test_complete_rejects_a_state_issued_to_another_user(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    who: Any,
) -> None:
    """The forwarded-connect-URL attack (D24): the attacker's state, the victim's bearer
    (or vice versa) must never connect anything."""
    attacker_body = await authorize(client)  # state bound to user_id
    victim = await new_user(maker)
    who.user_id = victim
    resp = await complete(client, attacker_body)
    assert (resp.status_code, resp.json()["error"]["code"]) == (400, "gcal_state_invalid")
    assert google.requests == []  # the code was never exchanged
    assert await row(maker, victim) is None and await row(maker, user_id) is None


async def test_complete_invalid_states_are_one_indistinguishable_400(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    clock: SettableClock,
    user_id: uuid.UUID,
) -> None:
    body = await authorize(client)
    good = body["state"]
    tampered = good[:30] + ("A" if good[30] != "A" else "B") + good[31:]
    foreign, _ = StateCodec(Fernet.generate_key().decode(), timedelta(minutes=10)).issue(
        user_id, clock.current
    )
    bodies = []
    for state in (tampered, foreign, "garbage"):
        resp = await complete(client, {"auth_code": "c", "state": state})
        assert resp.status_code == 400
        bodies.append(resp.json())
    clock.current += timedelta(minutes=10)
    resp = await complete(client, body)
    assert resp.status_code == 400
    bodies.append(resp.json())
    assert all(b == bodies[0] for b in bodies)  # no oracle
    assert bodies[0]["error"]["code"] == "gcal_state_invalid"
    assert google.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"auth_code": "c"},
        {"state": "s"},
        {"auth_code": "", "state": "s"},
        {"auth_code": "c", "state": "s", "extra": 1},
        ["not", "an", "object"],
    ],
)
async def test_complete_body_validation(
    client: httpx.AsyncClient, runtime: GcalRuntime, body: Any
) -> None:
    resp = await client.post("/gcal/complete", json=body)
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "validation_failed")


@pytest.mark.parametrize(
    "reply",
    [
        Reply(400, {"error": "invalid_grant", "error_description": "Malformed auth code."}),
        Reply(500, {"error": "internal_failure"}),
        Reply(raise_exc=httpx.ConnectTimeout("t")),
    ],
)
async def test_token_endpoint_failures_are_502(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    reply: Reply,
) -> None:
    google.exchange = reply
    resp = await complete(client, await authorize(client))
    assert (resp.status_code, resp.json()["error"]["code"]) == (502, "gcal_exchange_failed")
    assert "Malformed" not in resp.text and "internal_failure" not in resp.text
    assert await row(maker, user_id) is None


async def test_missing_refresh_token_is_exchange_failed_and_grant_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    google.refresh_token = None
    assert await connect(client) == "gcal_exchange_failed"
    assert google.revoked == [ACCESS_TOKEN]
    assert await row(maker, user_id) is None


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else.apps.googleusercontent.com"},
        {"iss": "https://accounts.evil.example"},
        {"email_verified": False},
    ],
)
async def test_bad_id_token_is_exchange_failed(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
    claims: dict[str, Any],
) -> None:
    body = google.exchange_ok() | {"id_token": id_token(clock.current, **claims)}
    google.exchange = Reply(200, body)
    assert await connect(client) == "gcal_exchange_failed"
    # Account unknown (unverifiable id_token): another user may hold a connection to it and
    # revocation is grant-wide, so the grant is left alone (T6.0 review; was: revoked).
    assert google.revoked == []
    assert await row(maker, user_id) is None


@pytest.mark.parametrize(
    "scope",
    [
        "openid https://www.googleapis.com/auth/userinfo.email",
        f"openid email {FREEBUSY}",
        f"openid email {APP_CREATED}",
        "",
    ],
)
async def test_partial_grant_is_scope_insufficient_and_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    scope: str,
) -> None:
    google.scope = scope
    resp = await complete(client, await authorize(client))
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "gcal_scope_insufficient")
    assert google.revoked == [REFRESH_TOKEN]
    assert await row(maker, user_id) is None


async def test_scope_insufficient_even_if_best_effort_revoke_fails(
    client: httpx.AsyncClient, runtime: GcalRuntime, google: FakeGoogle
) -> None:
    google.scope = "openid email"
    google.revoke = Reply(raise_exc=httpx.ConnectTimeout("t"))
    assert await connect(client) == "gcal_scope_insufficient"


async def test_flow_never_leaks_tokens_or_google_bodies(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    body = await authorize(client, code="CODE-SECRET")
    google.exchange = Reply(400, {"error": "invalid_grant", "error_description": "SECRET-BODY"})
    bad = await complete(client, body)
    google.exchange = None
    good = await complete(client, body | {"auth_code": "CODE-SECRET-2"})
    assert good.status_code == 200
    # Server-side loggers only: the test client's own httpx line logs the URL it requested.
    server_logs = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert "token endpoint rejected" in server_logs  # the server did log the failure
    text = server_logs + bad.text + good.text
    from app.log_redaction import CallbackQueryRedactor

    access = _access_record(f"/gcal/callback?state={body['state']}&code=CODE-SECRET")
    CallbackQueryRedactor().filter(access)
    text += access.getMessage()
    for needle in (
        "CODE-SECRET",
        "SECRET-BODY",
        REFRESH_TOKEN,
        ACCESS_TOKEN,
        CLIENT_SECRET,
        body["state"],
    ):
        assert needle not in text


# ---------------------------------------------------------------- failed reconnect: discard rule


async def test_failed_same_account_reconnect_does_not_revoke_the_stored_grant(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    assert await connect(client) == "connected"
    google.refresh_token = "1//0gREFRESH-token-0002"
    google.scope = "openid email"  # same verified email, missing calendar scopes
    assert await connect(client) == "gcal_scope_insufficient"
    assert google.revoked == []  # grant-wide revoke would kill the kept connection
    stored = await row(maker, user_id)
    assert stored is not None and stored.status == "active"
    assert cipher.decrypt(stored.refresh_token_enc) == REFRESH_TOKEN


async def test_failed_reconnect_with_unknown_email_and_stored_row_skips_revoke(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    assert await connect(client) == "connected"
    body = google.exchange_ok() | {"id_token": id_token(clock.current, aud="not-us")}
    google.exchange = Reply(200, body)
    assert await connect(client) == "gcal_exchange_failed"
    assert google.revoked == []


async def test_failed_reconnect_of_a_different_account_is_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    assert await connect(client) == "connected"
    google.refresh_token = "1//0gOTHER-account-token"
    google.email = "someone.else@gmail.com"
    google.scope = "openid email"
    assert await connect(client) == "gcal_scope_insufficient"
    assert google.revoked == ["1//0gOTHER-account-token"]
    stored = await row(maker, user_id)
    assert stored is not None and (stored.google_email, stored.status) == (EMAIL, "active")


# ---------------------------------------------------------------- reconnect


async def test_reconnect_same_email_keeps_app_calendar_and_reactivates(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    await connect(client)
    first = await row(maker, user_id)
    assert first is not None
    await set_row(
        maker, user_id, app_calendar_id="c_abc@group", status="revoked", revoked_at=clock.current
    )
    google.refresh_token = "1//0gREFRESH-token-0002"
    google.email = EMAIL.upper()  # citext: same account
    clock.current += timedelta(hours=1)
    assert await connect(client) == "connected"
    second = await row(maker, user_id)
    assert second is not None
    assert second.app_calendar_id == "c_abc@group"
    assert (second.status, second.revoked_at) == ("active", None)
    assert cipher.decrypt(second.refresh_token_enc) == "1//0gREFRESH-token-0002"
    assert second.created_at == first.created_at
    assert google.revoked == []  # same account: revoking the old token kills the new grant


async def test_switching_account_clears_app_calendar_and_revokes_old_account(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    await connect(client)
    await set_row(maker, user_id, app_calendar_id="c_abc@group")
    google.email = "someone.else@gmail.com"
    google.refresh_token = "1//0gOTHER-account-token"
    assert await connect(client) == "connected"
    stored = await row(maker, user_id)
    assert stored is not None
    assert (stored.google_email, stored.app_calendar_id) == ("someone.else@gmail.com", None)
    assert cipher.decrypt(stored.refresh_token_enc) == "1//0gOTHER-account-token"
    assert google.revoked == [REFRESH_TOKEN]  # the old account's grant, after commit


async def test_switching_account_succeeds_even_if_old_revoke_fails(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await connect(client)
    google.email = "someone.else@gmail.com"
    google.refresh_token = "1//0gOTHER-account-token"
    google.revoke = Reply(503, None)
    caplog.set_level(logging.WARNING)
    resp = await complete(client, await authorize(client))
    assert resp.status_code == 200
    assert resp.json()["connection"]["google_email"] == "someone.else@gmail.com"
    assert google.forms(REVOKE_URL) == [{"token": REFRESH_TOKEN}]
    assert "replaced google account failed" in caplog.text


async def test_switching_account_with_undecryptable_old_token_still_connects(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    await connect(client)
    await set_row(maker, user_id, refresh_token_enc=b"garbage")
    google.email = "someone.else@gmail.com"
    assert await connect(client) == "connected"
    assert google.revoked == []


# ---------------------------------------------------------------- access tokens


async def test_access_token_minted_on_demand_and_never_stored(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    await connect(client)
    before = await row(maker, user_id)
    token = await runtime.service(maker).access_token(user_id)
    assert token.token == ACCESS_TOKEN
    assert token.expires_at == clock.current + timedelta(seconds=3599)
    assert google.forms(TOKEN_URL)[-1]["refresh_token"] == REFRESH_TOKEN
    after = await row(maker, user_id)
    assert before is not None and after is not None
    assert after.refresh_token_enc == before.refresh_token_enc and after.status == "active"


async def test_invalid_grant_marks_revoked_then_reports_inactive(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    await connect(client)
    clock.current += timedelta(days=2)
    google.refresh = Reply(400, {"error": "invalid_grant", "error_description": "revoked"})
    service = runtime.service(maker)
    with pytest.raises(GcalGrantRevokedError):
        await service.access_token(user_id)
    stored = await row(maker, user_id)
    assert stored is not None
    assert (stored.status, stored.revoked_at) == ("revoked", clock.current)

    calls = len(google.requests)
    with pytest.raises(GcalConnectionInactiveError) as info:
        await service.access_token(user_id)
    assert not isinstance(info.value, GcalGrantRevokedError)
    assert len(google.requests) == calls  # no refresh attempted for a revoked row

    body = (await client.get("/gcal/status")).json()
    assert_conforms("getGcalStatus", 200, body)
    assert body["connection"]["status"] == "revoked"
    assert body["connection"]["revoked_at"] is not None


@pytest.mark.parametrize(
    "reply",
    [
        Reply(500, {"error": "backend"}),
        Reply(401, {"error": "invalid_client"}),
        Reply(400, {"error": "invalid_request"}),
        Reply(raise_exc=httpx.ReadTimeout("t")),
    ],
)
async def test_transient_refresh_failure_leaves_status_alone(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    reply: Reply,
) -> None:
    await connect(client)
    google.refresh = reply
    with pytest.raises(GcalTransientError):
        await runtime.service(maker).access_token(user_id)
    stored = await row(maker, user_id)
    assert stored is not None and (stored.status, stored.revoked_at) == ("active", None)


async def test_access_token_without_connection_or_with_undecryptable_credential(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    service = runtime.service(maker)
    with pytest.raises(GcalConnectionInactiveError):
        await service.access_token(user_id)
    await connect(client)
    await set_row(maker, user_id, refresh_token_enc=b"garbage", key_id=7)
    with pytest.raises(GcalCredentialUndecryptableError) as info:
        await service.access_token(user_id)
    assert info.value.key_id == 7


async def test_mark_revoked_skips_a_row_replaced_by_reconnect(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    await connect(client)
    async with maker() as s, s.begin():
        marked = await gcal_repo.mark_revoked(
            s, user_id, refresh_token_enc=b"an older ciphertext", at=clock.current
        )
    assert marked is False
    stored = await row(maker, user_id)
    assert stored is not None and stored.status == "active"


# ---------------------------------------------------------------- DELETE /gcal


@pytest.mark.parametrize(
    "reply",
    [Reply(200, {}), Reply(400, {"error": "invalid_token", "error_description": "revoked"})],
)
async def test_disconnect_revokes_then_deletes(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    reply: Reply,
) -> None:
    await connect(client)
    google.revoke = reply
    resp = await client.delete("/gcal")
    assert resp.status_code == 204 and resp.content == b""
    assert google.revoked == [REFRESH_TOKEN]
    assert await row(maker, user_id) is None
    assert (await client.get("/gcal/status")).json() == {"connection": None}


@pytest.mark.parametrize(
    "reply",
    [
        Reply(500, None),
        Reply(503, {"error": "backend", "error_description": "SECRET-BODY"}),
        Reply(400, {"error": "invalid_request"}),
        Reply(raise_exc=httpx.ReadTimeout("t")),
    ],
)
async def test_disconnect_fails_closed_when_google_does_not_confirm(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    reply: Reply,
) -> None:
    await connect(client)
    before = await row(maker, user_id)
    google.revoke = reply
    resp = await client.delete("/gcal")
    assert resp.status_code == 503
    body = resp.json()
    assert_conforms("disconnectGcal", 503, body)
    assert body["error"]["code"] == "gcal_revocation_failed"
    assert body["error"]["details"] == {"reason": "google_error"}
    assert "SECRET-BODY" not in resp.text
    after = await row(maker, user_id)
    assert before is not None and after is not None
    assert (after.refresh_token_enc, after.status) == (before.refresh_token_enc, before.status)


async def test_disconnect_undecryptable_credential_is_503_with_key_id(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    await connect(client)
    await set_row(maker, user_id, refresh_token_enc=b"garbage", key_id=9)
    resp = await client.delete("/gcal")
    assert resp.status_code == 503
    assert_conforms("disconnectGcal", 503, resp.json())
    assert resp.json()["error"]["details"] == {"reason": "undecryptable_credential", "key_id": 9}
    assert google.revoked == []
    assert await row(maker, user_id) is not None


async def test_disconnect_without_connection_is_404(
    client: httpx.AsyncClient, runtime: GcalRuntime, google: FakeGoogle
) -> None:
    resp = await client.delete("/gcal")
    assert resp.status_code == 404
    assert_conforms("disconnectGcal", 404, resp.json())
    assert resp.json()["error"]["code"] == "gcal_not_connected"
    assert google.requests == []


async def test_disconnect_revokes_a_credential_replaced_mid_revocation(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    await connect(client)
    inner = runtime.client
    raced = False

    class Racing:
        async def revoke_token(self, token: str) -> None:
            nonlocal raced
            await inner.revoke_token(token)
            if not raced:  # a reconnect lands while Google is being called
                raced = True
                await set_row(maker, user_id, refresh_token_enc=cipher.encrypt("rt-new"))

        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

    runtime.client = Racing()  # type: ignore[assignment]
    assert (await client.delete("/gcal")).status_code == 204
    assert google.revoked == [REFRESH_TOKEN, "rt-new"]
    assert await row(maker, user_id) is None


# ---------------------------------------------------------------- DELETE /me, real repository


class _Apple:
    def __init__(self, events: list[str], fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def exchange_code(self, code: str) -> Any:
        raise NotImplementedError

    async def revoke_refresh_token(self, token: str) -> None:
        from app.auth.errors import AppleUnavailableError

        self.events.append(f"apple:{token}")
        if self.fail:
            raise AppleUnavailableError()


async def _deletion(
    user_id: uuid.UUID,
    maker: async_sessionmaker[AsyncSession],
    cipher: TokenCipher,
    google: FakeGoogle,
    apple: _Apple,
    clock: SettableClock,
) -> Any:
    from app.auth.dependencies import sqlalchemy_repository_provider
    from app.auth.service import AccountDeletionService
    from app.gcal.client import HttpGoogleRevoker

    provider = sqlalchemy_repository_provider(maker)
    async with provider() as repo:
        await repo.insert_identity(
            IdentityRecord(
                provider="apple",
                subject=f"sub-{uuid.uuid4().hex[:6]}",
                user_id=user_id,
                email=None,
                email_verified=False,
                is_private_email=False,
                apple_refresh_token_enc=cipher.encrypt("apple-rt"),
                created_at=clock.current,
            )
        )
    return AccountDeletionService(
        provider,
        apple=apple,
        cipher=cipher,
        google=HttpGoogleRevoker(REVOKE_URL, transport=google.transport()),
        clock=clock,
    )


async def test_delete_me_revokes_google_then_apple_then_cascades(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    await connect(client)
    events: list[str] = []
    deletion = await _deletion(user_id, maker, cipher, google, _Apple(events), clock)
    await deletion.delete_account(user_id)
    assert google.revoked == [REFRESH_TOKEN]
    assert events == ["apple:apple-rt"]
    assert await row(maker, user_id) is None
    async with maker() as s:
        assert (await s.execute(select(User).where(User.id == user_id))).first() is None


async def test_delete_me_google_ok_apple_fails_marks_row_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    from app.auth.errors import AppleUnavailableError

    await connect(client)
    events: list[str] = []
    deletion = await _deletion(user_id, maker, cipher, google, _Apple(events, fail=True), clock)
    clock.current += timedelta(minutes=5)
    with pytest.raises(AppleUnavailableError):
        await deletion.delete_account(user_id)
    assert google.revoked == [REFRESH_TOKEN]
    stored = await row(maker, user_id)
    assert stored is not None and (stored.status, stored.revoked_at) == ("revoked", clock.current)
    async with maker() as s:
        assert (await s.execute(select(User).where(User.id == user_id))).first() is not None


async def test_delete_me_google_fails_touches_nothing(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    from app.gcal.errors import GcalRevocationFailedError

    await connect(client)
    events: list[str] = []
    deletion = await _deletion(user_id, maker, cipher, google, _Apple(events), clock)
    google.revoke = Reply(502, None)
    with pytest.raises(GcalRevocationFailedError) as info:
        await deletion.delete_account(user_id)
    assert info.value.details == {"reason": "google_error"}
    assert events == []  # Apple never called
    stored = await row(maker, user_id)
    assert stored is not None and (stored.status, stored.revoked_at) == ("active", None)


# ---------------------------------------------------------------- cleanup revoke races


async def _store(
    maker: async_sessionmaker[AsyncSession],
    uid: uuid.UUID,
    cipher: TokenCipher,
    *,
    email: str,
    token: str,
    clock: SettableClock,
) -> None:
    """What a concurrent successful POST /gcal/complete commits."""
    async with maker() as s, s.begin():
        await gcal_repo.lock_user(s, uid)
        previous = await gcal_repo.get_connection(s, uid)
        await gcal_repo.upsert_connection(
            s,
            uid,
            previous=previous,
            google_email=email,
            refresh_token_enc=cipher.encrypt(token),
            key_id=cipher.primary_key_id,
            scopes=[APP_CREATED, FREEBUSY],
            now=clock.current,
        )


def _race_before_revoke(runtime: GcalRuntime, hook: Any) -> None:
    """Run `hook` once, just before the next remote revoke reaches Google."""
    inner = runtime.client
    pending = [hook]

    class Racing:
        async def revoke_token(self, token: str) -> None:
            if pending:
                await pending.pop()()
            await inner.revoke_token(token)

        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

    runtime.client = Racing()  # type: ignore[assignment]


async def test_discard_racing_a_same_account_store_marks_the_row_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    google.scope = "openid email"  # this completion fails: scope_insufficient, email known
    body = await authorize(client)

    async def concurrent_store() -> None:  # another completion stores the same account
        await _store(maker, user_id, cipher, email=EMAIL.upper(), token="rt-fresh", clock=clock)

    _race_before_revoke(runtime, concurrent_store)
    resp = await complete(client, body)
    assert resp.json()["error"]["code"] == "gcal_scope_insufficient"
    assert google.revoked == [REFRESH_TOKEN]  # snapshot saw no row
    stored = await row(maker, user_id)
    assert stored is not None
    assert (stored.status, stored.revoked_at) == ("revoked", clock.current)


async def test_old_account_revoke_racing_a_fresh_store_of_that_account_marks_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
) -> None:
    assert await connect(client) == "connected"  # account A
    google.email, google.refresh_token = "someone.else@gmail.com", "rt-B"
    body = await authorize(client)

    async def concurrent_store_of_a() -> None:
        await _store(maker, user_id, cipher, email=EMAIL, token="rt-A-fresh", clock=clock)

    _race_before_revoke(runtime, concurrent_store_of_a)
    resp = await complete(client, body)  # commits B, then revokes A
    assert resp.status_code == 200
    assert google.revoked == [REFRESH_TOKEN]
    stored = await row(maker, user_id)
    assert stored is not None and stored.google_email == EMAIL
    assert (stored.status, stored.revoked_at) == ("revoked", clock.current)


async def test_no_race_no_spurious_marking(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    # Discard of a different account's failed grant: stored row (A) stays active.
    assert await connect(client) == "connected"
    google.email, google.refresh_token, google.scope = "b@gmail.com", "rt-B", "openid email"
    assert await connect(client) == "gcal_scope_insufficient"
    # Account switch: old A revoked after commit; the new B row stays active.
    google.scope = " ".join([FREEBUSY, APP_CREATED, "openid", "email"])
    google.refresh_token = "rt-B2"
    assert await connect(client) == "connected"
    assert google.revoked == ["rt-B", REFRESH_TOKEN]
    stored = await row(maker, user_id)
    assert stored is not None
    assert (stored.google_email, stored.status, stored.revoked_at) == (
        "b@gmail.com",
        "active",
        None,
    )


async def test_reconcile_failure_never_fails_the_request(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("db down")

    google.scope = "openid email"
    monkeypatch.setattr(gcal_repo, "lock_user", broken)
    assert await connect(client) == "gcal_scope_insufficient"
    assert google.revoked == [REFRESH_TOKEN]
    assert "reconcile after google revoke failed: RuntimeError" in caplog.text


# ---------------------------------------------------------------- uvicorn access log


def _access_record(full_path: str) -> logging.LogRecord:
    # uvicorn.protocols: logger.info('%s - "%s %s HTTP/%s" %d', *args)
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:51234", "GET", full_path, "1.1", 302),
        None,
    )


def test_access_log_redacts_callback_query() -> None:
    from app.log_redaction import CallbackQueryRedactor

    redactor = CallbackQueryRedactor()
    code, state = "4/0AeanS0-SECRET-CODE", "gAAAAABsecret-state-token=="
    record = _access_record(f"/gcal/callback?state={state}&code={code}&scope=x")
    assert redactor.filter(record) is True
    message = record.getMessage()
    assert message == '203.0.113.9:51234 - "GET /gcal/callback HTTP/1.1" 302'
    assert code not in message and state not in message
    # root_path prefix is covered; other paths are untouched.
    record = _access_record(f"/api/gcal/callback?code={code}")
    redactor.filter(record)
    assert code not in record.getMessage()
    record = _access_record("/gcal/status?x=1")
    redactor.filter(record)
    assert "/gcal/status?x=1" in record.getMessage()
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "plain", None, None)
    assert redactor.filter(record) is True  # odd shapes pass through


def test_app_installs_redaction_once_on_uvicorn_access() -> None:
    import app.main  # noqa: F401  (install runs at import)
    from app.log_redaction import CallbackQueryRedactor, install_access_log_redaction

    access = logging.getLogger("uvicorn.access")
    first = install_access_log_redaction()
    assert install_access_log_redaction() is first
    assert sum(isinstance(f, CallbackQueryRedactor) for f in access.filters) == 1
    record = _access_record("/gcal/callback?code=SECRET&state=SECRET2")
    assert access.filter(record)  # the logger-level filter rewrites the record
    assert "SECRET" not in record.getMessage()


# ------------------------------------------------------- cross-user same account (T6.0 review)


async def _other_user_holds(
    maker: async_sessionmaker[AsyncSession], cipher: TokenCipher, email: str
) -> uuid.UUID:
    other = await new_user(maker)
    async with maker() as s, s.begin():
        s.add(
            GoogleConnection(
                user_id=other,
                google_email=email,
                refresh_token_enc=cipher.encrypt("1//0g-other-user"),
                key_id=cipher.primary_key_id,
                scopes=[FREEBUSY, APP_CREATED],
                app_calendar_id=None,
                status="active",
                revoked_at=None,
                created_at=NOW,
            )
        )
    return other


async def test_partial_grant_of_an_account_another_user_connected_is_not_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    """User A has Google account G connected; user B (the caller) connects G but unticks a
    calendar scope. Revoking B's partial grant would kill A's connection (grant-wide)."""
    other = await _other_user_holds(maker, cipher, EMAIL.upper())  # citext: case-insensitive
    google.scope = "openid https://www.googleapis.com/auth/userinfo.email"
    assert await connect(client) == "gcal_scope_insufficient"
    assert google.revoked == []
    held = await row(maker, other)
    assert held is not None and held.status == "active"


async def test_missing_refresh_token_for_another_users_account_is_not_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await _other_user_holds(maker, cipher, EMAIL)
    google.refresh_token = None
    assert await connect(client) == "gcal_exchange_failed"
    assert google.revoked == []


async def test_partial_grant_revoked_when_another_user_holds_a_different_account(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await _other_user_holds(maker, cipher, "someone.else@gmail.com")
    google.scope = "openid https://www.googleapis.com/auth/userinfo.email"
    assert await connect(client) == "gcal_scope_insufficient"
    assert google.revoked == [REFRESH_TOKEN]


async def test_replaced_account_still_connected_by_another_user_is_not_revoked(
    client: httpx.AsyncClient,
    runtime: GcalRuntime,
    google: FakeGoogle,
    cipher: TokenCipher,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> None:
    """The caller switches from account X to G while another user still has X connected:
    the old grant is not revoked (it would kill the other user's connection)."""
    await connect(client)  # caller connected EMAIL
    await set_row(
        maker, user_id, google_email="x@gmail.com", refresh_token_enc=cipher.encrypt("1//0g-x")
    )
    await _other_user_holds(maker, cipher, "x@gmail.com")
    assert await connect(client, code="4/0Aean-second") == "connected"
    assert google.revoked == []
