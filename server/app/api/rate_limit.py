"""Per-user write rate limit (issue #23): every scheduling write route shares one fixed-window
limit per user, `limit` accepted writes per `window_seconds`, counted in `write_rate_counters`
(app.db.write_rate) so it holds across every server process.

`limit_writes` is attached to each covered route's decorator (`dependencies=[…]`), never to a
router, because the routers also serve GETs. It runs after authentication (401s never count)
and before the handler, taking its slot in its own short transaction that commits before the
handler's opens, so a request never holds two connections. Over the limit it raises 429
`rate_limited` with `Retry-After`, and the handler never runs. Every request that reaches it
counts, whatever the handler later returns.

Settings come from `WriteRateSettings.from_env()` (nothing reads env at import), cached on
`app.state.write_rate_settings`, which is also the test seam.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request

from app.api.deps import Maker, Now, UserId, transaction
from app.db.write_rate import take_write_slot, window_start
from app.errors import AppError

_RATE_LIMITED = "Too many changes at once. Try again in a moment."


@dataclass(frozen=True)
class WriteRateSettings:
    limit: int = 60  # accepted writes per user per window, shared by every covered route
    window_seconds: int = 60

    @classmethod
    def from_env(cls) -> WriteRateSettings:
        return cls(
            limit=int(os.environ.get("WRITE_RATE_LIMIT", cls.limit)),
            window_seconds=int(os.environ.get("WRITE_RATE_WINDOW_SECONDS", cls.window_seconds)),
        )


def write_rate_settings(request: Request) -> WriteRateSettings:
    settings = getattr(request.app.state, "write_rate_settings", None)
    if settings is None:
        settings = request.app.state.write_rate_settings = WriteRateSettings.from_env()
    return settings


async def limit_writes(request: Request, user_id: UserId, maker: Maker, now: Now) -> None:
    settings = write_rate_settings(request)
    window = timedelta(seconds=settings.window_seconds)
    async with transaction(maker) as tx:
        taken = await take_write_slot(tx, user_id, now=now, limit=settings.limit, window=window)
    if not taken:
        left = window_start(now, window) + window - now
        retry_after = max(1, math.ceil(left.total_seconds()))
        raise AppError(
            429, "rate_limited", _RATE_LIMITED, headers={"Retry-After": str(retry_after)}
        )


LimitWrites = Depends(limit_writes)
