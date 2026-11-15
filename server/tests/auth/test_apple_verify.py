from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.auth.apple import AppleIdentityVerifier, JwksCache, JwksUnavailableError
from app.auth.errors import AppleTokenInvalidError, AppleUnavailableError
from app.auth.settings import AuthSettings
from tests.auth.fixtures import FakeJwksFetcher, FrozenClock, SigningKey, TokenFactory

NONCE = "raw-nonce-verify-0001"


@pytest.fixture
def jwks(jwks_fetcher: FakeJwksFetcher, clock: FrozenClock, settings: AuthSettings) -> JwksCache:
    return JwksCache(
        jwks_fetcher, clock, ttl=settings.jwks_ttl, min_refresh_interval=timedelta(minutes=1)
    )


@pytest.fixture
def verifier(jwks: JwksCache, settings: AuthSettings, clock: FrozenClock) -> AppleIdentityVerifier:
    return AppleIdentityVerifier(jwks, settings, clock)


async def _reason(verifier: AppleIdentityVerifier, token: str, nonce: str = NONCE) -> str:
    with pytest.raises(AppleTokenInvalidError) as info:
        await verifier.verify(token, nonce)
    assert info.value.status_code == 401
    assert info.value.code == "apple_token_invalid"
    return info.value.details["reason"]


async def test_valid_token(verifier: AppleIdentityVerifier, tokens: TokenFactory) -> None:
    claims = await verifier.verify(
        tokens.make(raw_nonce=NONCE, email="a@example.com", is_private_email="false"), NONCE
    )
    assert claims.subject == "001234.apple-subject.0001"
    assert claims.email == "a@example.com"
    assert claims.email_verified is True
    assert claims.is_private_email is False


async def test_aud_list_containing_bundle_id_accepted(
    verifier: AppleIdentityVerifier, tokens: TokenFactory, settings: AuthSettings
) -> None:
    token = tokens.make(raw_nonce=NONCE, aud=["other", settings.apple_bundle_id])
    assert (await verifier.verify(token, NONCE)).subject


async def test_expired(
    verifier: AppleIdentityVerifier, tokens: TokenFactory, clock: FrozenClock
) -> None:
    token = tokens.make(raw_nonce=NONCE, ttl=timedelta(minutes=5))
    clock.advance(timedelta(minutes=5, seconds=31))  # past exp + 30s leeway
    assert await _reason(verifier, token) == "expired"


async def test_within_leeway_accepted(
    verifier: AppleIdentityVerifier, tokens: TokenFactory, clock: FrozenClock
) -> None:
    token = tokens.make(raw_nonce=NONCE, ttl=timedelta(minutes=5))
    clock.advance(timedelta(minutes=5, seconds=10))
    assert (await verifier.verify(token, NONCE)).subject


async def test_wrong_aud(verifier: AppleIdentityVerifier, tokens: TokenFactory) -> None:
    assert await _reason(verifier, tokens.make(raw_nonce=NONCE, aud="com.evil.app")) == "audience"


async def test_wrong_iss(verifier: AppleIdentityVerifier, tokens: TokenFactory) -> None:
    token = tokens.make(raw_nonce=NONCE, iss="https://evil.example.com")
    assert await _reason(verifier, token) == "issuer"


async def test_nonce_mismatch(verifier: AppleIdentityVerifier, tokens: TokenFactory) -> None:
    token = tokens.make(raw_nonce="some-other-raw-nonce")
    assert await _reason(verifier, token) == "nonce_mismatch"


async def test_raw_nonce_in_claim_rejected(
    verifier: AppleIdentityVerifier, tokens: TokenFactory
) -> None:
    # Claim must be SHA-256(raw), not the raw value itself.
    assert await _reason(verifier, tokens.make(raw_nonce=NONCE, nonce=NONCE)) == "nonce_mismatch"


async def test_signed_by_unknown_key(
    verifier: AppleIdentityVerifier, tokens: TokenFactory, signing_key: SigningKey
) -> None:
    forged = SigningKey(kid=signing_key.kid)  # same kid, different key
    assert await _reason(verifier, tokens.make(raw_nonce=NONCE, key=forged)) == "signature"


async def test_hs256_alg_confusion_rejected(
    verifier: AppleIdentityVerifier, tokens: TokenFactory
) -> None:
    import jwt

    token = jwt.encode(
        {"sub": "x"},
        "secret-secret-secret-secret-secret!",
        algorithm="HS256",
        headers={"kid": tokens.key.kid},
    )
    assert await _reason(verifier, token) == "signature"


async def test_garbage_token(verifier: AppleIdentityVerifier) -> None:
    assert await _reason(verifier, "not-a-jwt") == "signature"


async def test_jwks_cached_within_ttl(
    verifier: AppleIdentityVerifier,
    tokens: TokenFactory,
    jwks_fetcher: FakeJwksFetcher,
    clock: FrozenClock,
) -> None:
    for _ in range(3):
        await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)
    assert jwks_fetcher.fetch_count == 1
    clock.advance(timedelta(hours=6, seconds=1))
    await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)
    assert jwks_fetcher.fetch_count == 2


async def test_kid_miss_triggers_refresh(
    verifier: AppleIdentityVerifier,
    tokens: TokenFactory,
    jwks_fetcher: FakeJwksFetcher,
    clock: FrozenClock,
) -> None:
    await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)
    rotated = SigningKey(kid="fixture-kid-2")
    jwks_fetcher.keys.append(rotated)
    clock.advance(timedelta(minutes=2))
    assert (await verifier.verify(tokens.make(raw_nonce=NONCE, key=rotated), NONCE)).subject
    assert jwks_fetcher.fetch_count == 2


async def test_kid_miss_refresh_is_rate_limited(
    verifier: AppleIdentityVerifier, tokens: TokenFactory, jwks_fetcher: FakeJwksFetcher
) -> None:
    await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)
    for i in range(5):
        token = tokens.make(raw_nonce=NONCE, kid=f"unknown-{i}")
        assert await _reason(verifier, token) == "signature"
    assert jwks_fetcher.fetch_count == 1


async def test_jwks_outage_serves_stale_keys(
    verifier: AppleIdentityVerifier,
    tokens: TokenFactory,
    jwks_fetcher: FakeJwksFetcher,
    clock: FrozenClock,
) -> None:
    await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)
    jwks_fetcher.fail = True
    clock.advance(timedelta(hours=7))
    assert (await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)).subject


async def test_jwks_unavailable_cold(
    verifier: AppleIdentityVerifier, tokens: TokenFactory, jwks_fetcher: FakeJwksFetcher
) -> None:
    jwks_fetcher.fail = True
    with pytest.raises(AppleUnavailableError):
        await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)


async def test_relay_domain_marks_private_even_without_flag(
    verifier: AppleIdentityVerifier, tokens: TokenFactory
) -> None:
    token = tokens.make(
        raw_nonce=NONCE, email="abc@privaterelay.appleid.com", is_private_email="false"
    )
    assert (await verifier.verify(token, NONCE)).is_private_email is True


# ----------------------------------------------------------------- refresh backoff + single-flight


@pytest.fixture
def backoff_jwks(jwks_fetcher: FakeJwksFetcher, clock: FrozenClock) -> JwksCache:
    return JwksCache(
        jwks_fetcher,
        clock,
        ttl=timedelta(hours=6),
        min_refresh_interval=timedelta(minutes=1),
        refresh_backoff=timedelta(seconds=60),
    )


async def test_concurrent_callers_during_outage_make_one_provider_call(
    backoff_jwks: JwksCache, jwks_fetcher: FakeJwksFetcher, signing_key: SigningKey
) -> None:
    jwks_fetcher.fail = True
    jwks_fetcher.gate = asyncio.Event()
    calls = [asyncio.create_task(backoff_jwks.get_key(signing_key.kid)) for _ in range(20)]
    await asyncio.sleep(0.01)
    jwks_fetcher.gate.set()
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert jwks_fetcher.fetch_count == 1
    assert all(isinstance(r, JwksUnavailableError) for r in results)


async def test_cold_failure_backs_off_then_retries(
    backoff_jwks: JwksCache,
    jwks_fetcher: FakeJwksFetcher,
    signing_key: SigningKey,
    clock: FrozenClock,
) -> None:
    jwks_fetcher.fail = True
    for _ in range(3):
        with pytest.raises(JwksUnavailableError):
            await backoff_jwks.get_key(signing_key.kid)
    assert jwks_fetcher.fetch_count == 1
    clock.advance(timedelta(seconds=59))
    with pytest.raises(JwksUnavailableError):
        await backoff_jwks.get_key(signing_key.kid)
    assert jwks_fetcher.fetch_count == 1
    jwks_fetcher.fail = False
    clock.advance(timedelta(seconds=1))
    assert await backoff_jwks.get_key(signing_key.kid) is not None
    assert jwks_fetcher.fetch_count == 2


async def test_stale_keys_serve_during_backoff(
    backoff_jwks: JwksCache,
    jwks_fetcher: FakeJwksFetcher,
    signing_key: SigningKey,
    clock: FrozenClock,
) -> None:
    assert await backoff_jwks.get_key(signing_key.kid) is not None
    jwks_fetcher.fail = True
    clock.advance(timedelta(hours=7))  # keys now stale
    for _ in range(5):
        assert await backoff_jwks.get_key(signing_key.kid) is not None
        assert await backoff_jwks.get_key("unknown-kid") is None
    assert jwks_fetcher.fetch_count == 2  # initial success + one failed attempt
    clock.advance(timedelta(seconds=61))
    assert await backoff_jwks.get_key(signing_key.kid) is not None
    assert jwks_fetcher.fetch_count == 3


async def test_cached_reads_not_blocked_by_inflight_fetch(
    backoff_jwks: JwksCache,
    jwks_fetcher: FakeJwksFetcher,
    signing_key: SigningKey,
    clock: FrozenClock,
) -> None:
    assert await backoff_jwks.get_key(signing_key.kid) is not None
    clock.advance(timedelta(minutes=2))
    jwks_fetcher.gate = asyncio.Event()
    miss = asyncio.create_task(backoff_jwks.get_key("rotated-kid"))  # holds a fetch open
    await asyncio.sleep(0.01)
    assert jwks_fetcher.fetch_count == 2
    hit = await asyncio.wait_for(backoff_jwks.get_key(signing_key.kid), timeout=1)
    assert hit is not None
    jwks_fetcher.gate.set()
    assert await miss is None


def test_settings_default_refresh_backoff(settings: AuthSettings) -> None:
    assert settings.jwks_refresh_backoff == timedelta(seconds=60)


# ----------------------------------------------------------------- unusable JWKS = failed refresh

_UNUSABLE_DOCUMENTS = {
    "empty": {},
    "no_keys": {"keys": []},
    "ec_only": {"keys": [{"kty": "EC", "kid": "ec-1", "crv": "P-256", "x": "x", "y": "y"}]},
    "wrong_alg": "wrong_alg",  # built from the fixture key below (alg RS512)
    "not_an_object": ["keys"],
}


def _unusable(name: str, signing_key: SigningKey) -> object:
    if name == "wrong_alg":
        jwk = signing_key.public_jwk() | {"alg": "RS512", "kid": "other-kid"}
        return {"keys": [jwk]}
    return _UNUSABLE_DOCUMENTS[name]


@pytest.mark.parametrize("name", list(_UNUSABLE_DOCUMENTS))
async def test_unusable_jwks_keeps_cache_and_backs_off(
    name: str,
    backoff_jwks: JwksCache,
    jwks_fetcher: FakeJwksFetcher,
    signing_key: SigningKey,
    clock: FrozenClock,
) -> None:
    assert await backoff_jwks.get_key(signing_key.kid) is not None
    jwks_fetcher.document = _unusable(name, signing_key)
    clock.advance(timedelta(hours=7))  # stale: forces a refresh attempt
    assert await backoff_jwks.get_key(signing_key.kid) is not None  # previous key still served
    assert jwks_fetcher.fetch_count == 2
    clock.advance(timedelta(seconds=30))  # inside backoff: no new attempt
    assert await backoff_jwks.get_key(signing_key.kid) is not None
    assert await backoff_jwks.get_key("unknown-kid") is None
    assert jwks_fetcher.fetch_count == 2
    jwks_fetcher.document = None
    clock.advance(timedelta(seconds=31))
    assert await backoff_jwks.get_key(signing_key.kid) is not None
    assert jwks_fetcher.fetch_count == 3


async def test_unusable_jwks_valid_cached_key_still_verifies(
    verifier: AppleIdentityVerifier,
    tokens: TokenFactory,
    jwks_fetcher: FakeJwksFetcher,
    clock: FrozenClock,
) -> None:
    await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)
    jwks_fetcher.document = {"keys": []}
    clock.advance(timedelta(hours=7))
    assert (await verifier.verify(tokens.make(raw_nonce=NONCE), NONCE)).subject


async def test_unusable_jwks_cold_is_unavailable(
    backoff_jwks: JwksCache, jwks_fetcher: FakeJwksFetcher, signing_key: SigningKey
) -> None:
    jwks_fetcher.document = {}
    with pytest.raises(JwksUnavailableError):
        await backoff_jwks.get_key(signing_key.kid)
    with pytest.raises(JwksUnavailableError):  # backoff, not a retry
        await backoff_jwks.get_key(signing_key.kid)
    assert jwks_fetcher.fetch_count == 1
