"""Goal 14 — Threads: storage, reconcile, link writes, isolation, write surface.

Goal 17: tasks live in the local store, so the writes service runs for real against
the DB. The `tasks` fixture seeds user A's three lists (two pinned + one other);
"the task changed elsewhere" is a direct `Task` row edit (or a `/tasks/...` call)
followed by `GET /threads`, which reconciles. The `writes` spy records every
writes-service call the threads service makes.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, timezone

import pytest
from sqlmodel import select

from app.errors import ApiError
from app.threads import router as threads_router
from app.threads import service as threads_svc
from app.threads.models import Thread, ThreadStep
from app.writes import service as writes_mod
from tests.conftest import FOLLOW, MINE


@pytest.fixture
def tasks(store, user_a):
    """User A's three lists (My Tasks, Follow-ups, Groceries) in the local store."""
    store.lists(user_a)
    return store


@pytest.fixture
def writes(monkeypatch) -> list[tuple[str, dict]]:
    """Record (name, kwargs) for every task write, still running the real one."""
    calls: list[tuple[str, dict]] = []

    def _spy(name, real):
        async def spy(*args, **kwargs):
            calls.append((name, kwargs))
            return await real(*args, **kwargs)

        return spy

    for name in ("create_task", "update_content", "reschedule", "move", "delete"):
        monkeypatch.setattr(writes_mod, name, _spy(name, getattr(writes_mod, name)))
    return calls


def _names(calls) -> list[str]:
    return [c[0] for c in calls]


def _set(store, task_id: str, **fields) -> None:
    """Edit a task row directly — "changed elsewhere" (another tab, the phone)."""
    task = store.get(task_id)
    for k, v in fields.items():
        setattr(task, k, v)
    store.session.commit()


def _complete(store, task_id: str, when: datetime | None) -> None:
    _set(store, task_id, status="completed", completed_at=when)


def _reopen(store, task_id: str) -> None:
    _set(store, task_id, status="needsAction", completed_at=None)


def _delete(store, task_id: str) -> None:
    store.session.delete(store.get(task_id))
    store.session.commit()


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _thread(client, title="Partner pilot") -> dict:
    r = client.post("/threads", json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()


def _log(client, tid, label, **extra) -> dict:
    r = client.post(f"/threads/{tid}/steps", json={"label": label, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _next(client, tid, label="Nudge them", lst="follow", due="2026-10-01", **extra):
    return client.post(
        f"/threads/{tid}/next",
        json={"label": label, "list": lst, "due": due, **extra},
    )


def _get(client) -> list[dict]:
    r = client.get("/threads")
    assert r.status_code == 200, r.text
    return r.json()["threads"]


def _one(client, tid) -> dict:
    return next(t for t in _get(client) if t["id"] == tid)


# ── Storage + API contract ────────────────────────────────────────────────────


def test_create_log_and_set_follow_up_next(client, tasks):
    t = _thread(client)
    assert t["steps"] == [] and t["archived"] is False
    _log(client, t["id"], "Intro call", occurred_on="2026-09-01")
    _log(client, t["id"], "Shared the wiki", note="asked for feedback")
    r = _next(client, t["id"], note="ask who owns it")
    assert r.status_code == 201, r.text
    body = r.json()
    kinds = [s["kind"] for s in body["steps"]]
    assert kinds == ["done", "done", "next"]
    nxt = body["steps"][-1]
    assert nxt["list"] == "follow" and nxt["via"] is None
    assert nxt["due"] == "2026-10-01" and nxt["tasklist_id"] == FOLLOW
    # A real task exists in Follow-ups with the note as its description.
    task = tasks.get(nxt["task_id"])
    assert task.tasklist_id == FOLLOW and task.status == "needsAction"
    assert task.title == "Nudge them"
    assert task.notes == "ask who owns it"
    assert task.due == date(2026, 10, 1)
    assert body["steps"][1]["note"] == "asked for feedback"
    assert body["last_moved_on"] >= "2026-09-01"


def test_several_open_steps_ordered_by_due(client, tasks, user_a):
    """Goal 14a: a second and third open step succeed; the open block is served by
    due ascending (undated last), after every done step."""
    t = _thread(client)
    _log(client, t["id"], "Shortlisted three")
    assert (
        _next(client, t["id"], "Visit NGO 2", "mine", "2026-10-08").status_code == 201
    )
    assert _next(client, t["id"], "Email NGO 3", "follow", None).status_code == 201
    r = _next(client, t["id"], "Visit NGO 1", "mine", "2026-10-05")
    assert r.status_code == 201, r.text
    steps = r.json()["steps"]
    assert [(s["kind"], s["label"]) for s in steps] == [
        ("done", "Shortlisted three"),
        ("next", "Visit NGO 1"),
        ("next", "Visit NGO 2"),
        ("next", "Email NGO 3"),
    ]
    assert len({s["task_id"] for s in steps[1:]}) == 3
    assert len(tasks.tasks(user_a, MINE)) == 2
    assert len(tasks.tasks(user_a, FOLLOW)) == 1
    # Logging still lands before the whole open block.
    body = _log(client, t["id"], "Called NGO 1")
    assert [s["kind"] for s in body["steps"]] == [
        "done",
        "done",
        "next",
        "next",
        "next",
    ]
    assert body["steps"][1]["label"] == "Called NGO 1"


def test_log_step_inserts_before_next(client, tasks):
    t = _thread(client)
    _log(client, t["id"], "First")
    _next(client, t["id"])
    body = _log(client, t["id"], "Second")
    assert [(s["kind"], s["label"]) for s in body["steps"]] == [
        ("done", "First"),
        ("done", "Second"),
        ("next", "Nudge them"),
    ]
    # And on an empty-history thread with only a next step.
    t2 = _thread(client, "Other")
    _next(client, t2["id"], label="Only next", lst="mine")
    body = _log(client, t2["id"], "Happened")
    assert [s["kind"] for s in body["steps"]] == ["done", "next"]


def test_log_defaults_to_today_ist(client, tasks, monkeypatch):
    monkeypatch.setattr(threads_svc, "today_ist", lambda: date(2026, 9, 29))
    t = _thread(client)
    body = _log(client, t["id"], "Did a thing")
    assert body["steps"][0]["occurred_on"] == "2026-09-29"
    assert body["last_moved_on"] == "2026-09-29"


def test_delete_next_unlinks_and_leaves_task(client, tasks, writes):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.delete(f"/threads/{t['id']}/steps/{nxt['id']}")
    assert r.status_code == 200
    assert r.json()["unlinked"] is True
    assert r.json()["steps"] == []
    assert "delete" not in _names(writes)
    task = tasks.get(nxt["task_id"])
    assert task is not None and task.status == "needsAction"
    assert task.tasklist_id == FOLLOW


def test_delete_done_step(client, tasks):
    t = _thread(client)
    body = _log(client, t["id"], "Oops")
    r = client.delete(f"/threads/{t['id']}/steps/{body['steps'][0]['id']}")
    assert r.status_code == 200 and r.json()["unlinked"] is False
    assert r.json()["steps"] == []


def test_archive_and_restore_leave_task_alone(client, tasks, writes):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    before = list(writes)
    r = client.patch(f"/threads/{t['id']}", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True
    assert writes == before  # archive never writes a task
    task = tasks.get(nxt["task_id"])
    assert task is not None and task.status == "needsAction"
    r = client.patch(f"/threads/{t['id']}", json={"archived": False, "title": "New"})
    assert r.json()["archived"] is False and r.json()["title"] == "New"


def test_empty_title_and_label_rejected(client, tasks):
    assert client.post("/threads", json={"title": "  "}).status_code == 400
    t = _thread(client)
    assert (
        client.post(f"/threads/{t['id']}/steps", json={"label": ""}).status_code == 400
    )


def test_create_task_failure_writes_no_step(client, tasks, session, monkeypatch):
    t = _thread(client)

    async def boom(*args, **kwargs):
        raise ApiError(503, "boom", "create failed")

    monkeypatch.setattr(writes_mod, "create_task", boom)
    r = _next(client, t["id"])
    assert r.status_code == 503
    assert session.exec(select(ThreadStep)).all() == []


def test_missing_pinned_list_422(client, store, user_a, writes):
    store.lists(user_a, {MINE: "My Tasks", "L_OTHER": "Groceries"})
    t = _thread(client)
    r = _next(client, t["id"])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "pinned_list_missing"
    assert "create_task" not in _names(writes)
    assert store.tasks(user_a) == []


# ── Next-step edits (writes) ──────────────────────────────────────────────────


def test_patch_next_label_note_update_task(client, tasks):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.patch(
        f"/threads/{t['id']}/steps/{nxt['id']}",
        json={"label": "Nudge #2", "note": "try their lead"},
    )
    assert r.status_code == 200, r.text
    step = r.json()["steps"][-1]
    assert step["label"] == "Nudge #2" and step["note"] == "try their lead"
    task = tasks.get(nxt["task_id"])
    assert task.title == "Nudge #2" and task.notes == "try their lead"


def test_patch_next_unchanged_fields_skip_writes(client, tasks, writes):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    before = len(writes)
    r = client.patch(
        f"/threads/{t['id']}/steps/{nxt['id']}",
        json={"label": nxt["label"], "note": nxt["note"], "due": nxt["due"]},
    )
    assert r.status_code == 200
    assert len(writes) == before


def test_patch_next_due_reschedules(client, tasks, writes):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.patch(
        f"/threads/{t['id']}/steps/{nxt['id']}", json={"due": "2026-10-09"}
    )
    assert r.json()["steps"][-1]["due"] == "2026-10-09"
    assert _names(writes)[-1] == "reschedule"
    assert tasks.get(nxt["task_id"]).due == date(2026, 10, 9)


def test_patch_next_list_moves_and_keeps_id(client, tasks, user_a, writes):
    t = _thread(client)
    nxt = _next(client, t["id"], note="n").json()["steps"][-1]
    r = client.patch(
        f"/threads/{t['id']}/steps/{nxt['id']}",
        json={"list": "mine", "due": "2026-10-05"},
    )
    assert r.status_code == 200, r.text
    assert _names(writes)[-1] == "move" and "reschedule" not in _names(writes)
    step = r.json()["steps"][-1]
    assert step["kind"] == "next" and step["list"] == "mine"
    # Goal 17: a move is in place — same task id, new list.
    assert step["tasklist_id"] == MINE and step["task_id"] == nxt["task_id"]
    assert step["due"] == "2026-10-05"
    assert tasks.tasks(user_a, FOLLOW) == []
    moved = tasks.get(nxt["task_id"])
    assert moved.tasklist_id == MINE
    assert moved.notes == "n" and moved.due == date(2026, 10, 5)
    # And the next reconcile still sees it as the live next step.
    after = _one(client, t["id"])["steps"][-1]
    assert after["kind"] == "next" and after["list"] == "mine"
    assert (after["tasklist_id"], after["task_id"]) == (MINE, nxt["task_id"])


def test_patch_done_step_is_local(client, tasks, writes):
    t = _thread(client)
    step = _log(client, t["id"], "Call")["steps"][0]
    r = client.patch(
        f"/threads/{t['id']}/steps/{step['id']}",
        json={"label": "Intro call", "note": "x", "occurred_on": "2026-09-02"},
    )
    s = r.json()["steps"][0]
    assert (s["label"], s["note"], s["occurred_on"]) == (
        "Intro call",
        "x",
        "2026-09-02",
    )
    assert writes == []


def test_complete_endpoint_flips_and_completes_task(client, tasks, monkeypatch):
    monkeypatch.setattr(threads_svc, "today_ist", lambda: date(2026, 9, 29))
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.post(f"/threads/{t['id']}/steps/{nxt['id']}/complete")
    assert r.status_code == 200, r.text
    step = r.json()["steps"][-1]
    assert step["kind"] == "done" and step["via"] == "follow"
    assert step["occurred_on"] == "2026-09-29" and step["list"] is None
    task = tasks.get(nxt["task_id"])
    assert task.status == "completed" and task.completed_at is not None
    # The next reconcile agrees (no flip-back, still dated by the endpoint).
    after = _one(client, t["id"])["steps"][-1]
    assert after["kind"] == "done" and after["occurred_on"] == "2026-09-29"
    # Completing a done step is refused.
    again = client.post(f"/threads/{t['id']}/steps/{nxt['id']}/complete")
    assert again.status_code == 400


# ── Reconcile ─────────────────────────────────────────────────────────────────


def test_reconcile_completed_becomes_done(client, tasks):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    _set(
        tasks,
        nxt["task_id"],
        status="completed",
        title="Sent reminder #2",
        notes="no reply yet",
        # 20:00 UTC on the 30th is the 1st in IST.
        completed_at=_utc(2026, 9, 30, 20, 0),
    )
    step = _one(client, t["id"])["steps"][-1]
    assert step["kind"] == "done"
    assert step["occurred_on"] == "2026-10-01"
    assert (step["label"], step["note"], step["via"]) == (
        "Sent reminder #2",
        "no reply yet",
        "follow",
    )
    assert step["task_id"] == nxt["task_id"]  # link retained on the done step


def test_reconcile_completed_via_tasks_endpoint(client, tasks):
    """Completing through the Tasks panel (`PATCH /tasks/...`) flips the step on the
    next reconcile."""
    t = _thread(client)
    nxt = _next(client, t["id"], lst="mine").json()["steps"][-1]
    r = client.patch(f"/tasks/{MINE}/{nxt['task_id']}", json={"status": "completed"})
    assert r.status_code == 200, r.text
    step = _one(client, t["id"])["steps"][-1]
    assert step["kind"] == "done" and step["via"] == "mine"
    # Undo from the panel reopens it.
    r = client.patch(f"/tasks/{MINE}/{nxt['task_id']}", json={"status": "needsAction"})
    assert r.status_code == 200, r.text
    step = _one(client, t["id"])["steps"][-1]
    assert step["kind"] == "next" and step["list"] == "mine"


def test_reconcile_completed_without_timestamp_falls_back_to_today(
    client, tasks, monkeypatch
):
    monkeypatch.setattr(threads_svc, "today_ist", lambda: date(2026, 9, 29))
    t = _thread(client)
    nxt = _next(client, t["id"], lst="mine").json()["steps"][-1]
    _complete(tasks, nxt["task_id"], None)
    step = _one(client, t["id"])["steps"][-1]
    assert step["occurred_on"] == "2026-09-29" and step["via"] == "mine"


def test_reconcile_uncompleted_last_step_is_next_again(client, tasks):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    _complete(tasks, nxt["task_id"], _utc(2026, 9, 29, 5, 0))
    assert _one(client, t["id"])["steps"][-1]["kind"] == "done"
    _reopen(tasks, nxt["task_id"])
    step = _one(client, t["id"])["steps"][-1]
    assert step["kind"] == "next" and step["occurred_on"] is None
    assert step["list"] == "follow" and step["due"] == "2026-10-01"


def test_reconcile_uncompleted_not_last_rejoins_open_block(client, tasks):
    """Goal 14a: a reopened task is open again even with later history, and moves
    after every done step."""
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    _complete(tasks, nxt["task_id"], _utc(2026, 9, 29, 5, 0))
    _get(client)
    _log(client, t["id"], "Later update")
    _reopen(tasks, nxt["task_id"])
    steps = _one(client, t["id"])["steps"]
    assert [(s["kind"], s["label"]) for s in steps] == [
        ("done", "Later update"),
        ("next", "Nudge them"),
    ]


def test_reconcile_uncompleted_with_other_open_is_open_too(client, tasks):
    t = _thread(client)
    first = _next(client, t["id"]).json()["steps"][-1]
    _complete(tasks, first["task_id"], _utc(2026, 9, 29, 5, 0))
    _get(client)
    _next(client, t["id"], label="New next", lst="mine", due="2026-10-09")
    _reopen(tasks, first["task_id"])
    steps = _one(client, t["id"])["steps"]
    assert [(s["kind"], s["label"]) for s in steps] == [
        ("next", "Nudge them"),  # due 2026-10-01, sorts first in the open block
        ("next", "New next"),
    ]


def _three_open(client):
    t = _thread(client, "NGO visit")
    _log(client, t["id"], "Shortlisted three")
    for label, lst, due in (
        ("Visit NGO 1", "mine", "2026-10-05"),
        ("Email NGO 3", "follow", "2026-10-06"),
        ("Visit NGO 2", "mine", "2026-10-08"),
    ):
        assert _next(client, t["id"], label, lst, due).status_code == 201
    steps = _one(client, t["id"])["steps"]
    return t, {s["label"]: s for s in steps if s["kind"] == "next"}


def test_reconcile_one_of_three_completed_joins_history(client, tasks):
    t, opened = _three_open(client)
    _complete(tasks, opened["Email NGO 3"]["task_id"], _utc(2026, 10, 2, 5, 0))
    steps = _one(client, t["id"])["steps"]
    assert [(s["kind"], s["label"]) for s in steps] == [
        ("done", "Shortlisted three"),
        ("done", "Email NGO 3"),
        ("next", "Visit NGO 1"),
        ("next", "Visit NGO 2"),
    ]
    assert steps[1]["via"] == "follow"


def test_reconcile_deleting_one_of_three_keeps_siblings(client, tasks):
    t, opened = _three_open(client)
    _delete(tasks, opened["Visit NGO 1"]["task_id"])
    steps = _one(client, t["id"])["steps"]
    assert [s["label"] for s in steps if s["kind"] == "next"] == [
        "Email NGO 3",
        "Visit NGO 2",
    ]


def test_reconcile_all_completed_dangles_in_completion_order(client, tasks):
    t, opened = _three_open(client)
    # One completes and reconciles; the other two complete before the next poll.
    _complete(tasks, opened["Visit NGO 2"]["task_id"], _utc(2026, 10, 2, 5, 0))
    _get(client)
    _complete(tasks, opened["Visit NGO 1"]["task_id"], _utc(2026, 10, 4, 5, 0))
    _complete(tasks, opened["Email NGO 3"]["task_id"], _utc(2026, 10, 3, 5, 0))
    steps = _one(client, t["id"])["steps"]
    assert all(s["kind"] == "done" for s in steps)  # dangling now
    assert [s["label"] for s in steps] == [
        "Shortlisted three",
        "Visit NGO 2",
        "Email NGO 3",
        "Visit NGO 1",
    ]
    # Reopening the middle one returns it to the (now one-step) open block.
    _reopen(tasks, opened["Email NGO 3"]["task_id"])
    steps = _one(client, t["id"])["steps"]
    assert [(s["kind"], s["label"]) for s in steps][-1] == ("next", "Email NGO 3")


def test_complete_endpoint_on_one_of_three(client, tasks, monkeypatch):
    monkeypatch.setattr(threads_svc, "today_ist", lambda: date(2026, 10, 2))
    t, opened = _three_open(client)
    sid = opened["Visit NGO 2"]["id"]
    r = client.post(f"/threads/{t['id']}/steps/{sid}/complete")
    assert r.status_code == 200, r.text
    assert [(s["kind"], s["label"]) for s in r.json()["steps"]] == [
        ("done", "Shortlisted three"),
        ("done", "Visit NGO 2"),
        ("next", "Visit NGO 1"),
        ("next", "Email NGO 3"),
    ]
    # Then log an update: it lands after the completion, before the open block.
    body = _log(client, t["id"], "Debrief")
    assert [s["label"] for s in body["steps"]][:3] == [
        "Shortlisted three",
        "Visit NGO 2",
        "Debrief",
    ]


def test_delete_one_of_three_unlinks_only_it(client, tasks, writes):
    t, opened = _three_open(client)
    victim = opened["Email NGO 3"]
    r = client.delete(f"/threads/{t['id']}/steps/{victim['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["unlinked"] is True
    assert [s["label"] for s in r.json()["steps"] if s["kind"] == "next"] == [
        "Visit NGO 1",
        "Visit NGO 2",
    ]
    assert tasks.get(victim["task_id"]) is not None
    assert "delete" not in _names(writes)


def test_one_task_linked_once_across_threads(session, user_a):
    """The per-user one-link-per-task index still holds."""
    from sqlalchemy.exc import IntegrityError

    a = Thread(user_id=user_a.id, title="A")
    b = Thread(user_id=user_a.id, title="B")
    session.add_all([a, b])
    session.commit()
    for t in (a, b):
        session.add(
            ThreadStep(
                thread_id=t.id,
                user_id=user_a.id,
                position=1000.0,
                kind="next",
                label="Same task",
                tasklist_id=MINE,
                task_id="T_SHARED",
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_reconcile_deleted_removes_next(client, tasks):
    t = _thread(client)
    _log(client, t["id"], "History")
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.delete(f"/tasks/{FOLLOW}/{nxt['task_id']}")
    assert r.status_code == 200, r.text
    steps = _one(client, t["id"])["steps"]
    assert [s["kind"] for s in steps] == ["done"]  # dangling now


def test_reconcile_open_refreshes_cache(client, tasks):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    _set(
        tasks,
        nxt["task_id"],
        title="Edited on phone",
        notes="phone note",
        due=date(2026, 10, 12),
    )
    step = _one(client, t["id"])["steps"][-1]
    assert (step["kind"], step["label"], step["note"], step["due"]) == (
        "next",
        "Edited on phone",
        "phone note",
        "2026-10-12",
    )


def test_dashboard_move_keeps_link(client, tasks):
    """Goal 17: a cross-list move via the tasks endpoint keeps the task id, so the
    thread badge link survives with no repoint; reconcile picks up the new list."""
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.post(
        f"/tasks/{FOLLOW}/{nxt['task_id']}/move", json={"target_list_id": MINE}
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_task_id"] == nxt["task_id"]
    step = _one(client, t["id"])["steps"][-1]
    assert step["kind"] == "next"
    assert step["id"] == nxt["id"] and step["task_id"] == nxt["task_id"]
    assert (step["tasklist_id"], step["list"]) == (MINE, "mine")


def test_reconcile_skips_store_when_nothing_linked(client, tasks, monkeypatch):
    t = _thread(client)
    _log(client, t["id"], "Local only")

    def boom(*args, **kwargs):
        raise AssertionError("store read with no linked step")

    monkeypatch.setattr("app.tasks_store.service.get_task_lists", boom)
    assert [s["label"] for s in _one(client, t["id"])["steps"]] == ["Local only"]


def test_not_yet_imported_serves_cached_steps(auth, session, user_a):
    """Before the one-time import the store is empty: reconcile is skipped and the
    cached steps are served, none removed."""
    user_a.tasks_imported_at = None
    session.add(user_a)
    session.commit()
    thread = Thread(user_id=user_a.id, title="Pre-import")
    session.add(thread)
    session.commit()
    session.add_all(
        [
            ThreadStep(
                thread_id=thread.id,
                user_id=user_a.id,
                position=1000.0,
                kind="done",
                label="History",
                occurred_on=date(2026, 9, 1),
                tasklist_id=FOLLOW,
                task_id="G_DONE",
                via="follow",
            ),
            ThreadStep(
                thread_id=thread.id,
                user_id=user_a.id,
                position=2000.0,
                kind="next",
                label="Cached next",
                tasklist_id=MINE,
                task_id="G_OPEN",
                via="mine",
                due=date(2026, 10, 1),
            ),
        ]
    )
    session.commit()

    client = auth.as_user(user_a)
    for _ in range(2):  # twice: the first GET must not have deleted anything
        steps = _one(client, thread.id)["steps"]
        assert [(s["kind"], s["label"], s["task_id"]) for s in steps] == [
            ("done", "History", "G_DONE"),
            ("next", "Cached next", "G_OPEN"),
        ]
    assert len(session.exec(select(ThreadStep)).all()) == 2


def test_list_orders_steps_and_includes_archived(client, tasks):
    a = _thread(client, "A")
    b = _thread(client, "B")
    client.patch(f"/threads/{b['id']}", json={"archived": True})
    threads = _get(client)
    assert {t["title"]: t["archived"] for t in threads} == {"A": False, "B": True}
    assert a["created_at"]


# ── Isolation ─────────────────────────────────────────────────────────────────


def test_two_user_isolation_on_every_endpoint(auth, user_a, user_b, tasks):
    ca = auth.as_user(user_a)
    t = _thread(ca)
    done = _log(ca, t["id"], "A's history")["steps"][0]
    nxt = _next(ca, t["id"]).json()["steps"][-1]

    cb = auth.as_user(user_b)
    assert cb.get("/threads").json()["threads"] == []
    tid, did, nid = t["id"], done["id"], nxt["id"]
    attempts = [
        cb.patch(f"/threads/{tid}", json={"title": "pwned"}),
        cb.patch(f"/threads/{tid}", json={"archived": True}),
        cb.post(f"/threads/{tid}/steps", json={"label": "x"}),
        cb.post(f"/threads/{tid}/next", json={"label": "x", "list": "mine"}),
        cb.patch(f"/threads/{tid}/steps/{did}", json={"label": "x"}),
        cb.patch(f"/threads/{tid}/steps/{nid}", json={"note": "x"}),
        cb.post(f"/threads/{tid}/steps/{nid}/complete"),
        cb.delete(f"/threads/{tid}/steps/{did}"),
        cb.delete(f"/threads/{tid}/steps/{nid}"),
    ]
    assert [r.status_code for r in attempts] == [404] * len(attempts)

    # B's own thread can't reach A's step by id either.
    tb = _thread(cb, "B's")
    assert cb.delete(f"/threads/{tb['id']}/steps/{did}").status_code == 404

    ca = auth.as_user(user_a)
    mine = _one(ca, tid)
    assert mine["title"] == "Partner pilot" and mine["archived"] is False
    assert [s["label"] for s in mine["steps"]] == ["A's history", "Nudge them"]
    task = tasks.get(nxt["task_id"])
    assert task.notes in (None, "") and task.status == "needsAction"


def test_reconcile_and_next_are_user_scoped(auth, user_a, user_b, tasks):
    """B's next step goes into B's own pinned list, and B's reconcile never reads
    A's tasks: a B step pointing at A's task id is treated as deleted."""
    tasks.lists(user_b)
    ca = auth.as_user(user_a)
    ta = _thread(ca)
    a_next = _next(ca, ta["id"]).json()["steps"][-1]

    cb = auth.as_user(user_b)
    tb = _thread(cb, "B's")
    r = _next(cb, tb["id"], lst="mine")
    assert r.status_code == 201, r.text
    b_next = r.json()["steps"][-1]
    assert b_next["tasklist_id"] == f"{MINE}_{user_b.id}"
    assert tasks.tasks(user_a, MINE) == []

    # A B step forged to link A's task: reconcile scopes by user, so it's "gone".
    tasks.session.add(
        ThreadStep(
            thread_id=tb["id"],
            user_id=user_b.id,
            position=5000.0,
            kind="next",
            label="Forged",
            tasklist_id=FOLLOW,
            task_id=a_next["task_id"],
            via="follow",
        )
    )
    tasks.session.commit()
    steps = _one(cb, tb["id"])["steps"]
    assert [s["label"] for s in steps] == ["Nudge them"]
    assert steps[0]["task_id"] == b_next["task_id"]
    # A's own link is untouched.
    ca = auth.as_user(user_a)
    assert _one(ca, ta["id"])["steps"][-1]["task_id"] == a_next["task_id"]


# ── Write surface (writes.md) ─────────────────────────────────────────────────


def _attr_calls(mod, owner: str) -> set[str]:
    tree = ast.parse(inspect.getsource(mod))
    return {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == owner
    }


def _attr_refs(mod, owner: str) -> set[str]:
    """Every `owner.<attr>` reference (called or not)."""
    tree = ast.parse(inspect.getsource(mod))
    return {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == owner
    }


def _imported_modules(mod) -> set[str]:
    tree = ast.parse(inspect.getsource(mod))
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module)
            names.update(f"{n.module}.{a.name}" for a in n.names)
    return names


def test_threads_write_dependency_set_is_pinned():
    """Statically: threads calls exactly {create_task, update_content, reschedule,
    move} on the writes service — never `delete`/`delete_task`/`append_note`.
    (`writes_svc._UNSET`, the "keep the due" sentinel, is the only other ref.)"""
    assert _attr_calls(threads_svc, "writes_svc") == {
        "create_task",
        "update_content",
        "reschedule",
        "move",
    }
    assert _attr_refs(threads_svc, "writes_svc") - {"_UNSET"} == {
        "create_task",
        "update_content",
        "reschedule",
        "move",
    }


def test_threads_never_reaches_google_tasks_or_delete():
    """Goal 17: the threads package never imports the Google Tasks client (reads go
    to the local store), and `delete_task` isn't even referenced."""
    for mod in (threads_svc, threads_router):
        imported = _imported_modules(mod)
        assert not any(m.startswith("app.google.tasks") for m in imported), imported
        assert _attr_refs(mod, "tasks_client") == set()
        src = inspect.getsource(mod)
        assert "delete_task" not in src
        assert "writes_svc.delete(" not in src
