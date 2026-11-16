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
from app.gcal.app_calendar import AppCalendarService
from app.gcal.busy import GcalBusy
from app.gcal.calendar import GoogleCalendarClient, HttpGoogleCalendarClient
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
    calendar: GoogleCalendarClient  # Calendar REST (T4.2): freeBusy + app calendar

    def service(self, maker: async_sessionmaker[AsyncSession]) -> GcalService:
        return GcalService(
            maker,
            settings=self.settings,
            client=self.client,
            cipher=self.cipher,
            state=self.state,
            clock=self.clock,
        )

    def busy(self, maker: async_sessionmaker[AsyncSession]) -> GcalBusy:
        return GcalBusy(maker, lambda: self)

    def app_calendar(self, maker: async_sessionmaker[AsyncSession]) -> AppCalendarService:
        return AppCalendarService(maker, service=self.service(maker), calendar=self.calendar)


def build_gcal_runtime(
    settings: GcalSettings,
    *,
    cipher: TokenCipher,
    client: GoogleOAuthClient | None = None,
    calendar: GoogleCalendarClient | None = None,
    clock: Clock | None = None,
) -> GcalRuntime:
    settings.require_oauth_settings()
    return GcalRuntime(
        settings=settings,
        client=client or HttpGoogleOAuthClient(settings),
        cipher=cipher,
        state=StateCodec(settings.state_key, settings.state_ttl),
        clock=clock or SystemClock(),
        calendar=calendar or HttpGoogleCalendarClient(settings),
    )


def default_gcal_runtime() -> GcalRuntime:
    settings = GcalSettings.from_env()
    raw_keys = os.environ.get("AUTH_FERNET_KEYS", "").strip()
    if not raw_keys:
        raise GcalConfigError("AUTH_FERNET_KEYS must be set")
    return build_gcal_runtime(settings, cipher=TokenCipher(parse_fernet_keys(raw_keys)))
