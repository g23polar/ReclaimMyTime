from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.auth.apple import (
    AppleCodeRejectedError,
    AppleUnavailableError,
    AppleUnreachableError,
    HttpAppleClient,
    HttpJwksFetcher,
    JwksCache,
)
from app.auth.settings import AuthSettings
from tests.auth.fixtures import FrozenClock, SigningKey


@pytest.fixture
def apple_settings(settings: AuthSettings) -> AuthSettings:
    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    from dataclasses import replace

    return replace(
        settings, apple_team_id="TEAM123", apple_key_id="KEY123", apple_private_key_pem=pem
    )


def _client(
    settings: AuthSettings, clock: FrozenClock, handler, seen: list[httpx.Request]
) -> HttpAppleClient:
    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return HttpAppleClient(settings, clock, transport=httpx.MockTransport(record))


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


async def test_exchange_code_posts_signed_client_secret(
    apple_settings: AuthSettings, clock: FrozenClock
) -> None:
    seen: list[httpx.Request] = []
    client = _client(
        apple_settings,
        clock,
        lambda r: httpx.Response(200, json={"refresh_token": "rt-1", "id_token": "x"}),
        seen,
    )
    result = await client.exchange_code("the-code")
    assert result.refresh_token == "rt-1" and result.id_token == "x"
    form = _form(seen[0])
    assert str(seen[0].url) == apple_settings.apple_token_url
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["client_id"] == apple_settings.apple_bundle_id
    header = jwt.get_unverified_header(form["client_secret"])
    assert header["alg"] == "ES256" and header["kid"] == "KEY123"
    claims = jwt.decode(form["client_secret"], options={"verify_signature": False})
    assert claims["iss"] == "TEAM123"
    assert claims["sub"] == apple_settings.apple_bundle_id
    assert claims["aud"] == "https://appleid.apple.com"


async def test_exchange_code_rejected(apple_settings: AuthSettings, clock: FrozenClock) -> None:
    client = _client(
        apple_settings, clock, lambda r: httpx.Response(400, json={"error": "invalid_grant"}), []
    )
    with pytest.raises(AppleCodeRejectedError):
        await client.exchange_code("bad")


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_exchange_code_unreachable(
    apple_settings: AuthSettings, clock: FrozenClock, exc: type[httpx.HTTPError]
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise exc("down")

    client = _client(apple_settings, clock, fail, [])
    with pytest.raises(AppleUnreachableError) as info:
        await client.exchange_code("c")
    assert info.value.code == "apple_unavailable" and info.value.status_code == 503


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError])
async def test_exchange_code_failure_after_send_is_not_unreachable(
    apple_settings: AuthSettings, clock: FrozenClock, exc: type[httpx.HTTPError]
) -> None:
    # The request may have reached Apple (and consumed the code): not safe to release.
    def fail(request: httpx.Request) -> httpx.Response:
        raise exc("lost")

    client = _client(apple_settings, clock, fail, [])
    with pytest.raises(AppleUnavailableError) as info:
        await client.exchange_code("c")
    assert not isinstance(info.value, AppleUnreachableError)


async def test_exchange_code_without_id_token(
    apple_settings: AuthSettings, clock: FrozenClock
) -> None:
    client = _client(apple_settings, clock, lambda r: httpx.Response(200, json={}), [])
    result = await client.exchange_code("c")
    assert result.refresh_token is None and result.id_token is None


async def test_revoke(apple_settings: AuthSettings, clock: FrozenClock) -> None:
    seen: list[httpx.Request] = []
    client = _client(apple_settings, clock, lambda r: httpx.Response(200), seen)
    await client.revoke_refresh_token("rt-1")
    form = _form(seen[0])
    assert str(seen[0].url) == apple_settings.apple_revoke_url
    assert form["token"] == "rt-1" and form["token_type_hint"] == "refresh_token"


async def test_revoke_already_invalid_is_ok(
    apple_settings: AuthSettings, clock: FrozenClock
) -> None:
    client = _client(
        apple_settings, clock, lambda r: httpx.Response(400, json={"error": "invalid_grant"}), []
    )
    await client.revoke_refresh_token("rt-1")


async def test_revoke_server_error_raises(apple_settings: AuthSettings, clock: FrozenClock) -> None:
    client = _client(apple_settings, clock, lambda r: httpx.Response(503), [])
    with pytest.raises(AppleUnavailableError):
        await client.revoke_refresh_token("rt-1")


async def test_http_jwks_fetcher_feeds_cache(signing_key: SigningKey, clock: FrozenClock) -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json={"keys": [signing_key.public_jwk(), {"kty": "EC", "kid": "ec"}]}
        )
    )
    fetcher = HttpJwksFetcher("https://appleid.apple.com/auth/keys", transport=transport)
    cache = JwksCache(fetcher, clock, ttl=timedelta(hours=1), min_refresh_interval=timedelta(0))
    assert await cache.get_key(signing_key.kid) is not None
    assert await cache.get_key("ec") is None


# ----------------------------------------------------------------- tolerant error parsing

_ODD_BODIES = {
    "null": httpx.Response(400, content=b"null", headers={"content-type": "application/json"}),
    "list": httpx.Response(400, json=["invalid_grant"]),
    "string": httpx.Response(400, json="invalid_grant"),
    "number": httpx.Response(400, json=7),
    "non_json": httpx.Response(400, content=b"<html>bad request</html>"),
    "bad_utf8": httpx.Response(400, content=b"\xff\xfe\xfa"),
    "error_not_str": httpx.Response(400, json={"error": ["invalid_grant"]}),
}


@pytest.mark.parametrize("name", list(_ODD_BODIES))
async def test_exchange_code_odd_400_body_is_code_rejected(
    name: str, apple_settings: AuthSettings, clock: FrozenClock
) -> None:
    client = _client(apple_settings, clock, lambda r: _ODD_BODIES[name], [])
    with pytest.raises(AppleCodeRejectedError) as info:
        await client.exchange_code("bad")
    assert info.value.code == "apple_token_invalid"


@pytest.mark.parametrize("name", list(_ODD_BODIES))
async def test_revoke_odd_400_body_is_unavailable(
    name: str, apple_settings: AuthSettings, clock: FrozenClock
) -> None:
    client = _client(apple_settings, clock, lambda r: _ODD_BODIES[name], [])
    with pytest.raises(AppleUnavailableError) as info:
        await client.revoke_refresh_token("rt-1")
    assert info.value.code == "apple_unavailable"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"null", None),
        (b"[1]", None),
        (b'"x"', None),
        (b"not json", None),
        (b'{"error": 5}', None),
        (b'{"error": "invalid_grant"}', "invalid_grant"),
    ],
)
def test_error_code_parsing(body: bytes, expected: str | None) -> None:
    from app.auth.apple import _error_code

    assert _error_code(httpx.Response(400, content=body)) == expected
