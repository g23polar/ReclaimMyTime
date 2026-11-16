"""Calendar busy time for every route that solves (T4.2).

Rules (T4.2 + review round 1):
- busy time is fetched BEFORE the per-user advisory lock is taken (no Google call under the
  lock — the token mint may itself need that lock to mark a grant revoked, so fetching under
  it could deadlock);
- and with NO pooled connection checked out by the request: the fetch window (the horizon the
  solve will use) comes from a short preview transaction that is closed before the fetch;
- under the lock, the persisting step re-derives the horizon (in the locked transaction) and
  refuses to solve against a window that no longer covers it (`ensure_covers` → 503
  `gcal_unavailable`, retryable, nothing staged). Nothing under the lock opens another
  session or takes another lock.

Provider failures: 503 `gcal_unavailable`, 409 `gcal_reconnect_required`, raised before any
write transaction opens.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.api.deps import Maker
from app.api.gcal import get_gcal_runtime
from app.errors import AppError
from app.gcal.busy import BusyProvider, GcalBusy
from app.scheduler import Interval


@dataclass(frozen=True)
class BusyWindow:
    horizon: Interval | None = None  # what was fetched; None: nothing fetched (no solve expected)
    intervals: tuple[Interval, ...] = ()
    never_connected: bool = False  # busy is empty for ANY horizon

    def covers(self, horizon: Interval | None) -> bool:
        """Is this valid busy time for a solve over `horizon` (None: no solve)?"""
        if horizon is None:
            return True
        if self.horizon is None:
            return False  # nothing fetched (no preferences then), but a solve now
        return self.never_connected or horizon == self.horizon


async def fetch_window(
    provider: BusyProvider, user_id: uuid.UUID, horizon: Interval | None
) -> BusyWindow:
    """Busy time over `horizon`. Call with NO session open (no pooled connection held across
    the Google call); the provider uses its own short sessions."""
    if horizon is None:
        return BusyWindow()
    busy = await provider.busy(user_id, horizon)
    return BusyWindow(horizon, busy.intervals, never_connected=not busy.connected)


def ensure_covers(window: BusyWindow, horizon: Interval | None) -> None:
    """Under the per-user lock, before persisting a solve: 503 if the solve horizon moved since
    the busy read. Pure (no I/O), so it can never wait on another session or lock."""
    if not window.covers(horizon):
        raise window_moved()


def window_moved() -> AppError:
    """The solve horizon changed (a concurrent preferences change) between the busy fetch and
    the lock. Rare; retryable; nothing was staged."""
    return AppError(
        503,
        "gcal_unavailable",
        "Your schedule settings changed while your calendar was being checked, so nothing "
        "was changed. Please try again.",
    )


def get_busy_provider(request: Request, maker: Maker) -> BusyProvider:
    # The Google runtime is built only for a user who has a connection row.
    return GcalBusy(maker, lambda: get_gcal_runtime(request))


Busy = Annotated[BusyProvider, Depends(get_busy_provider)]
