from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from app.auth.apple import AppleUnavailableError
from app.auth.repository import InMemoryAuthRepository
from tests.auth.conftest import T0, TZ_HEADERS
from tests.auth.fixtures import FakeAppleClient, FrozenClock, TokenFactory


async def _sign_in(
    client: httpx.AsyncClient, tokens: TokenFactory, nonce: str, **claims: object
) -> dict:
    response = await client.post(
        "/auth/apple",
        json={
            "identity_token": tokens.make(raw_nonce=nonce, **claims),
            "authorization_code": "auth-code-0001",
            "nonce": nonce,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **TZ_HEADERS}


async def test_sign_in_response_matches_contract(
    client: httpx.AsyncClient, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    body = await _sign_in(client, tokens, new_nonce())
    assert set(body) == {"token", "session", "user", "is_new_user"}
    assert body["is_new_user"] is True
    assert len(body["token"]) >= 32
    assert body["session"]["created_at"] == "2026-11-15T02:40:03Z"
    assert body["session"]["expires_at"] == "2026-12-15T02:40:03Z"
    assert body["user"]["email"] is None
    assert set(body["user"]) == {"id", "email", "created_at"}


async def test_sign_in_invalid_token_envelope(
    client: httpx.AsyncClient, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    response = await client.post(
        "/auth/apple",
        json={
            "identity_token": tokens.make(raw_nonce="stale-nonce-value-01"),
            "authorization_code": "code",
            "nonce": new_nonce(),
        },
    )
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "apple_token_invalid",
            "message": "Apple identity token failed verification.",
            "details": {"reason": "nonce_mismatch"},
        }
    }


async def test_sign_in_validation_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/auth/apple", json={"identity_token": "x", "nonce": "short", "extra": 1}
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    paths = {f["path"] for f in error["details"]["fields"]}
    assert {"authorization_code", "nonce", "extra"} <= paths


async def test_malformed_json_is_bad_request(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/auth/apple", content=b"{nope", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


async def test_get_me(
    client: httpx.AsyncClient, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    signed = await _sign_in(client, tokens, new_nonce())
    response = await client.get("/me", headers=_auth(signed["token"]))
    assert response.status_code == 200
    assert response.json() == signed["user"]


@pytest.mark.parametrize(
    "headers",
    [
        TZ_HEADERS,
        {"Authorization": "Bearer", **TZ_HEADERS},
        {"Authorization": "Basic abc", **TZ_HEADERS},
        {"Authorization": "Bearer garbage-token-value", **TZ_HEADERS},
    ],
    ids=["missing", "empty", "wrong-scheme", "garbage"],
)
async def test_dependency_rejects_bad_bearer(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/me", headers=headers)
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthenticated", "message": "Missing or invalid session token."}
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_dependency_rejects_expired_bearer(
    client: httpx.AsyncClient,
    tokens: TokenFactory,
    clock: FrozenClock,
    new_nonce: Callable[[], str],
) -> None:
    signed = await _sign_in(client, tokens, new_nonce())
    clock.set(T0 + timedelta(days=30))
    response = await client.get("/me", headers=_auth(signed["token"]))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("tz", [None, "Not/AZone", "../etc/passwd"])
async def test_timezone_required(
    client: httpx.AsyncClient,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
    tz: str | None,
) -> None:
    signed = await _sign_in(client, tokens, new_nonce())
    headers = {"Authorization": f"Bearer {signed['token']}"}
    if tz is not None:
        headers["X-Timezone"] = tz
    response = await client.get("/me", headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "timezone_invalid"


async def test_delete_sessions_revokes_all(
    client: httpx.AsyncClient,
    tokens: TokenFactory,
    repo: InMemoryAuthRepository,
    new_nonce: Callable[[], str],
) -> None:
    a = await _sign_in(client, tokens, new_nonce())
    b = await _sign_in(client, tokens, new_nonce())
    assert len(repo.sessions) == 2
    response = await client.delete("/sessions", headers=_auth(a["token"]))
    assert response.status_code == 204
    assert response.content == b""
    assert repo.sessions == {}
    for token in (a["token"], b["token"]):
        assert (await client.get("/me", headers=_auth(token))).status_code == 401
    assert len(repo.users) == 1  # signing out does not delete the account


async def test_delete_sessions_current_revokes_only_that_one(
    client: httpx.AsyncClient, tokens: TokenFactory, new_nonce: Callable[[], str]
) -> None:
    a = await _sign_in(client, tokens, new_nonce())
    b = await _sign_in(client, tokens, new_nonce())
    assert (await client.delete("/sessions/current", headers=_auth(a["token"]))).status_code == 204
    assert (await client.get("/me", headers=_auth(a["token"]))).status_code == 401
    assert (await client.get("/me", headers=_auth(b["token"]))).status_code == 200


async def test_delete_me_revokes_then_deletes(
    client: httpx.AsyncClient,
    tokens: TokenFactory,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    signed = await _sign_in(client, tokens, new_nonce())
    original_delete = repo.delete_user

    async def spy_delete(user_id):  # noqa: ANN001, ANN202
        apple.events.append("delete")
        await original_delete(user_id)

    repo.delete_user = spy_delete  # type: ignore[method-assign]

    response = await client.delete("/me", headers=_auth(signed["token"]))
    assert response.status_code == 204
    assert apple.events == ["revoke", "delete"]
    assert apple.revoked == [apple.refresh_token]
    assert repo.users == {} and repo.identities == {} and repo.sessions == {}
    assert (await client.get("/me", headers=_auth(signed["token"]))).status_code == 401


async def test_delete_me_aborts_when_revoke_fails(
    client: httpx.AsyncClient,
    tokens: TokenFactory,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    new_nonce: Callable[[], str],
) -> None:
    signed = await _sign_in(client, tokens, new_nonce())
    apple.revoke_error = AppleUnavailableError()
    response = await client.delete("/me", headers=_auth(signed["token"]))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "apple_unavailable"
    assert len(repo.users) == 1


async def test_unknown_route_and_method_use_envelope(client: httpx.AsyncClient) -> None:
    not_found = await client.get("/nope")
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "not_found"
    not_allowed = await client.get("/auth/apple")
    assert not_allowed.status_code == 405
    assert not_allowed.json()["error"]["code"] == "method_not_allowed"


async def test_unhandled_error_is_internal_error_without_leak(
    client: httpx.AsyncClient,
    repo: InMemoryAuthRepository,
    tokens: TokenFactory,
    new_nonce: Callable[[], str],
) -> None:
    async def boom(*_: object) -> None:
        raise RuntimeError("db exploded with secret=abc")

    repo.create_session = boom  # type: ignore[method-assign]
    nonce = new_nonce()
    response = await client.post(
        "/auth/apple",
        json={
            "identity_token": tokens.make(raw_nonce=nonce),
            "authorization_code": "c",
            "nonce": nonce,
        },
    )
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred."}
    }


async def test_delete_me_blocked_when_credential_undecryptable(
    client: httpx.AsyncClient,
    tokens: TokenFactory,
    repo: InMemoryAuthRepository,
    apple: FakeAppleClient,
    runtime,
    new_nonce: Callable[[], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    signed = await _sign_in(client, tokens, new_nonce())
    key = ("apple", "001234.apple-subject.0001")
    repo.identities[key] = replace(repo.identities[key], apple_refresh_token_enc=b"garbage")
    with caplog.at_level(logging.DEBUG):
        response = await client.delete("/me", headers=_auth(signed["token"]))
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "apple_revocation_failed",
            "message": response.json()["error"]["message"],
            "details": {
                "reason": "undecryptable_credential",
                "key_id": runtime.cipher.primary_key_id,
            },
        }
    }
    assert apple.revoked == []
    assert len(repo.users) == 1 and len(repo.identities) == 1 and len(repo.sessions) == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "DecryptionError" in errors[0].getMessage()
    assert "garbage" not in caplog.text and "apple-subject" not in caplog.text
