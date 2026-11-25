"""The one way the server runs the solver (issue #22): `solve()` runs `pack` in a worker
thread under `SOLVER_TIMEOUT_SECONDS`, so a slow solve never blocks the event loop and a
runaway one fails as 503 `solver_timeout` instead of holding the request forever.

Configuration is built via `SolverSettings.from_env()` on each call (nothing reads env at
import time).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from app.errors import AppError
from app.scheduler import PlannedBlock, SolverInput, SolverOutput, pack

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SolverSettings:
    # Golden corpus (33 inputs × 7 runs, `replay()`, Intel i9-9880H 2.3 GHz, Python 3.12):
    # median 4.2 ms, worst 46 ms (infeasible_conflicting_pins). 10× the worst case rounds up
    # to 1 s; the 5 s floor leaves room for the shared single vCPU in production.
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> SolverSettings:
        return cls(
            timeout_seconds=float(os.environ.get("SOLVER_TIMEOUT_SECONDS", cls.timeout_seconds))
        )


class SolverTimeoutError(AppError):
    """The solve ran out of time. Raised inside the request's transaction, so it rolls back:
    nothing is staged, applied or promoted."""

    def __init__(self) -> None:
        super().__init__(
            503,
            "solver_timeout",
            "Planning took too long, so nothing was changed. Please try again.",
        )


async def solve(input: SolverInput, previous: Sequence[PlannedBlock] | None = None) -> SolverOutput:
    """`pack(input, previous)` in a worker thread, bounded by `SOLVER_TIMEOUT_SECONDS`.

    A timed-out solve is NOT stopped: Python threads cannot be killed, so it runs on in its
    thread until `pack` returns, and its result is discarded. The caller gets
    `SolverTimeoutError` as soon as the timeout runs out."""
    timeout = SolverSettings.from_env().timeout_seconds
    try:
        return await asyncio.wait_for(asyncio.to_thread(pack, input, previous), timeout)
    except TimeoutError:
        logger.warning("solver timed out after %ss", timeout)
        raise SolverTimeoutError() from None
