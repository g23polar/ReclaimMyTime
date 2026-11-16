"""Pure T4.1 pieces: PKCE S256, the stateless OAuth state, settings, id_token checks."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.gcal.client import IdTokenInvalidError, google_email_from_id_token
from app.gcal.settings import GcalConfigError, GcalSettings
from app.gcal.state import StateCodec, StateInvalidError, new_code_verifier, s256_challenge
from tests.gcal.fakes import CLIENT_ID, DROP, EMAIL, id_token

NOW = datetime(2026, 11, 15, 2, 40, tzinfo=UTC)
TTL = timedelta(minutes=10)
UID = uuid.UUID("0b6e2f1a-3c4d-4e5f-8a9b-0c1d2e3f4a5b")


# ---------------------------------------------------------------- PKCE


def test_code_verifier_is_rfc7636_shaped_and_random() -> None:
    a, b = new_code_verifier(), new_code_verifier()
    assert a != b
    for v in (a, b):
        assert 43 <= len(v) <= 128
        assert set(v) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def test_s256_challenge_is_unpadded_base64url_sha256() -> None:
    verifier = "a" * 64
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    assert s256_challenge(verifier) == expected.rstrip("=")
    assert len(s256_challenge(verifier)) == 43 and "=" not in s256_challenge(verifier)


# ---------------------------------------------------------------- state


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


def test_state_round_trips(key: str) -> None:
    codec = StateCodec(key, TTL)
    token, issued = codec.issue(UID, NOW)
    opened = codec.open(token, NOW + TTL - timedelta(seconds=1))
    assert opened == issued
    assert opened.user_id == UID and opened.expires_at == int((NOW + TTL).timestamp())
    assert issued.code_verifier not in token  # encrypted, not just signed


def test_each_state_is_unique(key: str) -> None:
    codec = StateCodec(key, TTL)
    (t1, s1), (t2, s2) = codec.issue(UID, NOW), codec.issue(UID, NOW)
    assert t1 != t2 and s1.nonce != s2.nonce and s1.code_verifier != s2.code_verifier


def test_state_expires_after_ttl(key: str) -> None:
    codec = StateCodec(key, TTL)
    token, _ = codec.issue(UID, NOW)
    with pytest.raises(StateInvalidError, match="expired"):
        codec.open(token, NOW + TTL)


def test_state_from_another_key_is_rejected(key: str) -> None:
    token, _ = StateCodec(Fernet.generate_key().decode(), TTL).issue(UID, NOW)
    with pytest.raises(StateInvalidError, match="undecryptable"):
        StateCodec(key, TTL).open(token, NOW)


@pytest.mark.parametrize("position", [0, 20, 60, -5])
def test_tampered_state_is_rejected(key: str, position: int) -> None:
    codec = StateCodec(key, TTL)
    token, _ = codec.issue(UID, NOW)
    i = position % len(token)  # -5: inside the HMAC, clear of base64 padding bits
    tampered = token[:i] + ("A" if token[i] != "A" else "B") + token[i + 1 :]
    with pytest.raises(StateInvalidError):
        codec.open(tampered, NOW)


@pytest.mark.parametrize("token", [None, "", "not-a-fernet-token", "é"])
def test_missing_or_garbage_state_is_rejected(key: str, token: str | None) -> None:
    with pytest.raises(StateInvalidError):
        StateCodec(key, TTL).open(token, NOW)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        json.dumps({"u": "not-a-uuid", "v": "a" * 64, "n": "n", "exp": 2**40}).encode(),
        json.dumps({"u": str(UID), "v": "short", "n": "n", "exp": 2**40}).encode(),
        json.dumps({"u": str(UID), "v": "a" * 64, "n": "n", "exp": "soon"}).encode(),
        json.dumps({"u": str(UID), "v": "a" * 64, "n": "n", "exp": True}).encode(),
        json.dumps({"u": str(UID), "v": "a" * 64, "exp": 2**40}).encode(),
    ],
)
def test_malformed_payload_under_the_right_key_is_rejected(key: str, payload: bytes) -> None:
    token = Fernet(key.encode()).encrypt(payload).decode()
    with pytest.raises(StateInvalidError, match="malformed"):
        StateCodec(key, TTL).open(token, NOW)


# ---------------------------------------------------------------- settings


def _env(**over: str) -> dict[str, str]:
    env = {
        "GOOGLE_CLIENT_ID": CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": "s",
        "GOOGLE_REDIRECT_URI": "https://api.example/gcal/callback",
        "GCAL_STATE_KEY": Fernet.generate_key().decode(),
        "AUTH_FERNET_KEYS": f"1:{Fernet.generate_key().decode()}",
    }
    env.update(over)
    return env


def test_settings_from_env_defaults() -> None:
    s = GcalSettings.from_env(_env())
    assert s.app_callback_url == "reclaimmytime://gcal/callback"
    assert s.token_url == "https://oauth2.googleapis.com/token"
    assert s.revoke_url == "https://oauth2.googleapis.com/revoke"
    assert s.state_ttl == timedelta(minutes=10)
    assert "secret" not in repr(s) and s.state_key not in repr(s)


def test_settings_endpoints_are_injectable() -> None:
    s = GcalSettings.from_env(
        _env(
            GOOGLE_TOKEN_URL="https://t/token",
            GOOGLE_REVOKE_URL="https://t/revoke",
            GOOGLE_AUTH_URL="https://t/auth",
            GOOGLE_HTTP_TIMEOUT_SECONDS="2.5",
            GCAL_APP_CALLBACK_URL="myapp://cb",
        )
    )
    assert (s.token_url, s.revoke_url, s.auth_url) == (
        "https://t/token",
        "https://t/revoke",
        "https://t/auth",
    )
    assert s.http_timeout_s == 2.5 and s.app_callback_url == "myapp://cb"


@pytest.mark.parametrize(
    "missing", ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GCAL_STATE_KEY"]
)
def test_settings_require_oauth_vars(missing: str) -> None:
    with pytest.raises(GcalConfigError, match=missing):
        GcalSettings.from_env(_env(**{missing: ""}))


def test_revocation_only_settings_need_no_credentials() -> None:
    s = GcalSettings.from_env({"GOOGLE_REVOKE_URL": "https://t/revoke"}, require_oauth=False)
    assert s.revoke_url == "https://t/revoke" and s.client_id == ""


def test_state_key_must_be_fernet_and_separate_from_auth_keys() -> None:
    with pytest.raises(GcalConfigError, match="Fernet"):
        GcalSettings.from_env(_env(GCAL_STATE_KEY="not-a-key"))
    shared = Fernet.generate_key().decode()
    with pytest.raises(GcalConfigError, match="differ"):
        GcalSettings.from_env(_env(GCAL_STATE_KEY=shared, AUTH_FERNET_KEYS=f"2:x,1:{shared}"))


def test_app_callback_url_must_not_carry_a_query() -> None:
    with pytest.raises(GcalConfigError):
        GcalSettings.from_env(_env(GCAL_APP_CALLBACK_URL="myapp://cb?x=1"))


# ---------------------------------------------------------------- id_token


def _email(token: str | None, now: datetime = NOW) -> str:
    return google_email_from_id_token(
        token, client_id=CLIENT_ID, now=now, leeway=timedelta(seconds=30)
    )


def test_id_token_yields_verified_email() -> None:
    assert _email(id_token(NOW)) == EMAIL
    assert _email(id_token(NOW, iss="accounts.google.com")) == EMAIL
    assert _email(id_token(NOW, email_verified="true")) == EMAIL
    assert _email(id_token(NOW, aud=[CLIENT_ID])) == EMAIL


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"iss": "https://evil.example"}, "issuer"),
        ({"iss": DROP}, "issuer"),
        ({"aud": "someone-else.apps.googleusercontent.com"}, "audience"),
        ({"aud": ["other", CLIENT_ID], "azp": "other"}, "audience"),
        ({"exp": DROP}, "exp"),
        ({"email": DROP}, "email"),
        ({"email_verified": False}, "email_verified"),
        ({"email_verified": DROP}, "email_verified"),
    ],
)
def test_id_token_claim_failures(claims: dict[str, object], reason: str) -> None:
    with pytest.raises(IdTokenInvalidError, match=reason):
        _email(id_token(NOW, **claims))


def test_id_token_expiry_uses_injected_clock_with_leeway() -> None:
    token = id_token(NOW - timedelta(hours=1))  # exp == NOW
    assert _email(token, NOW + timedelta(seconds=29)) == EMAIL
    with pytest.raises(IdTokenInvalidError, match="expired"):
        _email(token, NOW + timedelta(seconds=30))


@pytest.mark.parametrize("token", [None, "", "garbage", "a.b.c"])
def test_missing_or_undecodable_id_token(token: str | None) -> None:
    with pytest.raises(IdTokenInvalidError):
        _email(token)


def test_revoke_only_settings_ignore_broken_consent_config() -> None:
    s = GcalSettings.from_env(
        {
            "GCAL_APP_CALLBACK_URL": "myapp://cb?x=1#y",
            "GCAL_STATE_KEY": "not-a-key",
            "GOOGLE_CLIENT_ID": "",
        },
        require_oauth=False,
    )
    assert s.revoke_url == "https://oauth2.googleapis.com/revoke"
    with pytest.raises(GcalConfigError):
        GcalSettings.from_env({"GOOGLE_HTTP_TIMEOUT_SECONDS": "soon"}, require_oauth=False)


@pytest.mark.parametrize("url", ["myapp://cb?x=1", "myapp://cb#frag", ""])
def test_build_gcal_runtime_applies_the_same_callback_url_rules(url: str) -> None:
    from app.auth.crypto import TokenCipher
    from app.gcal.runtime import build_gcal_runtime
    from tests.gcal.fakes import settings

    cipher = TokenCipher([(1, Fernet.generate_key().decode())])
    with pytest.raises(GcalConfigError, match="GCAL_APP_CALLBACK_URL"):
        build_gcal_runtime(settings(app_callback_url=url), cipher=cipher)
    with pytest.raises(GcalConfigError, match="GCAL_APP_CALLBACK_URL"):
        GcalSettings.from_env(_env(GCAL_APP_CALLBACK_URL=url or " "))


def test_build_gcal_runtime_rejects_a_bad_state_key() -> None:
    from app.auth.crypto import TokenCipher
    from app.gcal.runtime import build_gcal_runtime
    from tests.gcal.fakes import settings

    cipher = TokenCipher([(1, Fernet.generate_key().decode())])
    with pytest.raises(GcalConfigError, match="Fernet"):
        build_gcal_runtime(settings(state_key="nope"), cipher=cipher)
