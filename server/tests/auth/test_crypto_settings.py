from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.auth.crypto import DecryptionError, TokenCipher
from app.auth.settings import AuthConfigError, AuthSettings, parse_fernet_keys


def test_round_trip_and_ciphertext_hides_plaintext() -> None:
    cipher = TokenCipher([(1, Fernet.generate_key().decode())])
    enc = cipher.encrypt("apple-refresh-secret")
    assert b"apple-refresh-secret" not in enc
    assert cipher.decrypt(enc) == "apple-refresh-secret"
    assert cipher.primary_key_id == 1


def test_rotation_with_key_ids() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    enc_old = TokenCipher([(1, old)]).encrypt("s")
    rotated_cipher = TokenCipher([(2, new), (1, old)])
    assert rotated_cipher.primary_key_id == 2
    assert rotated_cipher.decrypt(enc_old) == "s"
    rotated = rotated_cipher.rotate(enc_old)
    assert TokenCipher([(2, new)]).decrypt(rotated) == "s"
    with pytest.raises(DecryptionError):
        TokenCipher([(2, new)]).decrypt(enc_old)


def test_parse_fernet_keys() -> None:
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    assert parse_fernet_keys(f"2:{k2}, 1:{k1}") == ((2, k2), (1, k1))
    for bad in ("", "nokeyid", f"x:{k1}", f"1:{k1},1:{k2}"):
        with pytest.raises(AuthConfigError):
            parse_fernet_keys(bad)


def test_settings_from_env_is_explicit() -> None:
    key = Fernet.generate_key().decode()
    s = AuthSettings.from_env(
        {"APPLE_BUNDLE_ID": "com.x.app", "AUTH_FERNET_KEYS": f"1:{key}"},
        require_apple_credentials=False,
    )
    assert s.apple_bundle_id == "com.x.app"
    assert s.apple_issuer == "https://appleid.apple.com"
    assert s.session_sliding.days == 30 and s.session_absolute.days == 180
    assert key not in repr(s)
    with pytest.raises(AuthConfigError):
        AuthSettings.from_env({})


def test_main_app_mounts_auth_routes() -> None:
    from app.main import app

    ops = {
        (path, method): op["operationId"]
        for path, item in app.openapi()["paths"].items()
        for method, op in item.items()
    }
    # operationIds and paths verbatim from contracts/openapi.yaml.
    assert {
        ("/auth/apple", "post"): "signInWithApple",
        ("/me", "get"): "getMe",
        ("/me", "delete"): "deleteMe",
        ("/sessions", "delete"): "revokeAllSessions",
        ("/sessions/current", "delete"): "revokeCurrentSession",
    }.items() <= ops.items()


# ----------------------------------------------------------------- production Apple credentials

_APPLE_CREDS = ("APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY")


def _base_env() -> dict[str, str]:
    return {
        "APPLE_BUNDLE_ID": "com.x.app",
        "AUTH_FERNET_KEYS": f"1:{Fernet.generate_key().decode()}",
    }


@pytest.mark.parametrize("missing", [*_APPLE_CREDS, "all"])
def test_from_env_requires_apple_credentials_by_default(missing: str) -> None:
    env = _base_env() | {"APPLE_TEAM_ID": "T", "APPLE_KEY_ID": "K", "APPLE_PRIVATE_KEY": "pem"}
    absent = _APPLE_CREDS if missing == "all" else (missing,)
    for name in absent:
        env[name] = "  " if name == "APPLE_KEY_ID" else ""
        if name == "APPLE_TEAM_ID":
            del env[name]
    with pytest.raises(AuthConfigError) as info:
        AuthSettings.from_env(env)
    message = str(info.value)
    for name in absent:
        assert name in message
    for name in set(_APPLE_CREDS) - set(absent):
        assert name not in message
    assert "pem" not in message


def test_from_env_with_apple_credentials() -> None:
    env = _base_env() | {"APPLE_TEAM_ID": "T", "APPLE_KEY_ID": "K", "APPLE_PRIVATE_KEY": "a\\nb"}
    s = AuthSettings.from_env(env)
    assert (s.apple_team_id, s.apple_key_id, s.apple_private_key_pem) == ("T", "K", "a\nb")


def test_from_env_without_apple_credentials_when_client_injected() -> None:
    s = AuthSettings.from_env(_base_env(), require_apple_credentials=False)
    assert s.apple_team_id == "" and s.apple_private_key_pem == ""


def test_build_runtime_requires_credentials_only_without_injected_client(
    settings: AuthSettings, tokens
) -> None:
    from contextlib import asynccontextmanager

    from app.auth.dependencies import build_runtime
    from app.auth.repository import InMemoryAuthRepository
    from tests.auth.fixtures import FakeAppleClient

    @asynccontextmanager
    async def provide():  # noqa: ANN202
        yield InMemoryAuthRepository()

    assert settings.apple_team_id == ""
    injected = build_runtime(
        settings, repository_provider=provide, apple_client=FakeAppleClient(tokens)
    )
    assert isinstance(injected.apple, FakeAppleClient)
    with pytest.raises(AuthConfigError) as info:
        build_runtime(settings, repository_provider=provide)
    assert all(name in str(info.value) for name in _APPLE_CREDS)


def test_default_runtime_with_injected_apple_client_skips_credentials(
    monkeypatch: pytest.MonkeyPatch, tokens
) -> None:
    from app.auth.dependencies import default_runtime
    from tests.auth.fixtures import FakeAppleClient

    for name, value in _base_env().items():
        monkeypatch.setenv(name, value)
    for name in _APPLE_CREDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:1/db")
    with pytest.raises(AuthConfigError):
        default_runtime()
    runtime = default_runtime(apple_client=FakeAppleClient(tokens))
    assert isinstance(runtime.apple, FakeAppleClient)
