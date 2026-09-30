import logging
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Query
from google.oauth2.credentials import Credentials
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.auth.deps import get_current_credentials, get_current_user
from app.auth.models import User
from app.db import get_session
from app.errors import ApiError
from app.google import tasks as tasks_client
from app.overlay import service as overlay_svc
from app.threads import service as threads_svc
from app.writes import service as writes_svc

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/tasks")
async def list_tasks(
    view: Annotated[Literal["grouped", "flat"], Query()] = "grouped",
    show_completed: Annotated[bool, Query()] = False,
    user: User = Depends(get_current_user),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    try:
        raw_lists = await tasks_client.get_task_lists(creds)
    except Exception as exc:
        logger.exception("Google Tasks fetch failed: %s", exc)
        raise ApiError(
            502, "google_tasks_unavailable", "Could not fetch Google Tasks."
        ) from exc

    task_lists = overlay_svc.get_merged_task_lists(
        session, user.id, raw_lists, view=view, show_completed=show_completed
    )
    return {"task_lists": task_lists}


class OverlayUpdate(BaseModel):
    rank: Optional[float] = None
    group_id: Optional[int] = None  # None = ungroup when explicitly in body


@router.patch("/tasks/{tasklist_id}/{task_id}/overlay")
async def update_overlay(
    tasklist_id: str,
    task_id: str,
    body: OverlayUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    group_id_provided = "group_id" in body.model_fields_set
    if body.rank is None and not group_id_provided:
        raise ApiError(400, "no_fields", "Provide at least one of rank or group_id.")
    row = overlay_svc.upsert_overlay(
        session,
        user.id,
        tasklist_id=tasklist_id,
        task_id=task_id,
        rank=body.rank,
        group_id=body.group_id if group_id_provided else overlay_svc._UNSET,
    )
    return {
        "tasklist_id": row.tasklist_id,
        "task_id": row.task_id,
        "rank": row.rank,
        "group_id": row.group_id,
    }


# ── Google write commands (goal 4) ────────────────────────────────────────────


class RescheduleRequest(BaseModel):
    due_date: Optional[str] = None  # "YYYY-MM-DD" (IST bucket key) or null = NO_DATE
    rank: Optional[float] = None
    group_id: Optional[int] = None  # destination group, or null for standalone


class MoveRequest(BaseModel):
    target_list_id: str
    rank: Optional[float] = None
    # Goal 6 cross-list drag: an omitted due_date preserves the source due; an
    # explicit value (or null → NO_DATE) reschedules on the insert leg. group_id
    # names the destination group (validated against the dest bucket) or null.
    due_date: Optional[str] = None
    group_id: Optional[int] = None


@router.post("/tasks/{tasklist_id}/{task_id}/reschedule")
async def reschedule_task(
    tasklist_id: str,
    task_id: str,
    body: RescheduleRequest,
    user: User = Depends(get_current_user),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    return await writes_svc.reschedule(
        session,
        creds,
        user.id,
        tasklist_id=tasklist_id,
        task_id=task_id,
        due_date=body.due_date,
        rank=body.rank,
        group_id=body.group_id,
    )


@router.post("/tasks/{tasklist_id}/{task_id}/move")
async def move_task(
    tasklist_id: str,
    task_id: str,
    body: MoveRequest,
    user: User = Depends(get_current_user),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    fields = body.model_fields_set
    result = await writes_svc.move(
        session,
        creds,
        user.id,
        tasklist_id=tasklist_id,
        task_id=task_id,
        target_list_id=body.target_list_id,
        rank=body.rank,
        due_date=body.due_date if "due_date" in fields else writes_svc._UNSET,
        group_id=body.group_id,
    )
    # A move re-mints the task id (insert-then-delete); a thread step linked to the
    # old id must follow it, or the next reconcile reads it as deleted (goal 14).
    threads_svc.repoint_link(
        session,
        user.id,
        tasklist_id,
        task_id,
        body.target_list_id,
        result["new_task_id"],
    )
    return result


# ── Task content CRUD (goal 4a) ────────────────────────────────────────────────


class TaskCreate(BaseModel):
    title: str
    rank: Optional[float] = None  # top-of-bucket rank computed by the client
    notes: Optional[str] = None
    due_date: Optional[str] = None  # "YYYY-MM-DD"


class TaskContentUpdate(BaseModel):
    # All optional; "field omitted" vs "explicitly null" is read from
    # model_fields_set (the goal-3 PATCH partial-update convention).
    title: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None  # "needsAction" | "completed"


class ListRename(BaseModel):
    title: str


@router.post("/tasks/{tasklist_id}", status_code=201)
async def create_task(
    tasklist_id: str,
    body: TaskCreate,
    user: User = Depends(get_current_user),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    return await writes_svc.create_task(
        session,
        creds,
        user.id,
        tasklist_id=tasklist_id,
        title=body.title,
        rank=body.rank,
        notes=body.notes,
        due_date=body.due_date,
    )


@router.patch("/tasks/{tasklist_id}/{task_id}")
async def update_task(
    tasklist_id: str,
    task_id: str,
    body: TaskContentUpdate,
    user: User = Depends(get_current_user),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    fields = body.model_fields_set
    if not fields & {"title", "notes", "status"}:
        raise ApiError(
            400, "no_fields", "Provide at least one of title, notes, or status."
        )
    return await writes_svc.update_content(
        session,
        creds,
        user.id,
        tasklist_id=tasklist_id,
        task_id=task_id,
        title=body.title if "title" in fields else writes_svc._UNSET,
        notes=body.notes if "notes" in fields else writes_svc._UNSET,
        status=body.status if "status" in fields else writes_svc._UNSET,
    )


@router.delete("/tasks/{tasklist_id}/{task_id}")
async def delete_task(
    tasklist_id: str,
    task_id: str,
    user: User = Depends(get_current_user),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    return await writes_svc.delete(
        session, creds, user.id, tasklist_id=tasklist_id, task_id=task_id
    )


@router.patch("/lists/{tasklist_id}")
async def rename_list(
    tasklist_id: str,
    body: ListRename,
    creds: Credentials = Depends(get_current_credentials),
):
    return await writes_svc.rename_list(creds, tasklist_id, body.title)


# ── Group CRUD ────────────────────────────────────────────────────────────────


class GroupCreate(BaseModel):
    name: str
    bucket_key: str
    rank: Optional[float] = None


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    rank: Optional[float] = None


def _group_response(grp) -> dict:
    return {
        "id": grp.id,
        "tasklist_id": grp.tasklist_id,
        "bucket_key": grp.bucket_key,
        "name": grp.name,
        "rank": grp.rank,
    }


@router.post("/tasks/{tasklist_id}/groups", status_code=201)
async def create_group(
    tasklist_id: str,
    body: GroupCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        grp = overlay_svc.create_group(
            session,
            user.id,
            tasklist_id=tasklist_id,
            bucket_key=body.bucket_key,
            name=body.name,
            rank=body.rank,
        )
    except IntegrityError:
        raise ApiError(
            409, "group_exists", "A group with that name already exists in this bucket."
        )
    return _group_response(grp)


@router.patch("/tasks/{tasklist_id}/groups/{group_id}")
async def update_group(
    tasklist_id: str,
    group_id: int,
    body: GroupUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if body.name is None and body.rank is None:
        raise ApiError(400, "no_fields", "Provide at least one of name or rank.")
    grp = overlay_svc.update_group(
        session,
        user.id,
        group_id=group_id,
        tasklist_id=tasklist_id,
        name=body.name,
        rank=body.rank,
    )
    if grp is None:
        raise ApiError(404, "not_found", "Group not found.")
    return _group_response(grp)


@router.delete("/tasks/{tasklist_id}/groups/{group_id}")
async def delete_group(
    tasklist_id: str,
    group_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ok = overlay_svc.delete_group(
        session, user.id, group_id=group_id, tasklist_id=tasklist_id
    )
    if not ok:
        raise ApiError(404, "not_found", "Group not found.")
    return {"ok": True}
