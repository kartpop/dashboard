"""Goal 14 — Threads: storage, reconcile, link writes, isolation, write surface.

Google is a small in-memory fake (two pinned lists + one other) patched over the
`app.google.tasks` wrappers, so the writes service runs for real on top of it and
every Google call is recorded.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date

import pytest
from sqlmodel import select

from app.google.tasks import _UNSET, _reshape_task
from app.threads import router as threads_router
from app.threads import service as threads_svc
from app.threads.models import ThreadStep

MINE, FOLLOW, OTHER = "L_MINE", "L_FOLLOW", "L_OTHER"


class FakeGoogle:
    def __init__(self):
        self.lists: dict[str, dict] = {
            MINE: {"title": "My Tasks", "tasks": {}},
            FOLLOW: {"title": "Follow-ups", "tasks": {}},
            OTHER: {"title": "Groceries", "tasks": {}},
        }
        self.calls: list[tuple] = []
        self.insert_error: Exception | None = None
        self._n = 0

    # reads
    async def get_task_lists(self, creds):
        self.calls.append(("get_task_lists",))
        return [
            {
                "id": lid,
                "title": tl["title"],
                "tasks": [_reshape_task(t) for t in tl["tasks"].values()],
            }
            for lid, tl in self.lists.items()
        ]

    async def get_tasklist_refs(self, creds):
        self.calls.append(("get_tasklist_refs",))
        return [{"id": lid, "title": tl["title"]} for lid, tl in self.lists.items()]

    async def get_task(self, creds, tasklist_id, task_id):
        self.calls.append(("get_task", tasklist_id, task_id))
        task = self.lists.get(tasklist_id, {}).get("tasks", {}).get(task_id)
        return _reshape_task(task) if task else None

    # writes
    async def insert_task(self, creds, tasklist_id, body):
        self.calls.append(("insert_task", tasklist_id, body))
        if self.insert_error:
            raise self.insert_error
        self._n += 1
        task = {"id": f"T{self._n}", "status": "needsAction", **body}
        self.lists[tasklist_id]["tasks"][task["id"]] = task
        return _reshape_task(task)

    async def delete_task(self, creds, tasklist_id, task_id):
        self.calls.append(("delete_task", tasklist_id, task_id))
        self.lists[tasklist_id]["tasks"].pop(task_id, None)

    async def update_task_content(
        self, creds, tasklist_id, task_id, title=_UNSET, notes=_UNSET, status=_UNSET
    ):
        body = {}
        if title is not _UNSET:
            body["title"] = title
        if notes is not _UNSET:
            body["notes"] = notes
        if status is not _UNSET:
            body["status"] = status
        self.calls.append(("update_task_content", tasklist_id, task_id, body))
        task = self.lists[tasklist_id]["tasks"][task_id]
        task.update(body)
        return _reshape_task(task)

    async def update_due_date(self, creds, tasklist_id, task_id, due):
        self.calls.append(("update_due_date", tasklist_id, task_id, due))
        task = self.lists[tasklist_id]["tasks"][task_id]
        if due is None:
            task.pop("due", None)
        else:
            task["due"] = due

    # helpers
    def task(self, list_id, task_id) -> dict | None:
        return self.lists[list_id]["tasks"].get(task_id)

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    for name in (
        "get_task_lists",
        "get_tasklist_refs",
        "get_task",
        "insert_task",
        "delete_task",
        "update_task_content",
        "update_due_date",
    ):
        monkeypatch.setattr(f"app.google.tasks.{name}", getattr(fake, name))
    return fake


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


def test_create_log_and_set_follow_up_next(client, google):
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
    assert nxt["due"] == "2026-10-01"
    # The Google task exists in Follow-ups with the note as its description.
    task = google.task(FOLLOW, nxt["task_id"])
    assert task["title"] == "Nudge them"
    assert task["notes"] == "ask who owns it"
    assert task["due"] == "2026-10-01T00:00:00.000Z"
    assert body["steps"][1]["note"] == "asked for feedback"
    assert body["last_moved_on"] >= "2026-09-01"


def test_next_409_when_next_exists(client, google):
    t = _thread(client)
    assert _next(client, t["id"]).status_code == 201
    inserts = google.names().count("insert_task")
    r = _next(client, t["id"], label="Another")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "next_exists"
    assert google.names().count("insert_task") == inserts  # no Google write


def test_log_step_inserts_before_next(client, google):
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


def test_log_defaults_to_today_ist(client, google, monkeypatch):
    monkeypatch.setattr(threads_svc, "today_ist", lambda: date(2026, 9, 29))
    t = _thread(client)
    body = _log(client, t["id"], "Did a thing")
    assert body["steps"][0]["occurred_on"] == "2026-09-29"
    assert body["last_moved_on"] == "2026-09-29"


def test_delete_next_unlinks_and_leaves_google_task(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.delete(f"/threads/{t['id']}/steps/{nxt['id']}")
    assert r.status_code == 200
    assert r.json()["unlinked"] is True
    assert r.json()["steps"] == []
    assert "delete_task" not in google.names()
    assert google.task(FOLLOW, nxt["task_id"]) is not None


def test_delete_done_step(client, google):
    t = _thread(client)
    body = _log(client, t["id"], "Oops")
    r = client.delete(f"/threads/{t['id']}/steps/{body['steps'][0]['id']}")
    assert r.status_code == 200 and r.json()["unlinked"] is False
    assert r.json()["steps"] == []


def test_archive_and_restore_leave_google_alone(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    before = list(google.calls)
    r = client.patch(f"/threads/{t['id']}", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True
    assert google.calls == before  # archive never touches Google
    assert google.task(FOLLOW, nxt["task_id"]) is not None
    r = client.patch(f"/threads/{t['id']}", json={"archived": False, "title": "New"})
    assert r.json()["archived"] is False and r.json()["title"] == "New"


def test_empty_title_and_label_rejected(client, google):
    assert client.post("/threads", json={"title": "  "}).status_code == 400
    t = _thread(client)
    assert (
        client.post(f"/threads/{t['id']}/steps", json={"label": ""}).status_code == 400
    )


def test_create_task_failure_writes_no_step(client, google, session):
    t = _thread(client)
    google.insert_error = RuntimeError("boom")
    r = _next(client, t["id"])
    assert r.status_code == 502
    assert session.exec(select(ThreadStep)).all() == []


def test_missing_pinned_list_422(client, google):
    del google.lists[FOLLOW]
    t = _thread(client)
    r = _next(client, t["id"])
    assert r.status_code == 422
    assert "insert_task" not in google.names()


# ── Next-step edits (writes) ──────────────────────────────────────────────────


def test_patch_next_label_note_go_to_google(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.patch(
        f"/threads/{t['id']}/steps/{nxt['id']}",
        json={"label": "Nudge #2", "note": "try their lead"},
    )
    assert r.status_code == 200, r.text
    step = r.json()["steps"][-1]
    assert step["label"] == "Nudge #2" and step["note"] == "try their lead"
    task = google.task(FOLLOW, nxt["task_id"])
    assert task["title"] == "Nudge #2" and task["notes"] == "try their lead"


def test_patch_next_unchanged_fields_skip_google(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    before = len(google.calls)
    r = client.patch(
        f"/threads/{t['id']}/steps/{nxt['id']}",
        json={"label": nxt["label"], "note": nxt["note"], "due": nxt["due"]},
    )
    assert r.status_code == 200
    assert len(google.calls) == before


def test_patch_next_due_reschedules(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.patch(
        f"/threads/{t['id']}/steps/{nxt['id']}", json={"due": "2026-10-09"}
    )
    assert r.json()["steps"][-1]["due"] == "2026-10-09"
    assert google.task(FOLLOW, nxt["task_id"])["due"] == "2026-10-09T00:00:00.000Z"


def test_patch_next_list_moves_and_stays_linked(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"], note="n").json()["steps"][-1]
    r = client.patch(
        f"/threads/{t['id']}/steps/{nxt['id']}",
        json={"list": "mine", "due": "2026-10-05"},
    )
    assert r.status_code == 200, r.text
    step = r.json()["steps"][-1]
    assert step["kind"] == "next" and step["list"] == "mine"
    assert step["tasklist_id"] == MINE and step["task_id"] != nxt["task_id"]
    assert google.task(FOLLOW, nxt["task_id"]) is None
    moved = google.task(MINE, step["task_id"])
    assert moved["notes"] == "n" and moved["due"] == "2026-10-05T00:00:00.000Z"
    # And the next reconcile still sees it as the live next step.
    after = _one(client, t["id"])["steps"][-1]
    assert after["kind"] == "next" and after["list"] == "mine"


def test_patch_done_step_is_local(client, google):
    t = _thread(client)
    step = _log(client, t["id"], "Call")["steps"][0]
    before = len(google.calls)
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
    assert len(google.calls) == before


def test_complete_endpoint_flips_and_completes_task(client, google, monkeypatch):
    monkeypatch.setattr(threads_svc, "today_ist", lambda: date(2026, 9, 29))
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.post(f"/threads/{t['id']}/steps/{nxt['id']}/complete")
    assert r.status_code == 200, r.text
    step = r.json()["steps"][-1]
    assert step["kind"] == "done" and step["via"] == "follow"
    assert step["occurred_on"] == "2026-09-29" and step["list"] is None
    assert google.task(FOLLOW, nxt["task_id"])["status"] == "completed"
    # Completing a done step is refused.
    again = client.post(f"/threads/{t['id']}/steps/{nxt['id']}/complete")
    assert again.status_code == 400


# ── Reconcile ─────────────────────────────────────────────────────────────────


def test_reconcile_completed_becomes_done(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    task = google.task(FOLLOW, nxt["task_id"])
    task.update(
        status="completed",
        title="Sent reminder #2",
        notes="no reply yet",
        # 20:00 UTC on the 30th is the 1st in IST.
        completed="2026-09-30T20:00:00.000Z",
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


def test_reconcile_completed_without_timestamp_falls_back_to_today(
    client, google, monkeypatch
):
    monkeypatch.setattr(threads_svc, "today_ist", lambda: date(2026, 9, 29))
    t = _thread(client)
    nxt = _next(client, t["id"], lst="mine").json()["steps"][-1]
    google.task(MINE, nxt["task_id"])["status"] = "completed"
    step = _one(client, t["id"])["steps"][-1]
    assert step["occurred_on"] == "2026-09-29" and step["via"] == "mine"


def test_reconcile_uncompleted_last_step_is_next_again(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    task = google.task(FOLLOW, nxt["task_id"])
    task.update(status="completed", completed="2026-09-29T05:00:00.000Z")
    assert _one(client, t["id"])["steps"][-1]["kind"] == "done"
    task.update(status="needsAction", completed=None)
    step = _one(client, t["id"])["steps"][-1]
    assert step["kind"] == "next" and step["occurred_on"] is None
    assert step["list"] == "follow" and step["due"] == "2026-10-01"


def test_reconcile_uncompleted_not_last_stays_done(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    task = google.task(FOLLOW, nxt["task_id"])
    task.update(status="completed")
    _get(client)
    _log(client, t["id"], "Later update")
    task.update(status="needsAction")
    steps = _one(client, t["id"])["steps"]
    assert [s["kind"] for s in steps] == ["done", "done"]


def test_reconcile_uncompleted_with_other_next_stays_done(client, google):
    t = _thread(client)
    first = _next(client, t["id"]).json()["steps"][-1]
    google.task(FOLLOW, first["task_id"]).update(status="completed")
    _get(client)
    _next(client, t["id"], label="New next", lst="mine")
    google.task(FOLLOW, first["task_id"]).update(status="needsAction")
    steps = _one(client, t["id"])["steps"]
    assert [(s["kind"], s["label"]) for s in steps] == [
        ("done", "Nudge them"),
        ("next", "New next"),
    ]


def test_reconcile_deleted_removes_next(client, google):
    t = _thread(client)
    _log(client, t["id"], "History")
    nxt = _next(client, t["id"]).json()["steps"][-1]
    del google.lists[FOLLOW]["tasks"][nxt["task_id"]]
    steps = _one(client, t["id"])["steps"]
    assert [s["kind"] for s in steps] == ["done"]  # dangling now


def test_reconcile_open_refreshes_cache(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    google.task(FOLLOW, nxt["task_id"]).update(
        title="Edited on phone", notes="phone note", due="2026-10-12T00:00:00.000Z"
    )
    step = _one(client, t["id"])["steps"][-1]
    assert (step["kind"], step["label"], step["note"], step["due"]) == (
        "next",
        "Edited on phone",
        "phone note",
        "2026-10-12",
    )


def test_dashboard_move_repoints_link(client, google):
    t = _thread(client)
    nxt = _next(client, t["id"]).json()["steps"][-1]
    r = client.post(
        f"/tasks/{FOLLOW}/{nxt['task_id']}/move", json={"target_list_id": MINE}
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["new_task_id"]
    step = _one(client, t["id"])["steps"][-1]
    assert step["kind"] == "next"
    assert (step["tasklist_id"], step["task_id"], step["list"]) == (
        MINE,
        new_id,
        "mine",
    )


def test_reconcile_skips_google_when_nothing_linked(client, google):
    t = _thread(client)
    _log(client, t["id"], "Local only")
    google.calls.clear()
    _get(client)
    assert "get_task_lists" not in google.names()


def test_reconcile_google_failure_serves_cache(client, google, monkeypatch):
    t = _thread(client)
    _next(client, t["id"])

    async def boom(creds):
        raise RuntimeError("down")

    monkeypatch.setattr("app.google.tasks.get_task_lists", boom)
    steps = _one(client, t["id"])["steps"]
    assert [s["kind"] for s in steps] == ["next"]


def test_list_orders_steps_and_includes_archived(client, google):
    a = _thread(client, "A")
    b = _thread(client, "B")
    client.patch(f"/threads/{b['id']}", json={"archived": True})
    threads = _get(client)
    assert {t["title"]: t["archived"] for t in threads} == {"A": False, "B": True}
    assert a["created_at"]


# ── Isolation ─────────────────────────────────────────────────────────────────


def test_two_user_isolation_on_every_endpoint(auth, user_a, user_b, google):
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
    assert google.task(FOLLOW, nxt["task_id"]).get("notes") in (None, "")


def test_repoint_link_is_user_scoped(session, user_a, user_b):
    from app.threads.models import Thread

    ta = Thread(user_id=user_a.id, title="a")
    session.add(ta)
    session.commit()
    session.add(
        ThreadStep(
            thread_id=ta.id,
            user_id=user_a.id,
            position=1000,
            kind="next",
            label="x",
            tasklist_id=FOLLOW,
            task_id="T1",
        )
    )
    session.commit()
    assert threads_svc.repoint_link(session, user_b.id, FOLLOW, "T1", MINE, "T9") == 0
    assert threads_svc.repoint_link(session, user_a.id, FOLLOW, "T1", MINE, "T9") == 1


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


def test_threads_write_dependency_set_is_pinned():
    """Statically: threads calls exactly {create_task, update_content, reschedule,
    move} on the writes service — never `delete`/`delete_task`/`append_note`."""
    assert _attr_calls(threads_svc, "writes_svc") == {
        "create_task",
        "update_content",
        "reschedule",
        "move",
    }


def test_threads_never_reaches_delete_task():
    """Threads reads Google directly only (list fetches); no thin write wrapper is
    called from the threads package, and `delete_task` isn't even referenced."""
    assert _attr_calls(threads_svc, "tasks_client") == {
        "get_task_lists",
        "get_tasklist_refs",
    }
    for mod in (threads_svc, threads_router):
        src = inspect.getsource(mod)
        assert "delete_task" not in src.replace("never calls `delete_task`", "")
        assert "writes_svc.delete(" not in src
