"""Opaque session tokens: minting, hashing, and expiry arithmetic (pure; no I/O, no clock).

- Token: 256 random bits, urlsafe base64 (43 chars). Only sha256(token) is stored.
- Expiry: sliding `sliding` window from last activity, capped at `absolute` from created_at.
- Activity (`last_seen_at`) is written at most once per `last_seen_interval`; the sliding
  extension rides on the same write, so an active session's expires_at is extended at
  most hourly too.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

TOKEN_BYTES = 32  # 256 bits
_RESOLUTION = timedelta(microseconds=1)


@dataclass(frozen=True)
class SessionPolicy:
    sliding: timedelta
    absolute: timedelta
    last_seen_interval: timedelta

    def expiry(self, created_at: datetime, now: datetime) -> datetime:
        return min(now + self.sliding, created_at + self.absolute)

    def is_expired(self, expires_at: datetime, now: datetime) -> bool:
        return now >= expires_at

    def touch_threshold(self, now: datetime) -> datetime:
        """A session is due a touch iff last_seen_at is NULL or < this.

        Equivalent to `now - last_seen_at >= last_seen_interval` at microsecond resolution
        (Python datetime and Postgres timestamptz), expressed as a strict bound so the
        repository can re-check it against the persisted row.
        """
        return now - self.last_seen_interval + _RESOLUTION

    def needs_touch(self, last_seen_at: datetime | None, now: datetime) -> bool:
        return last_seen_at is None or last_seen_at < self.touch_threshold(now)


def mint_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
