"""Google Calendar connection routes (`gcal` tag; T4.1). Thin: all logic is in
app.gcal.service. The callback is the only unauthenticated route and the only one that
skips X-Timezone (the browser, not the API client, hits it); it only hands Google's code
back to the app, which finishes with its bearer via POST /gcal/complete (D24).

`POST /gcal/sync` (T4.3, contract D25) is the single sync entry point: write-back (a), then
Google-side edit ingestion and the drift check (b, app.api.gcal_sync)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select

from app.api.deps import Maker, Now, RawBody, UserId, parse_body, transaction
from app.api.views import changeset_view
from app.auth.dependencies import require_timezone
from app.auth.schemas import UtcDateTime
from app.db import outbox
from app.db.models import Changeset
from app.errors import AppError
from app.gcal.busy import BusyProvider, GcalBusy
from app.gcal.repository import ConnectionRecord
from app.gcal.runtime import GcalRuntime, default_gcal_runtime
from app.gcal.service import GcalService
from app.gcal.writeback import DrainStatus

router = APIRouter(tags=["gcal"])


class GcalConnectUrlOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorization_url: str


class GcalCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_code: str = Field(min_length=1)
    state: str = Field(min_length=1)


_COMPLETE: TypeAdapter[GcalCompleteIn] = TypeAdapter(GcalCompleteIn)


class GoogleConnectionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    google_email: str
    scopes: list[str]
    app_calendar_id: str | None
    status: str
    revoked_at: UtcDateTime | None
    created_at: UtcDateTime


class GcalStatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: GoogleConnectionOut | None


def _connection_out(row: ConnectionRecord) -> GoogleConnectionOut:
    return GoogleConnectionOut(
        google_email=row.google_email,
        scopes=list(row.scopes),
        app_calendar_id=row.app_calendar_id,
        status=row.status,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------- seams (app.state)


def runtime_for_app(app: FastAPI) -> GcalRuntime:
    runtime = getattr(app.state, "gcal_runtime", None)
    if runtime is None:
        runtime = app.state.gcal_runtime = default_gcal_runtime()
    return runtime


def get_gcal_runtime(request: Request) -> GcalRuntime:
    return runtime_for_app(request.app)


def get_gcal_service(
    runtime: Annotated[GcalRuntime, Depends(get_gcal_runtime)], maker: Maker
) -> GcalService:
    return runtime.service(maker)


Service = Annotated[GcalService, Depends(get_gcal_service)]


# ---------------------------------------------------------------- routes


@router.get(
    "/gcal/connect",
    operation_id="getGcalConnectUrl",
    response_model=GcalConnectUrlOut,
    dependencies=[Depends(require_timezone)],
)
async def get_connect_url(user_id: UserId, service: Service) -> GcalConnectUrlOut:
    return GcalConnectUrlOut(authorization_url=service.authorization_url(user_id))


@router.get("/gcal/callback", operation_id="gcalCallback", status_code=302)
async def gcal_callback(
    service: Service,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    # Every parameter is optional here so a malformed callback still gets the contract's
    # 302 rather than a JSON 422. `scope` is not read: the token response is authoritative.
    return RedirectResponse(
        service.callback_redirect(state=state, code=code, error=error),
        status_code=302,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post(
    "/gcal/complete",
    operation_id="completeGcalConnect",
    response_model=GcalStatusOut,
    dependencies=[Depends(require_timezone)],
)
async def complete(user_id: UserId, service: Service, body: RawBody) -> GcalStatusOut:
    req = parse_body(_COMPLETE, body)
    row = await service.complete(user_id, auth_code=req.auth_code, state=req.state)
    return GcalStatusOut(connection=_connection_out(row))


@router.get(
    "/gcal/status",
    operation_id="getGcalStatus",
    response_model=GcalStatusOut,
    dependencies=[Depends(require_timezone)],
)
async def get_status(user_id: UserId, service: Service) -> GcalStatusOut:
    row = await service.connection(user_id)
    return GcalStatusOut(connection=_connection_out(row) if row is not None else None)


@router.delete(
    "/gcal",
    operation_id="disconnectGcal",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_timezone)],
)
async def disconnect(user_id: UserId, service: Service) -> Response:
    await service.disconnect(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- sync (T4.3, D25)


def _busy_provider(request: Request, maker: Maker) -> BusyProvider:
    # Same provider as app.api.busy.get_busy_provider (which imports this module).
    return GcalBusy(maker, lambda: get_gcal_runtime(request))


SyncBusy = Annotated[BusyProvider, Depends(_busy_provider)]

SYNC_BUDGET_S = 10.0  # overall wait for the write-back; the rest stays pending


@router.post(
    "/gcal/sync",
    operation_id="syncGcal",
    response_model=None,
    dependencies=[Depends(require_timezone)],
)
async def sync(
    user_id: UserId, request: Request, maker: Maker, now: Now, busy: SyncBusy
) -> dict[str, Any]:
    """Drain this user's write-back outbox (bounded), then answer with the outbox state and the
    user's current pending changeset (O2/G3). No request transaction is held while Google is
    called: the drain opens its own short sessions, and the read below runs after it."""
    from app.api.outbox import get_outbox  # app.api.outbox imports this module

    result = await get_outbox(request.app).wait(user_id, SYNC_BUDGET_S)
    if result is not None and result.status is DrainStatus.RECONNECT_REQUIRED:
        raise AppError(
            409,
            "gcal_reconnect_required",
            "Google Calendar access was revoked. Please reconnect your calendar.",
        )
    if result is not None and result.status is DrainStatus.UNAVAILABLE:
        # What was written stays written; the rest stays pending (retried by confirm/sync).
        raise AppError(
            503, "gcal_unavailable", "Google Calendar could not be reached. Please try again."
        )
    if result is None or result.status is not DrainStatus.NOT_CONNECTED:
        # T4.3b, after the write-back (so Google holds our latest writes): ingest edits made
        # to our events inside Google (hybrid: direct applies + a `google_edit` proposal),
        # then the drift check (live blocks ∩ fresh busy → a `drift` proposal). Google is
        # read before the lock; see app.api.gcal_sync. The listing's 404 also recreates an
        # app calendar deleted while every block was synced (deferred from part (a)).
        from app.api.gcal_sync import ingest_and_check_drift  # imports this module

        # A drain still running past the budget (result None): drift only, no ingest.
        await ingest_and_check_drift(
            request.app, user_id, now=now, busy=busy, ingest_edits=result is not None
        )
    async with transaction(maker) as tx:
        counts = (
            outbox.OutboxCounts()
            if result is not None and result.status is DrainStatus.NOT_CONNECTED
            else await outbox.counts(tx, user_id, now)
        )
        pending = (
            await tx.execute(
                select(Changeset.id)
                .where(Changeset.user_id == user_id, Changeset.status == "pending")
                .order_by(Changeset.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        view = None if pending is None else await changeset_view(tx, user_id, pending)
    return {
        "pending_changeset": view,
        "outbox": {"pending": counts.pending, "synced": counts.synced},
    }
