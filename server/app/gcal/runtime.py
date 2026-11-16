"""Long-lived gcal collaborators (settings, Google client, cipher, state codec, clock).

Lives on `app.state.gcal_runtime`; tests install one built with `build_gcal_runtime` and a
MockTransport client. Otherwise `default_gcal_runtime()` builds the production one from env
on first use (GOOGLE_*, GCAL_STATE_KEY, AUTH_FERNET_KEYS — refresh tokens are encrypted
with the same MultiFernet keys as Apple's, so key rotation is one procedure).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.clock import Clock, SystemClock
from app.auth.crypto import TokenCipher
from app.auth.settings import parse_fernet_keys
from app.gcal.client import GoogleOAuthClient, HttpGoogleOAuthClient
from app.gcal.service import GcalService
from app.gcal.settings import GcalConfigError, GcalSettings
from app.gcal.state import StateCodec


@dataclass
class GcalRuntime:
    settings: GcalSettings
    client: GoogleOAuthClient
    cipher: TokenCipher
    state: StateCodec
    clock: Clock

    def service(self, maker: async_sessionmaker[AsyncSession]) -> GcalService:
        return GcalService(
            maker,
            settings=self.settings,
            client=self.client,
            cipher=self.cipher,
            state=self.state,
            clock=self.clock,
        )


def build_gcal_runtime(
    settings: GcalSettings,
    *,
    cipher: TokenCipher,
    client: GoogleOAuthClient | None = None,
    clock: Clock | None = None,
) -> GcalRuntime:
    settings.require_oauth_settings()
    return GcalRuntime(
        settings=settings,
        client=client or HttpGoogleOAuthClient(settings),
        cipher=cipher,
        state=StateCodec(settings.state_key, settings.state_ttl),
        clock=clock or SystemClock(),
    )


def default_gcal_runtime() -> GcalRuntime:
    settings = GcalSettings.from_env()
    raw_keys = os.environ.get("AUTH_FERNET_KEYS", "").strip()
    if not raw_keys:
        raise GcalConfigError("AUTH_FERNET_KEYS must be set")
    return build_gcal_runtime(settings, cipher=TokenCipher(parse_fernet_keys(raw_keys)))
