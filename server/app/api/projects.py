"""Projects (`projects` tag): list/get read committed rows; create/patch/archive are staged.

D23: `staged` rows are invisible to reads (excluded from the list, 404 on get). D2 softer
option (per the T2.3 brief): a name-only PATCH applies directly via `apply_mutation`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.busy import Busy
from app.api.deps import (
    Maker,
    Now,
    RawBody,
    Tx,
    UserId,
    not_found,
    parse_body,
    validation_failed,
)
from app.api.mappers import project_out
from app.api.outbox import kick_outbox
from app.api.staging import manual, stage_request
from app.auth.dependencies import require_timezone
from app.db.models import Project
from app.db.models.staged import ProjectDraft, ProjectPatch, StagedMutation
from app.db.write_paths import apply_mutation, sweep_past_blocks
from app.errors import AppError

router = APIRouter(tags=["projects"], dependencies=[Depends(require_timezone)])

_DRAFT: TypeAdapter[Any] = TypeAdapter(ProjectDraft)
_PATCH: TypeAdapter[ProjectPatch] = TypeAdapter(ProjectPatch)
_OTHER_KIND_FIELDS = {
    "deadline": ("weekly_target_hours",),
    "perennial": ("target_date", "estimated_total_hours"),
}


async def _get(tx: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID) -> Project | None:
    return (
        await tx.execute(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _writable(tx: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID) -> Project:
    """404 if unknown/foreign, 409 `project_archived` if archived. A `staged` row passes: a
    write on a project the pending changeset is creating is carried forward with it (O9);
    any other staged row fails in staging as not_found."""
    row = await _get(tx, user_id, project_id)
    if row is None:
        raise not_found("project", project_id)
    if row.status == "archived":
        raise AppError(409, "project_archived", "This project is archived.")
    return row


@router.get("/projects", operation_id="listProjects")
async def list_projects(
    user_id: UserId, tx: Tx, now: Now, include_archived: bool = Query(False)
) -> dict[str, Any]:
    await sweep_past_blocks(tx, user_id, now)
    query = select(Project).where(Project.user_id == user_id, Project.status != "staged")
    if not include_archived:
        query = query.where(Project.status != "archived")
    rows = (await tx.execute(query.order_by(Project.created_at, Project.id))).scalars()
    return {"projects": [project_out(p) for p in rows]}


@router.post("/projects", operation_id="createProject", status_code=202)
async def create_project(
    user_id: UserId, maker: Maker, now: Now, busy: Busy, body: RawBody
) -> JSONResponse:
    draft = parse_body(_DRAFT, body, union_tags=frozenset({"deadline", "perennial"}))
    payload = {"project_id": str(uuid.uuid4()), "project": draft.model_dump(mode="json")}
    mutation = manual("project_created", payload)

    async def build(tx: AsyncSession) -> list[StagedMutation]:
        return [mutation]

    return await stage_request(maker, user_id, build, now=now, busy=busy)


@router.get("/projects/{project_id}", operation_id="getProject")
async def get_project(user_id: UserId, tx: Tx, now: Now, project_id: uuid.UUID) -> dict[str, Any]:
    await sweep_past_blocks(tx, user_id, now)
    row = await _get(tx, user_id, project_id)
    if row is None or row.status == "staged":
        raise not_found("project", project_id)
    return project_out(row)


@router.patch("/projects/{project_id}", operation_id="updateProject", status_code=202)
async def update_project(
    request: Request,
    user_id: UserId,
    maker: Maker,
    now: Now,
    busy: Busy,
    project_id: uuid.UUID,
    body: RawBody,
) -> JSONResponse:
    patch = parse_body(_PATCH, body)
    payload = {
        "project_id": str(project_id),
        "changes": patch.model_dump(mode="json", exclude_unset=True),
    }
    mutation = manual("project_updated", payload)

    async def build(tx: AsyncSession) -> list[StagedMutation] | JSONResponse:
        row = await _writable(tx, user_id, project_id)
        wrong = [f for f in _OTHER_KIND_FIELDS[row.kind] if f in patch.model_fields_set]
        if wrong:
            other = "perennial" if row.kind == "deadline" else "deadline"
            raise validation_failed(
                [{"path": f, "message": f"only valid when kind is '{other}'"} for f in wrong],
                f"Field not allowed for a {row.kind} project.",
            )
        if patch.model_fields_set == {"name"} and row.status != "staged":
            # D2 softer option: name is not a scheduling input → applied directly, 200 +
            # Project (updateProject 200, contracts 9679c03). No solve, so no busy read.
            # The rename re-pends the project's not-yet-ended live blocks (event titles,
            # T4.3a) in this transaction; the drain is kicked after commit, below.
            await apply_mutation(tx, user_id, mutation, now=now)
            updated = await _get(tx, user_id, project_id)
            assert updated is not None
            return JSONResponse(status_code=200, content=project_out(updated))
        return [mutation]

    response = await stage_request(maker, user_id, build, now=now, busy=busy)
    if response.status_code == 200:  # applied directly and committed: refresh event titles
        kick_outbox(request.app, user_id)
    return response


@router.delete("/projects/{project_id}", operation_id="archiveProject", status_code=202)
async def archive_project(
    user_id: UserId, maker: Maker, now: Now, busy: Busy, project_id: uuid.UUID
) -> JSONResponse:
    # TODO(contract): archiving a project whose creation is still pending is carried forward
    # (creation + archive confirm together) rather than dropping the creation.
    mutation = manual("project_archived", {"project_id": str(project_id)})

    async def build(tx: AsyncSession) -> list[StagedMutation]:
        await _writable(tx, user_id, project_id)
        return [mutation]

    return await stage_request(maker, user_id, build, now=now, busy=busy)
