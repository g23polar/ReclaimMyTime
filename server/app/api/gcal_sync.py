"""`POST /gcal/sync` part (b) (T4.3b; contract D25): ingest edits made in Google, then drift.

Runs after the write-back drain, and only when it neither failed nor found no connection.
Three phases, so that no Google call ever runs under the per-user lock or with a pooled
connection checked out (the T4.2 deadlock lesson; tests/api/test_gcal_ingest_pool.py):

1. a short read transaction: the live synced rows to compare, and the solve horizon;
2. Google, with no session open: list the app calendar (+ events.get for rows missing from
   the listing), then ONE fresh primary freeBusy read over the solve horizon, shared by the
   edit re-solves and the drift check (user decision 2026-11-16: a busy failure applies no
   edit and answers 503/409; what the drain wrote stays written);
3. one transaction under the lock: `ensure_covers` (the horizon has not moved), then
   `apply_google_edits` (re-validates every edit; state that changed since phase 2 is skipped
   and re-evaluated by the next sync), then `propose_drift`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import FastAPI

from app.api.busy import ensure_covers, fetch_window
from app.api.deps import clock_for_app, maker_for_app, transaction
from app.api.gcal import runtime_for_app
from app.api.outbox import kick_outbox
from app.db import outbox
from app.db.write_paths import apply_google_edits, lock_user, propose_drift, solve_horizon
from app.gcal.busy import BusyProvider
from app.gcal.ingest import IngestResult, read_google_edits

logger = logging.getLogger(__name__)


async def ingest_and_check_drift(
    app: FastAPI, user_id: uuid.UUID, *, now: datetime, busy: BusyProvider, ingest_edits: bool
) -> None:
    """Raises GcalReconnectRequiredError (409) / GcalUnavailableError (503) from Google, and
    the busy window's 503 if the solve horizon moved meanwhile; otherwise commits.

    `ingest_edits=False` (the drain is still running past the sync budget): only the drift
    check runs. Listing would queue behind a slow Google and race the drain's writes; the
    next sync ingests."""
    maker = maker_for_app(app)
    async with transaction(maker) as tx:
        candidates = await outbox.ingest_candidates(tx, user_id, now)
        horizon = await solve_horizon(tx, user_id, [], now)
    if candidates.window is None or horizon is None:
        return  # no live plan of this or a later week: nothing to compare, nothing to drift

    ingest = (
        await read_google_edits(maker, lambda: runtime_for_app(app), user_id, candidates, now=now)
        if ingest_edits
        else IngestResult()
    )
    if not ingest.connected:
        return
    window = await fetch_window(busy, user_id, horizon)

    async with transaction(maker) as tx:
        await lock_user(tx, user_id)
        ensure_covers(window, await solve_horizon(tx, user_id, [], now))
        outcome = await apply_google_edits(
            tx,
            user_id,
            ingest.edits,
            now=now,
            busy=window.intervals,
            clock=clock_for_app(app).now,  # stamps a restaged changeset at write time
        )
        drift = await propose_drift(tx, user_id, now=now, busy=window.intervals)
    if ingest.edits:
        logger.info(
            "gcal ingest: %d applied, %d staged, %d skipped%s",
            outcome.applied,
            outcome.staged,
            outcome.skipped,
            ", proposal staged" if outcome.changeset_id is not None else "",
        )
    if drift is not None:
        logger.info("gcal drift: live blocks overlap calendar busy time; re-solve proposed")
    if ingest.recreated or outcome.applied:
        # Recreated calendar: refill it. Direct applies: delete the removed block's event.
        kick_outbox(app, user_id)
