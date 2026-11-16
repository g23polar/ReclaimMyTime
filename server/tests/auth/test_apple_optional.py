"""T6.0 (D26): Sign in with Apple is optional. The server builds and serves everything with no
APPLE_* variable; /auth/apple then answers 503 `apple_unavailable`; partial Apple
configuration fails fast. No database: the boot test only uses routes that never reach it."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

import app.db.session as db_session
from app.auth.apple import HttpAppleClient, UnconfiguredAppleClient
from app.auth.dependencies import build_runtime, default_runtime
from app.auth.errors import AppleNotConfiguredError
from app.auth.repository import InMemoryAuthRepository
from app.auth.settings import AuthConfigError, AuthSettings
from app.gcal.state import nonce_hash
from tests.api.contract import assert_conforms

_APPLE_VARS = ("APPLE_BUNDLE_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY")


def _keys() -> str:
    return f"1:{Fernet.generate_key().decode()}"


@asynccontextmanager
async def _provide():  # noqa: ANN202
    yield InMemoryAuthRepository()


def test_settings_without_any_apple_var() -> None:
    s = AuthSettings.from_env({"AUTH_FERNET_KEYS": _keys()})
    assert s.apple_configured is False and s.apple_bundle_id == ""


@pytest.mark.parametrize("stray", ["APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"])
def test_apple_credential_without_bundle_id_fails_fast(stray: str) -> None:
    with pytest.raises(AuthConfigError) as info:
        AuthSettings.from_env({"AUTH_FERNET_KEYS": _keys(), stray: "value"})
    assert "APPLE_BUNDLE_ID" in str(info.value) and stray in str(info.value)
    assert "value" not in str(info.value)


def test_bundle_id_without_credentials_still_fails_fast() -> None:
    with pytest.raises(AuthConfigError):
        AuthSettings.from_env({"AUTH_FERNET_KEYS": _keys(), "APPLE_BUNDLE_ID": "com.x.app"})


def test_runtime_without_apple_uses_the_unconfigured_client() -> None:
    settings = AuthSettings(apple_bundle_id="", fernet_keys=((1, Fernet.generate_key().decode()),))
    rt = build_runtime(settings, repository_provider=_provide)
    assert rt.apple_enabled is False and isinstance(rt.apple, UnconfiguredAppleClient)


def test_runtime_with_full_apple_config_is_unchanged() -> None:
    env = {
        "AUTH_FERNET_KEYS": _keys(),
        "APPLE_BUNDLE_ID": "com.x.app",
        "APPLE_TEAM_ID": "T",
        "APPLE_KEY_ID": "K",
        "APPLE_PRIVATE_KEY": "pem",
    }
    rt = build_runtime(AuthSettings.from_env(env), repository_provider=_provide)
    assert rt.apple_enabled is True and isinstance(rt.apple, HttpAppleClient)


async def test_unconfigured_apple_client_is_503_on_every_call() -> None:
    client = UnconfiguredAppleClient()
    with pytest.raises(AppleNotConfiguredError) as info:
        await client.exchange_code("code")
    assert (info.value.status_code, info.value.code) == (503, "apple_unavailable")
    with pytest.raises(AppleNotConfiguredError):
        await client.revoke_refresh_token("rt")


def test_default_runtime_builds_without_apple_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _APPLE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_FERNET_KEYS", _keys())
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:1/db")
    rt = default_runtime()
    assert rt.apple_enabled is False and isinstance(rt.apple, UnconfiguredAppleClient)


@pytest.fixture
async def booted(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The production app (app.main) configured from env with NO Apple variable: Google +
    Fernet keys only, and an unreachable DATABASE_URL (nothing below may touch the DB)."""
    from app.main import app as main_app

    for name in _APPLE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_FERNET_KEYS", _keys())
    monkeypatch.setenv("GCAL_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://api.example/gcal/callback")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:1/db")
    # Runtimes and the process-wide engine are built lazily from env; start clean and let
    # monkeypatch put back whatever was there.
    for attr in ("auth_runtime", "gcal_runtime"):
        monkeypatch.delattr(main_app.state, attr, raising=False)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_sessionmaker", None)
    transport = httpx.ASGITransport(app=main_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, main_app
    for attr in ("auth_runtime", "gcal_runtime"):
        if hasattr(main_app.state, attr):
            delattr(main_app.state, attr)


async def test_server_serves_without_apple_configuration(booted: Any) -> None:
    client, main_app = booted
    assert (await client.get("/health")).json() == {"status": "ok"}

    start = await client.post("/auth/google/start", json={"nonce_hash": nonce_hash("n" * 20)})
    assert start.status_code == 200, start.text
    assert start.json()["authorization_url"].startswith("https://accounts.google.com/")

    apple = await client.post("/auth/apple", json={})
    assert apple.status_code == 503 and apple.json()["error"]["code"] == "apple_unavailable"
    assert_conforms("signInWithApple", 503, apple.json())
    assert main_app.state.auth_runtime.apple_enabled is False

    bad = await client.post(
        "/auth/google/complete", json={"auth_code": "c", "state": "junk", "nonce": "n" * 20}
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "gcal_state_invalid"

    cb = await client.get("/gcal/callback", params={"state": "junk", "code": "c"})
    assert cb.status_code == 302
    assert (
        cb.headers["location"]
        == "reclaimmytime://gcal/callback?result=error&code=gcal_state_invalid"
    )

    # Authenticated routes (auth's own and the scheduling API) gate on the bearer as usual.
    tz = {"X-Timezone": "Europe/Paris"}
    for method, path in (("DELETE", "/me"), ("GET", "/projects"), ("GET", "/gcal/status")):
        resp = await client.request(method, path, headers=tz)
        assert (resp.status_code, resp.json()["error"]["code"]) == (401, "unauthenticated")


async def test_apple_route_refuses_before_validating_the_body(monkeypatch: Any) -> None:
    """Unconfigured → 503 regardless of the body (no 422 leak of the Apple schema)."""
    from app.auth.errors import register_error_handlers
    from app.auth.router import router

    settings = AuthSettings(apple_bundle_id="", fernet_keys=((1, Fernet.generate_key().decode()),))
    application = FastAPI()
    register_error_handlers(application)
    application.include_router(router)
    application.state.auth_runtime = build_runtime(settings, repository_provider=_provide)
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for body in (None, {}, {"identity_token": 1}):
            resp = await c.post("/auth/apple", json=body)
            assert resp.status_code == 503, resp.text
