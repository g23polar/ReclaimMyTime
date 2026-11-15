"""No-database wiring: X-Timezone and auth gate every route, the envelope move kept auth's
imports, T2.1 exceptions map to the contract table, and body paths name the sent field."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter

from app import errors as app_errors
from app.api.deps import parse_body
from app.api.errors import register_write_path_handlers, write_path_error
from app.auth import errors as auth_errors
from app.db.models.staged import InvalidMutation, ProjectDraft
from app.db.write_paths import (
    BlockNotFound,
    BlockNotStarted,
    BlockSuperseded,
    ChangesetNotFound,
    ChangesetNotPending,
    PlanVerificationError,
    PreferencesNotSet,
)
from app.errors import AppError
from app.scheduler import Violation
from tests.api.conftest import build_app

ROUTES = [
    ("GET", "/projects"),
    ("POST", "/projects"),
    ("GET", f"/projects/{uuid.uuid4()}"),
    ("PATCH", f"/projects/{uuid.uuid4()}"),
    ("DELETE", f"/projects/{uuid.uuid4()}"),
    ("GET", "/preferences"),
    ("PUT", "/preferences"),
    ("GET", "/exceptions"),
    ("POST", "/exceptions"),
    ("GET", f"/exceptions/{uuid.uuid4()}"),
    ("DELETE", f"/exceptions/{uuid.uuid4()}"),
    ("GET", "/pins"),
    ("DELETE", f"/pins/{uuid.uuid4()}"),
    ("PATCH", f"/blocks/{uuid.uuid4()}"),
    ("GET", "/plans/current"),
    ("GET", f"/plans/{uuid.uuid4()}/diff?against={uuid.uuid4()}"),
    ("POST", f"/changesets/{uuid.uuid4()}/confirm"),
    ("POST", f"/changesets/{uuid.uuid4()}/reject"),
]


@pytest.fixture
async def client() -> Any:
    app = build_app()
    app.state.auth_runtime = object()  # never reached without a bearer token
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_timezone_then_auth_gate_every_route(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    body = {} if method in ("POST", "PUT", "PATCH") else None
    for headers in ({}, {"X-Timezone": "Mars/Olympus"}, {"X-Timezone": "not a zone"}):
        resp = await client.request(method, path, json=body, headers=headers)
        assert (resp.status_code, resp.json()["error"]["code"]) == (400, "timezone_invalid")
    resp = await client.request(method, path, json=body, headers={"X-Timezone": "Europe/Paris"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (401, "unauthenticated")


def test_envelope_moved_with_auth_reexports() -> None:
    assert auth_errors.AppError is app_errors.AppError
    assert auth_errors.register_error_handlers is app_errors.register_error_handlers
    assert auth_errors.error_body is app_errors.error_body
    assert issubclass(auth_errors.UnauthenticatedError, app_errors.AppError)


@pytest.mark.parametrize(
    ("exc", "status", "code", "details"),
    [
        (ChangesetNotFound("c1"), 404, "not_found", {"resource": "changeset", "id": "c1"}),
        (BlockNotFound("b1"), 404, "not_found", {"resource": "block", "id": "b1"}),
        (ChangesetNotPending("expired"), 409, "changeset_not_pending", {"status": "expired"}),
        (BlockSuperseded("b1"), 409, "block_superseded", None),
        (BlockNotStarted("b1"), 409, "block_not_started", None),
        (InvalidMutation("not_found", "gone"), 404, "not_found", None),
        (InvalidMutation("project_archived", "a"), 409, "project_archived", None),
        (InvalidMutation("pin_exists", "dup"), 409, "pin_exists", None),
        (
            InvalidMutation("validation_failed", "min > max"),
            422,
            "validation_failed",
            {"fields": [{"path": "", "message": "min > max"}]},
        ),
        (PreferencesNotSet("none"), 404, "preferences_not_set", None),
    ],
)
def test_write_path_error_table(
    exc: Exception, status: int, code: str, details: dict[str, Any] | None
) -> None:
    err = write_path_error(exc)
    assert (err.status_code, err.code, err.details) == (status, code, details)


async def test_plan_verification_error_is_500_and_logs_no_user_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app_errors.register_error_handlers(app)
    register_write_path_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise PlanVerificationError([Violation(kind="overlap", message="Thesis 9-10am secret")])

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        with caplog.at_level(logging.ERROR):
            resp = await c.get("/boom")
    assert (resp.status_code, resp.json()["error"]["code"]) == (500, "internal_error")
    assert "overlap" in caplog.text and "secret" not in caplog.text


def test_parse_body_paths_drop_union_tags() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(ProjectDraft)
    with pytest.raises(AppError) as err:
        parse_body(
            adapter,
            {"name": "G", "kind": "deadline", "estimated_total_hours": 2, "max_block_minutes": 60},
            union_tags=frozenset({"deadline", "perennial"}),
        )
    assert err.value.status_code == 422
    assert [f["path"] for f in err.value.details["fields"]] == ["target_date"]  # type: ignore[index]
    with pytest.raises(AppError):
        parse_body(adapter, ["not", "an", "object"])
