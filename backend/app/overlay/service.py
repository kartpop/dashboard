"""Overlay service: sorts and groups the user's tasks into date buckets.

This is the ONLY place where merge, sort, and group logic lives. Since goal 17 the
tasks come from the local store (`app.tasks_store`) with `rank` / `group_id` already
on each row — the old overlay table is no longer joined. Routers stay thin.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.overlay.models import TaskGroup
from app.tasks_store import service as store
from app.tasks_store.models import Task

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")
_NO_DATE = "NO_DATE"
_OVERDUE = "OVERDUE"  # synthetic render-only bucket; rolls up all past-due dates

# Sentinel to distinguish "field not provided" from "explicitly None"
_UNSET: Any = object()


def _today_ist() -> date:
    return datetime.now(_IST).date()


def _bucket_key(due_str: str | None) -> str:
    """Canonical bucket key: YYYY-MM-DD (IST) or NO_DATE."""
    if not due_str:
        return _NO_DATE
    due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
    return due_dt.astimezone(_IST).date().isoformat()


def _bucket_key_to_label(bkey: str) -> str:
    if bkey == _NO_DATE:
        return "No date"
    d = date.fromisoformat(bkey)
    today = _today_ist()
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a, %b %-d")


def _bucket_key_sort_key(bkey: str) -> tuple:
    if bkey == _NO_DATE:
        return (1, date.max)
    return (0, date.fromisoformat(bkey))


def _date_sort_key(due_str: str | None) -> tuple:
    if not due_str:
        return (1, date.max)
    due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
    return (0, due_dt.astimezone(_IST).date())


def _merge_task(task: dict) -> dict:
    return {
        **task,
        "type": "task",
        "rank": task.get("rank"),
        "group_id": task.get("group_id"),
    }


def get_merged_task_lists(
    session: Session,
    user_id: int,
    raw_lists: list[dict],
    view: Literal["grouped", "flat"] = "grouped",
    show_completed: bool = False,
) -> list[dict]:
    """Sort/group THIS user's tasks (`raw_lists` from the task store)."""
    groups_by_list: dict[str, list[TaskGroup]] = {}
    for grp in session.exec(
        select(TaskGroup).where(TaskGroup.user_id == user_id)
    ).all():
        groups_by_list.setdefault(grp.tasklist_id, []).append(grp)

    result = []
    for task_list in raw_lists:
        list_id: str = task_list["id"]
        tasks: list[dict] = task_list["tasks"]

        if not show_completed:
            tasks = [t for t in tasks if t.get("status") != "completed"]

        merged = [_merge_task(t) for t in tasks]

        base = {k: v for k, v in task_list.items() if k != "tasks"}
        if view == "flat":
            ranked = sorted(
                [t for t in merged if t["rank"] is not None], key=lambda t: t["rank"]
            )
            unranked = [t for t in merged if t["rank"] is None]
            result.append({**base, "tasks": ranked + unranked})
        else:
            list_groups = groups_by_list.get(list_id, [])
            result.append({**base, "buckets": _build_buckets(merged, list_groups)})

    return result


def _build_buckets(
    tasks: list[dict], list_groups: list[TaskGroup]
) -> list[dict[str, Any]]:
    """Bucket tasks by due date, arranging each bucket as ordered items."""
    groups_by_bucket: dict[str, list[TaskGroup]] = {}
    for grp in list_groups:
        groups_by_bucket.setdefault(grp.bucket_key, []).append(grp)

    bucket_tasks: dict[str, list[dict]] = {}
    bucket_sort: dict[str, tuple] = {}

    for task in tasks:
        bkey = _bucket_key(task.get("due"))
        sk = _date_sort_key(task.get("due"))
        bucket_tasks.setdefault(bkey, []).append(task)
        bucket_sort[bkey] = sk

    # include buckets that have groups but no tasks
    for bkey in groups_by_bucket:
        if bkey not in bucket_sort:
            bucket_sort[bkey] = _bucket_key_sort_key(bkey)
            bucket_tasks[bkey] = []

    sorted_keys = sorted(bucket_sort, key=lambda k: bucket_sort[k])

    # Past-due items roll up into a single synthetic "Overdue" bucket at the top
    # rather than scattering across past-date buckets. Keys sort ascending by
    # date, so overdue items accumulate oldest-first. The rollup is render-only:
    # OVERDUE is not a real due-date, so it is never a drag/reschedule target —
    # past dates are reached via the date-picker. Groups keep their real
    # bucket_key, so group-scope / reschedule logic is unaffected.
    today = _today_ist()
    overdue_items: list[dict] = []
    result = []
    for bkey in sorted_keys:
        items = _build_bucket_items(
            bucket_tasks.get(bkey, []), groups_by_bucket.get(bkey, [])
        )
        if not items:
            continue
        if bkey != _NO_DATE and date.fromisoformat(bkey) < today:
            overdue_items.extend(items)
        else:
            result.append(
                {"label": _bucket_key_to_label(bkey), "key": bkey, "items": items}
            )

    if overdue_items:
        result.insert(0, {"label": "Overdue", "key": _OVERDUE, "items": overdue_items})

    return result


def _build_bucket_items(
    bucket_tasks: list[dict], groups: list[TaskGroup]
) -> list[dict]:
    """Build the ordered items list for a bucket (standalone tasks + groups)."""
    known_ids = {grp.id for grp in groups}

    tasks_by_group: dict[int, list[dict]] = {}
    standalone: list[dict] = []

    for task in bucket_tasks:
        gid = task.get("group_id")
        if gid is not None and gid in known_ids:
            tasks_by_group.setdefault(gid, []).append(task)
        else:
            standalone.append(task)

    group_items: list[dict] = []
    for grp in groups:
        t_list = tasks_by_group.get(grp.id, [])
        ranked_t = sorted(
            [t for t in t_list if t["rank"] is not None], key=lambda t: t["rank"]
        )
        unranked_t = [t for t in t_list if t["rank"] is None]
        group_items.append(
            {
                "type": "group",
                "id": grp.id,
                "name": grp.name,
                "rank": grp.rank,
                "items": ranked_t + unranked_t,
            }
        )

    ranked_s = sorted(
        [t for t in standalone if t["rank"] is not None], key=lambda t: t["rank"]
    )
    unranked_s = [t for t in standalone if t["rank"] is None]
    ranked_g = sorted(
        [g for g in group_items if g["rank"] is not None], key=lambda g: g["rank"]
    )
    unranked_g = [g for g in group_items if g["rank"] is None]

    # merge ranked standalone tasks and ranked groups by rank value
    items: list[dict] = []
    si, gi = 0, 0
    while si < len(ranked_s) and gi < len(ranked_g):
        if ranked_s[si]["rank"] <= ranked_g[gi]["rank"]:
            items.append(ranked_s[si])
            si += 1
        else:
            items.append(ranked_g[gi])
            gi += 1
    items.extend(ranked_s[si:])
    items.extend(ranked_g[gi:])
    items.extend(unranked_s)
    items.extend(unranked_g)

    return items


# ── Group CRUD ────────────────────────────────────────────────────────────────


def create_group(
    session: Session,
    user_id: int,
    tasklist_id: str,
    bucket_key: str,
    name: str,
    rank: float | None,
) -> TaskGroup:
    now = datetime.now(timezone.utc)
    grp = TaskGroup(
        user_id=user_id,
        tasklist_id=tasklist_id,
        bucket_key=bucket_key,
        name=name,
        rank=rank,
        created_at=now,
        updated_at=now,
    )
    session.add(grp)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise
    session.refresh(grp)
    return grp


def get_group(
    session: Session, user_id: int, group_id: int, tasklist_id: str
) -> TaskGroup | None:
    grp = session.get(TaskGroup, group_id)
    if grp is None or grp.user_id != user_id or grp.tasklist_id != tasklist_id:
        return None
    return grp


def update_group(
    session: Session,
    user_id: int,
    group_id: int,
    tasklist_id: str,
    name: str | None,
    rank: float | None,
) -> TaskGroup | None:
    grp = get_group(session, user_id, group_id, tasklist_id)
    if grp is None:
        return None
    now = datetime.now(timezone.utc)
    if name is not None:
        grp.name = name
    if rank is not None:
        grp.rank = rank
    grp.updated_at = now
    session.commit()
    session.refresh(grp)
    return grp


def delete_group(
    session: Session, user_id: int, group_id: int, tasklist_id: str
) -> bool:
    grp = get_group(session, user_id, group_id, tasklist_id)
    if grp is None:
        return False
    for task in session.exec(
        select(Task).where(Task.user_id == user_id, Task.group_id == group_id)
    ).all():
        task.group_id = None
    session.delete(grp)
    session.commit()
    return True


# ── Rank / group upsert (the old overlay write, now columns on the task) ─────


def upsert_overlay(
    session: Session,
    user_id: int,
    tasklist_id: str,
    task_id: str,
    rank: float | None = None,
    group_id: Any = _UNSET,
) -> Task | None:
    """Set rank and/or group_id on a task. Pass group_id=None to explicitly ungroup.
    Returns None when the task doesn't exist for this user."""
    task = store.get_task(session, user_id, tasklist_id, task_id)
    if task is None:
        return None
    if rank is not None:
        task.rank = rank
    if group_id is not _UNSET:
        task.group_id = group_id
    task.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(task)
    return task
