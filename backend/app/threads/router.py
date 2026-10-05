"""Threads API (goal 14) — thin; every call is scoped to `current_user.id`.

All mutations return the updated `Thread` so the client can settle its optimistic
state from the response. Errors are `ApiError`s (the `{"error": {...}}` envelope).
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from app.auth.deps import get_current_user
from app.auth.models import User
from app.db import get_session
from app.tasks_store.deps import tasks_ready
from app.threads import service as threads_svc

router = APIRouter()

ListKey = Literal["mine", "follow"]


class ThreadCreate(BaseModel):
    title: str


class ThreadUpdate(BaseModel):
    title: Optional[str] = None
    archived: Optional[bool] = None


class StepCreate(BaseModel):
    label: str
    note: Optional[str] = None
    occurred_on: Optional[date] = None


class NextCreate(BaseModel):
    label: str
    list: ListKey
    due: Optional[date] = None
    note: Optional[str] = None


class StepUpdate(BaseModel):
    # Partial update: an omitted field is untouched (read from model_fields_set).
    label: Optional[str] = None
    note: Optional[str] = None
    occurred_on: Optional[date] = None
    due: Optional[date] = None  # explicit null clears the next step's due
    list: Optional[ListKey] = None


@router.get("/threads")
async def list_threads(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return {"threads": await threads_svc.list_threads(session, user.id)}


@router.post("/threads", status_code=201)
async def create_thread(
    body: ThreadCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return threads_svc.create_thread(session, user.id, body.title)


@router.patch("/threads/{thread_id}")
async def update_thread(
    thread_id: int,
    body: ThreadUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return threads_svc.update_thread(
        session, user.id, thread_id, title=body.title, archived=body.archived
    )


@router.post("/threads/{thread_id}/steps", status_code=201)
async def add_step(
    thread_id: int,
    body: StepCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return threads_svc.add_step(
        session,
        user.id,
        thread_id,
        label=body.label,
        note=body.note,
        occurred_on=body.occurred_on,
    )


@router.post("/threads/{thread_id}/next", status_code=201)
async def set_next(
    thread_id: int,
    body: NextCreate,
    user: User = Depends(tasks_ready),
    session: Session = Depends(get_session),
):
    return await threads_svc.set_next(
        session,
        user.id,
        thread_id,
        label=body.label,
        list_key=body.list,
        due=body.due,
        note=body.note,
    )


@router.patch("/threads/{thread_id}/steps/{step_id}")
async def update_step(
    thread_id: int,
    step_id: int,
    body: StepUpdate,
    user: User = Depends(tasks_ready),
    session: Session = Depends(get_session),
):
    fields = body.model_fields_set
    unset = threads_svc._UNSET
    return await threads_svc.update_step(
        session,
        user.id,
        thread_id,
        step_id,
        label=body.label if "label" in fields else unset,
        note=body.note if "note" in fields else unset,
        occurred_on=body.occurred_on if "occurred_on" in fields else unset,
        due=body.due if "due" in fields else unset,
        list_key=body.list if "list" in fields else unset,
    )


@router.post("/threads/{thread_id}/steps/{step_id}/complete")
async def complete_step(
    thread_id: int,
    step_id: int,
    user: User = Depends(tasks_ready),
    session: Session = Depends(get_session),
):
    return await threads_svc.complete_step(session, user.id, thread_id, step_id)


@router.delete("/threads/{thread_id}/steps/{step_id}")
async def delete_step(
    thread_id: int,
    step_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return threads_svc.delete_step(session, user.id, thread_id, step_id)
