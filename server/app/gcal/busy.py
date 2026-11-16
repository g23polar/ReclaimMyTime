"""Calendar busy time for a solve (T4.2; ARCH §3 "Busy time & drift").

`GcalBusy.busy(user_id, horizon)` is what every solving route calls BEFORE taking the
per-user lock. It fails closed (decision T4.2 #3):

- never connected                          → `Busy(connected=False)`, no intervals;
- connection `revoked` (stored, or Google
  says invalid_grant while minting a token),
  or a stored credential no key decrypts      → GcalReconnectRequiredError (409);
- Google 5xx / timeout / unexpected answer /
  per-calendar `errors`                      → GcalUnavailableError (503).

The pure scheduler never imports this (import-linter `scheduler-purity`); callers hand the
resulting `Interval`s to `app.db.solver_io` like any other solver input.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.gcal import repository as repo
from app.gcal.calendar import BusySpan
from app.gcal.client import GoogleUnavailableError
from app.gcal.errors import (
    GcalConnectionInactiveError,
    GcalCredentialUndecryptableError,
    GcalGrantRevokedError,
    GcalReconnectRequiredError,
    GcalTransientError,
    GcalUnavailableError,
)
from app.scheduler import Interval

if TYPE_CHECKING:
    from app.gcal.runtime import GcalRuntime

logger = logging.getLogger(__name__)

_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class Busy:
    intervals: tuple[Interval, ...] = ()
    connected: bool = False  # False: the user never connected Google (busy is empty by fact)


class BusyProvider(Protocol):
    async def busy(self, user_id: uuid.UUID, horizon: Interval) -> Busy:
        """Busy time over `horizon` (clipped, merged, sorted). Raises GcalUnavailableError /
        GcalReconnectRequiredError."""
        ...


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _ceil_minute(dt: datetime) -> datetime:
    floored = _floor_minute(dt)
    return floored if floored == dt else floored + _MINUTE


def normalize_busy(spans: Iterable[BusySpan], horizon: Interval) -> tuple[Interval, ...]:
    """Raw Google spans → solver `Interval`s: widened outward to whole minutes (the solver
    contract; widening never frees busy time), clipped to `horizon`, merged where they overlap
    or touch, sorted. Pure."""
    clipped: list[tuple[datetime, datetime]] = []
    for span in spans:
        start = max(_floor_minute(span.start.astimezone(UTC)), horizon.start)
        end = min(_ceil_minute(span.end.astimezone(UTC)), horizon.end)
        if start < end:
            clipped.append((start, end))
    clipped.sort()
    merged: list[list[datetime]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple(Interval(start=s, end=e) for s, e in merged)


class GcalBusy:
    """Production `BusyProvider`. `runtime` is a factory so a user who never connected Google
    costs one DB read and needs no Google configuration at all."""

    def __init__(
        self, maker: async_sessionmaker[AsyncSession], runtime: Callable[[], GcalRuntime]
    ) -> None:
        self._maker = maker
        self._runtime = runtime

    async def _connection(self, user_id: uuid.UUID) -> repo.ConnectionRecord | None:
        async with self._maker() as s, s.begin():
            return await repo.get_connection(s, user_id)

    async def busy(self, user_id: uuid.UUID, horizon: Interval) -> Busy:
        row = await self._connection(user_id)
        if row is None:
            return Busy()
        if row.status != "active":
            raise GcalReconnectRequiredError()
        runtime = self._runtime()
        try:
            token = await runtime.service(self._maker).access_token(user_id)
        except GcalGrantRevokedError:
            raise GcalReconnectRequiredError() from None
        except GcalConnectionInactiveError:
            # Disconnected or revoked since the read above: decide on the current row.
            if await self._connection(user_id) is None:
                return Busy()
            raise GcalReconnectRequiredError() from None
        except GcalCredentialUndecryptableError as exc:
            # Decision T4.2 review Q2: reconnecting stores a fresh token under the current key,
            # so the user can self-heal; the operator still sees the key id.
            logger.error(
                "busy time: stored google credential undecryptable (key_id=%s)", exc.key_id
            )
            raise GcalReconnectRequiredError() from None
        except GcalTransientError:
            raise GcalUnavailableError() from None
        try:
            spans = await runtime.calendar.free_busy(
                token.token, time_min=horizon.start, time_max=horizon.end
            )
        except GoogleUnavailableError:
            raise GcalUnavailableError() from None
        intervals = normalize_busy(spans, horizon)
        logger.info("google freeBusy: %d busy interval(s) over the horizon", len(intervals))
        return Busy(intervals=intervals, connected=True)
