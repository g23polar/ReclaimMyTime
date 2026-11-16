"""Google Calendar connection routes (`gcal` tag; T4.1). Thin: all logic is in
app.gcal.service. The callback is the only unauthenticated route and the only one that
skips X-Timezone (the browser, not the API client, hits it); it only hands Google's code
back to the app, which finishes with its bearer via POST /gcal/complete (D24)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.api.deps import Maker, RawBody, UserId, parse_body
from app.auth.dependencies import require_timezone
from app.auth.schemas import UtcDateTime
from app.gcal.repository import ConnectionRecord
from app.gcal.runtime import GcalRuntime, default_gcal_runtime
from app.gcal.service import GcalService

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


def get_gcal_runtime(request: Request) -> GcalRuntime:
    runtime = getattr(request.app.state, "gcal_runtime", None)
    if runtime is None:
        runtime = request.app.state.gcal_runtime = default_gcal_runtime()
    return runtime


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
