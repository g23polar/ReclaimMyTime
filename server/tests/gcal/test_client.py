"""HttpGoogleOAuthClient / HttpGoogleRevoker against recorded responses (MockTransport)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.gcal.client import (
    GoogleRejectedError,
    GoogleRevocationError,
    GoogleUnavailableError,
    HttpGoogleOAuthClient,
    HttpGoogleRevoker,
)
from tests.gcal.fakes import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    REFRESH_TOKEN,
    REVOKE_URL,
    TOKEN_URL,
    FakeGoogle,
    Reply,
    settings,
)

NOW = datetime(2026, 11, 15, 2, 40, tzinfo=UTC)


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle(now=NOW)


@pytest.fixture
def client(google: FakeGoogle) -> HttpGoogleOAuthClient:
    return HttpGoogleOAuthClient(settings(), transport=google.transport())


async def test_exchange_posts_pkce_verifier_and_client_credentials(
    google: FakeGoogle, client: HttpGoogleOAuthClient
) -> None:
    tokens = await client.exchange_code("4/0Aean-code", "v" * 64)
    assert google.forms(TOKEN_URL) == [
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": "4/0Aean-code",
            "code_verifier": "v" * 64,
            "redirect_uri": REDIRECT_URI,
        }
    ]
    assert tokens.refresh_token == REFRESH_TOKEN and tokens.access_token == ACCESS_TOKEN
    assert "https://www.googleapis.com/auth/calendar.freebusy" in tokens.scopes
    assert REFRESH_TOKEN not in repr(tokens) and ACCESS_TOKEN not in repr(tokens)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply(400, {"error": "invalid_grant", "error_description": "Bad code"}), "invalid_grant"),
        (Reply(401, {"error": "invalid_client"}), "invalid_client"),
        (Reply(400, {"error": "<script>"}), None),  # not a whitelisted OAuth code
        (Reply(400, "not an object"), None),
    ],
)
async def test_exchange_4xx_is_rejected(
    google: FakeGoogle, client: HttpGoogleOAuthClient, reply: Reply, error: str | None
) -> None:
    google.exchange = reply
    with pytest.raises(GoogleRejectedError) as info:
        await client.exchange_code("c", "v" * 64)
    assert info.value.error == error


@pytest.mark.parametrize(
    "reply",
    [
        Reply(500, {"error": "internal"}),
        Reply(503, None),
        Reply(200, ["not", "a", "dict"]),
        Reply(raise_exc=httpx.ConnectTimeout("t")),
        Reply(raise_exc=httpx.ReadTimeout("t")),
    ],
)
async def test_exchange_5xx_network_or_garbage_is_unavailable(
    google: FakeGoogle, client: HttpGoogleOAuthClient, reply: Reply
) -> None:
    google.exchange = reply
    with pytest.raises(GoogleUnavailableError):
        await client.exchange_code("c", "v" * 64)


async def test_refresh_mints_in_memory_access_token(
    google: FakeGoogle, client: HttpGoogleOAuthClient
) -> None:
    token = await client.refresh(REFRESH_TOKEN, NOW)
    assert token.token == ACCESS_TOKEN
    assert token.expires_at == NOW + timedelta(seconds=3599)
    assert ACCESS_TOKEN not in repr(token)
    assert google.forms(TOKEN_URL)[0] == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
    }


async def test_refresh_invalid_grant_and_missing_fields(
    google: FakeGoogle, client: HttpGoogleOAuthClient
) -> None:
    google.refresh = Reply(400, {"error": "invalid_grant", "error_description": "revoked"})
    with pytest.raises(GoogleRejectedError) as info:
        await client.refresh(REFRESH_TOKEN, NOW)
    assert info.value.error == "invalid_grant"
    google.refresh = Reply(200, {"access_token": "x"})  # no expires_in
    with pytest.raises(GoogleUnavailableError):
        await client.refresh(REFRESH_TOKEN, NOW)


@pytest.mark.parametrize(
    "reply",
    [
        Reply(200, {}),
        Reply(400, {"error": "invalid_token", "error_description": "Token expired or revoked"}),
        Reply(400, {"error": "invalid_grant"}),
    ],
)
async def test_revoke_success_and_already_revoked(google: FakeGoogle, reply: Reply) -> None:
    google.revoke = reply
    await HttpGoogleRevoker(REVOKE_URL, transport=google.transport()).revoke_token("rt")
    assert google.forms(REVOKE_URL) == [{"token": "rt"}]


@pytest.mark.parametrize(
    "reply",
    [
        Reply(500, None),
        Reply(503, {"error": "backend"}),
        Reply(400, {"error": "invalid_request"}),  # fail closed on an unknown 400
        Reply(400, "html"),
        Reply(401, {"error": "invalid_client"}),
        Reply(raise_exc=httpx.ConnectTimeout("t")),
        Reply(raise_exc=httpx.ReadTimeout("t")),
    ],
)
async def test_revoke_failures(google: FakeGoogle, reply: Reply) -> None:
    google.revoke = reply
    with pytest.raises(GoogleRevocationError):
        await HttpGoogleRevoker(REVOKE_URL, transport=google.transport()).revoke_token("rt")


async def test_no_token_material_or_error_bodies_in_logs(
    google: FakeGoogle, client: HttpGoogleOAuthClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    google.exchange = Reply(400, {"error": "invalid_grant", "error_description": "SECRET-DESC"})
    with pytest.raises(GoogleRejectedError):
        await client.exchange_code("CODE-SECRET", "VERIFIER-SECRET" * 4)
    google.refresh = Reply(500, {"error_description": "SECRET-DESC"})
    with pytest.raises(GoogleUnavailableError):
        await client.refresh(REFRESH_TOKEN, NOW)
    google.revoke = Reply(503, {"error_description": "SECRET-DESC"})
    with pytest.raises(GoogleRevocationError):
        await client.revoke_token(REFRESH_TOKEN)
    for needle in ("CODE-SECRET", "VERIFIER-SECRET", REFRESH_TOKEN, "SECRET-DESC", CLIENT_SECRET):
        assert needle not in caplog.text
