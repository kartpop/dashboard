"""Endpoint tests for the task write paths against the local task store (goal 17).

Tasks live in the dashboard's own DB since goal 17 — no Google Tasks call is
reachable from these endpoints. Each test seeds lists/tasks with the `store`
fixture, hits the endpoint via `client` (authenticated as user A), and asserts on
the response AND on the resulting DB rows.

Covered:
- reschedule: due/rank/group writes, same-bucket idempotency, clearing to NO_DATE,
  group-in-destination-bucket validation (422, no write), unknown task (404).
- move: in-place (task keeps its id), `due_date` omitted (preserve) vs explicit
  value vs explicit null (clear), destination-group validation, same list (400),
  unknown task / unknown or foreign target list (404).
- create / edit (title, notes) / complete / uncomplete (`completed` set + cleared) /
  bad status / delete, and list rename.
- `GET /tasks?show_completed=true` includes completed tasks; the default hides them.
- Two-user isolation: user B can neither read nor mutate user A's tasks or lists by
  id — every such attempt is a 404 and A's rows are untouched.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.overlay.models import TaskGroup
from tests.conftest import FOLLOW, MINE, OTHER

# The wire form of a stored due of 2026-06-15 (midnight UTC, as Google used).
DUE_0615 = "2026-06-15T00:00:00.000Z"


@pytest.fixture
def lists(store, user_a):
    """User A's three default lists (MINE / FOLLOW / OTHER)."""
    store.lists(user_a)


def _group(session, user, list_id, bucket_key, name="g", rank=1.0) -> TaskGroup:
    grp = TaskGroup(
        user_id=user.id,
        tasklist_id=list_id,
        bucket_key=bucket_key,
        name=name,
        rank=rank,
    )
    session.add(grp)
    session.commit()
    session.refresh(grp)
    return grp


def _all_task_ids(payload: dict) -> set[str]:
    """Every task id in a `GET /tasks?view=flat` payload."""
    return {t["id"] for tl in payload["task_lists"] for t in tl["tasks"]}


# ── reschedule ────────────────────────────────────────────────────────────────


def test_reschedule_to_different_bucket(client, store, user_a, lists):
    # Undated task (NO_DATE bucket) → 2026-06-15.
    store.add(user_a, MINE, "T1")
    resp = client.post(
        f"/tasks/{MINE}/T1/reschedule",
        json={"due_date": "2026-06-15", "rank": 100.0, "group_id": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "tasklist_id": MINE,
        "task_id": "T1",
        "due": DUE_0615,
        "rank": 100.0,
        "group_id": None,
    }
    row = store.get("T1")
    assert row.due == date(2026, 6, 15)
    assert row.rank == 100.0 and row.group_id is None


def test_reschedule_idempotent_same_bucket(client, store, user_a, lists):
    # Already due 2026-06-15; same target bucket → due unchanged, rank still written.
    store.add(user_a, MINE, "T1", due="2026-06-15", rank=1.0)
    resp = client.post(
        f"/tasks/{MINE}/T1/reschedule",
        json={"due_date": "2026-06-15", "rank": 50.0, "group_id": None},
    )
    assert resp.status_code == 200
    assert resp.json()["due"] == DUE_0615
    row = store.get("T1")
    assert row.due == date(2026, 6, 15) and row.rank == 50.0

    # Repeating the identical request is a no-op on the row's state.
    again = client.post(
        f"/tasks/{MINE}/T1/reschedule",
        json={"due_date": "2026-06-15", "rank": 50.0, "group_id": None},
    )
    assert again.status_code == 200 and again.json() == resp.json()


def test_reschedule_to_no_date_clears_due(client, store, user_a, lists):
    store.add(user_a, MINE, "T1", due="2026-06-15")
    resp = client.post(
        f"/tasks/{MINE}/T1/reschedule",
        json={"due_date": None, "rank": 10.0, "group_id": None},
    )
    assert resp.status_code == 200
    assert resp.json()["due"] is None
    assert store.get("T1").due is None


def test_reschedule_group_wrong_bucket_422(client, store, session, user_a, lists):
    store.add(user_a, MINE, "T1", rank=9.0)
    # Group lives in a DIFFERENT bucket than the destination.
    grp = _group(session, user_a, MINE, "2026-06-20")

    resp = client.post(
        f"/tasks/{MINE}/T1/reschedule",
        json={"due_date": "2026-06-15", "rank": 5.0, "group_id": grp.id},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "group_wrong_bucket"
    # Nothing written.
    row = store.get("T1")
    assert row.due is None and row.rank == 9.0 and row.group_id is None


def test_reschedule_group_correct_bucket_200(client, store, session, user_a, lists):
    store.add(user_a, MINE, "T1")
    grp = _group(session, user_a, MINE, "2026-06-15")

    resp = client.post(
        f"/tasks/{MINE}/T1/reschedule",
        json={"due_date": "2026-06-15", "rank": 5.0, "group_id": grp.id},
    )
    assert resp.status_code == 200
    assert resp.json()["group_id"] == grp.id
    assert store.get("T1").group_id == grp.id


def test_reschedule_group_in_other_list_422(client, store, session, user_a, lists):
    # Right bucket, wrong list → still not a destination group.
    store.add(user_a, MINE, "T1")
    grp = _group(session, user_a, FOLLOW, "2026-06-15")
    resp = client.post(
        f"/tasks/{MINE}/T1/reschedule",
        json={"due_date": "2026-06-15", "rank": 5.0, "group_id": grp.id},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "group_wrong_bucket"


def test_reschedule_task_not_found_404(client, lists):
    resp = client.post(
        f"/tasks/{MINE}/missing/reschedule",
        json={"due_date": "2026-06-15", "rank": 1.0, "group_id": None},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


def test_reschedule_wrong_list_in_path_404(client, store, user_a, lists):
    # The task exists, but not in the list named by the path.
    store.add(user_a, MINE, "T1")
    resp = client.post(
        f"/tasks/{FOLLOW}/T1/reschedule",
        json={"due_date": "2026-06-15", "rank": 1.0, "group_id": None},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"
    assert store.get("T1").due is None


# ── move ──────────────────────────────────────────────────────────────────────


def test_move_happy_path_keeps_task_id(client, store, user_a, lists):
    store.add(user_a, MINE, "T1", title="x", due="2026-06-15", notes="n", rank=9.0)

    resp = client.post(
        f"/tasks/{MINE}/T1/move", json={"target_list_id": FOLLOW, "rank": 7.0}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "target_list_id": FOLLOW,
        "new_task_id": "T1",
        "rank": 7.0,
        "group_id": None,
    }
    # Same row, new list; content carried, rank replaced, nothing duplicated.
    row = store.get("T1")
    assert row.tasklist_id == FOLLOW
    assert row.title == "x" and row.notes == "n"
    assert row.rank == 7.0 and row.group_id is None
    assert store.tasks(user_a, MINE) == []
    assert [t.id for t in store.tasks(user_a, FOLLOW)] == ["T1"]


def test_move_lands_on_top_of_target_list(client, store, user_a, lists):
    store.add(user_a, FOLLOW, "F1")
    store.add(user_a, FOLLOW, "F2")
    store.add(user_a, MINE, "T1")
    resp = client.post(f"/tasks/{MINE}/T1/move", json={"target_list_id": FOLLOW})
    assert resp.status_code == 200
    assert [t.id for t in store.tasks(user_a, FOLLOW)] == ["T1", "F1", "F2"]


def test_move_with_due_date_reschedules(client, store, user_a, lists):
    # Undated source dragged onto the 2026-06-15 bucket of the destination.
    store.add(user_a, MINE, "T1")
    resp = client.post(
        f"/tasks/{MINE}/T1/move",
        json={"target_list_id": FOLLOW, "rank": 7.0, "due_date": "2026-06-15"},
    )
    assert resp.status_code == 200
    row = store.get("T1")
    assert row.tasklist_id == FOLLOW and row.due == date(2026, 6, 15)


def test_move_with_due_date_null_clears_due(client, store, user_a, lists):
    # A dated source dragged onto the destination's NO_DATE bucket → due cleared.
    store.add(user_a, MINE, "T1", due="2026-06-15")
    resp = client.post(
        f"/tasks/{MINE}/T1/move",
        json={"target_list_id": FOLLOW, "rank": 1.0, "due_date": None},
    )
    assert resp.status_code == 200
    assert store.get("T1").due is None


def test_move_omitted_due_preserves_source_due(client, store, user_a, lists):
    # No due_date key → the source due is carried over (menu-move parity).
    store.add(user_a, MINE, "T1", due="2026-06-15")
    resp = client.post(
        f"/tasks/{MINE}/T1/move", json={"target_list_id": FOLLOW, "rank": 1.0}
    )
    assert resp.status_code == 200
    assert store.get("T1").due == date(2026, 6, 15)


def test_move_into_group_in_dest_bucket_200(client, store, session, user_a, lists):
    store.add(user_a, MINE, "T1")
    # A group in the DESTINATION list + destination bucket.
    grp = _group(session, user_a, FOLLOW, "2026-06-15")

    resp = client.post(
        f"/tasks/{MINE}/T1/move",
        json={
            "target_list_id": FOLLOW,
            "rank": 3.0,
            "due_date": "2026-06-15",
            "group_id": grp.id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["group_id"] == grp.id
    row = store.get("T1")
    assert row.tasklist_id == FOLLOW and row.group_id == grp.id


def test_move_preserved_due_validates_group_against_it(
    client, store, session, user_a, lists
):
    # due_date omitted → the group must match the task's EXISTING due's bucket.
    store.add(user_a, MINE, "T1", due="2026-06-15")
    grp = _group(session, user_a, FOLLOW, "2026-06-15")
    resp = client.post(
        f"/tasks/{MINE}/T1/move",
        json={"target_list_id": FOLLOW, "rank": 3.0, "group_id": grp.id},
    )
    assert resp.status_code == 200
    assert store.get("T1").group_id == grp.id


def test_move_group_wrong_bucket_422_no_writes(client, store, session, user_a, lists):
    store.add(user_a, MINE, "T1", rank=9.0)
    # Group lives in a different bucket than the drop target → reject before writing.
    grp = _group(session, user_a, FOLLOW, "2026-06-20")

    resp = client.post(
        f"/tasks/{MINE}/T1/move",
        json={
            "target_list_id": FOLLOW,
            "rank": 3.0,
            "due_date": "2026-06-15",
            "group_id": grp.id,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "group_wrong_bucket"
    row = store.get("T1")
    assert row.tasklist_id == MINE and row.due is None and row.rank == 9.0


def test_move_drops_source_group(client, store, session, user_a, lists):
    # A group belongs to its list; moving without a group_id leaves the task ungrouped.
    src = _group(session, user_a, MINE, "NO_DATE")
    store.add(user_a, MINE, "T1", group_id=src.id)
    resp = client.post(f"/tasks/{MINE}/T1/move", json={"target_list_id": FOLLOW})
    assert resp.status_code == 200
    assert store.get("T1").group_id is None


def test_move_same_list_400(client, store, user_a, lists):
    store.add(user_a, MINE, "T1")
    resp = client.post(f"/tasks/{MINE}/T1/move", json={"target_list_id": MINE})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "same_list"
    assert store.get("T1").tasklist_id == MINE


def test_move_task_not_found_404(client, lists):
    resp = client.post(f"/tasks/{MINE}/missing/move", json={"target_list_id": FOLLOW})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


def test_move_unknown_target_list_404(client, store, user_a, lists):
    store.add(user_a, MINE, "T1")
    resp = client.post(f"/tasks/{MINE}/T1/move", json={"target_list_id": "L_NOPE"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "list_not_found"
    assert store.get("T1").tasklist_id == MINE


def test_move_to_other_users_list_404(client, store, user_a, user_b, lists):
    store.lists(user_b, {"L_B": "B's list"})
    store.add(user_a, MINE, "T1")
    resp = client.post(f"/tasks/{MINE}/T1/move", json={"target_list_id": "L_B"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "list_not_found"
    row = store.get("T1")
    assert row.tasklist_id == MINE and row.user_id == user_a.id


# ── create ─────────────────────────────────────────────────────────────────────


def test_create_task(client, store, user_a, lists):
    resp = client.post(f"/tasks/{MINE}", json={"title": "new task", "rank": 500.0})
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"]
    assert data["type"] == "task"
    assert data["title"] == "new task"
    assert data["status"] == "needsAction"
    assert data["due"] is None and data["completed"] is None
    assert data["rank"] == 500.0 and data["group_id"] is None
    row = store.get(data["id"])
    assert row is not None
    assert row.user_id == user_a.id and row.tasklist_id == MINE
    assert row.title == "new task" and row.due is None and row.rank == 500.0


def test_create_task_with_notes_and_due(client, store, lists):
    resp = client.post(
        f"/tasks/{MINE}",
        json={"title": "t", "notes": "body", "due_date": "2026-06-15"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["due"] == DUE_0615 and data["notes"] == "body"
    row = store.get(data["id"])
    assert row.due == date(2026, 6, 15) and row.notes == "body"


def test_create_task_lands_on_top(client, store, user_a, lists):
    store.add(user_a, MINE, "OLD")
    resp = client.post(f"/tasks/{MINE}", json={"title": "fresh"})
    assert resp.status_code == 201
    assert [t.id for t in store.tasks(user_a, MINE)] == [resp.json()["id"], "OLD"]


def test_create_task_empty_title_400(client, store, user_a, lists):
    resp = client.post(f"/tasks/{MINE}", json={"title": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_title"
    assert store.tasks(user_a) == []


def test_create_task_unknown_list_404(client, store, user_a, lists):
    resp = client.post("/tasks/L_NOPE", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "list_not_found"
    assert store.tasks(user_a) == []


# ── content edit (title / notes) ────────────────────────────────────────────────


def test_edit_title_only(client, store, user_a, lists):
    store.add(user_a, MINE, "T1", title="old", notes="keep", due="2026-06-15")
    resp = client.patch(f"/tasks/{MINE}/T1", json={"title": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "renamed"
    # Omitted fields are left alone, not nulled.
    row = store.get("T1")
    assert row.title == "renamed" and row.notes == "keep"
    assert row.status == "needsAction" and row.due == date(2026, 6, 15)


def test_edit_notes_only(client, store, user_a, lists):
    store.add(user_a, MINE, "T1", title="keep")
    resp = client.patch(f"/tasks/{MINE}/T1", json={"notes": "some notes"})
    assert resp.status_code == 200
    assert resp.json()["notes"] == "some notes"
    row = store.get("T1")
    assert row.notes == "some notes" and row.title == "keep"


def test_edit_notes_null_clears(client, store, user_a, lists):
    store.add(user_a, MINE, "T1", notes="old")
    resp = client.patch(f"/tasks/{MINE}/T1", json={"notes": None})
    assert resp.status_code == 200
    assert store.get("T1").notes is None


def test_edit_empty_title_400(client, store, user_a, lists):
    store.add(user_a, MINE, "T1", title="keep")
    resp = client.patch(f"/tasks/{MINE}/T1", json={"title": ""})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_title"
    assert store.get("T1").title == "keep"


def test_edit_no_fields_400(client, store, user_a, lists):
    store.add(user_a, MINE, "T1")
    resp = client.patch(f"/tasks/{MINE}/T1", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_fields"


def test_edit_missing_task_404(client, lists):
    resp = client.patch(f"/tasks/{MINE}/missing", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


# ── complete / uncomplete (status rides the content patch) ──────────────────────


def test_complete_task_sets_completed(client, store, user_a, lists):
    store.add(user_a, MINE, "T1")
    resp = client.patch(f"/tasks/{MINE}/T1", json={"status": "completed"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["completed"] is not None and data["completed"].endswith("Z")
    row = store.get("T1")
    assert row.status == "completed" and row.completed_at is not None


def test_uncomplete_task_clears_completed(client, store, user_a, lists):
    store.add(user_a, MINE, "T1", status="completed")
    assert store.get("T1").completed_at is not None
    resp = client.patch(f"/tasks/{MINE}/T1", json={"status": "needsAction"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needsAction" and data["completed"] is None
    row = store.get("T1")
    assert row.status == "needsAction" and row.completed_at is None


def test_bad_status_400(client, store, user_a, lists):
    store.add(user_a, MINE, "T1")
    resp = client.patch(f"/tasks/{MINE}/T1", json={"status": "done"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_status"
    assert store.get("T1").status == "needsAction"


# ── delete (user path: immediate, row gone) ────────────────────────────────────


def test_delete_task_removes_row(client, store, user_a, lists):
    store.add(user_a, MINE, "T1", rank=3.0)
    store.add(user_a, MINE, "T2")
    resp = client.delete(f"/tasks/{MINE}/T1")
    assert resp.status_code == 200
    assert resp.json() == {"tasklist_id": MINE, "task_id": "T1", "deleted": True}
    assert store.get("T1") is None
    assert store.get("T2") is not None


def test_delete_missing_task_404(client, lists):
    resp = client.delete(f"/tasks/{MINE}/missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


# ── list rename ─────────────────────────────────────────────────────────────────


def test_rename_list(client, store, user_a, lists):
    resp = client.patch(f"/lists/{OTHER}", json={"title": "Renamed List"})
    assert resp.status_code == 200
    assert resp.json() == {"id": OTHER, "title": "Renamed List"}
    titles = {tl["id"]: tl["title"] for tl in client.get("/tasks").json()["task_lists"]}
    assert titles[OTHER] == "Renamed List"


def test_rename_list_empty_400(client, lists):
    resp = client.patch(f"/lists/{OTHER}", json={"title": "  "})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_title"


def test_rename_unknown_list_404(client, lists):
    resp = client.patch("/lists/L_NOPE", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "list_not_found"


# ── GET /tasks reads the store ──────────────────────────────────────────────────


def test_get_tasks_hides_completed_by_default(client, store, user_a, lists):
    store.add(user_a, MINE, "OPEN")
    store.add(user_a, MINE, "DONE", status="completed")
    resp = client.get("/tasks", params={"view": "flat"})
    assert resp.status_code == 200
    assert _all_task_ids(resp.json()) == {"OPEN"}


def test_get_tasks_show_completed_includes_completed(client, store, user_a, lists):
    store.add(user_a, MINE, "OPEN")
    store.add(user_a, MINE, "DONE", status="completed")
    resp = client.get("/tasks", params={"view": "flat", "show_completed": "true"})
    assert resp.status_code == 200
    payload = resp.json()
    assert _all_task_ids(payload) == {"OPEN", "DONE"}
    done = next(
        t for tl in payload["task_lists"] for t in tl["tasks"] if t["id"] == "DONE"
    )
    assert done["status"] == "completed" and done["completed"] is not None


# ── two-user isolation ──────────────────────────────────────────────────────────


@pytest.fixture
def a_task(store, user_a, user_b, lists):
    """User A's task T1 (due 2026-06-15, rank 1.0) in MINE; B has own lists."""
    store.lists(user_b)
    return store.add(user_a, MINE, "T1", title="a's", due="2026-06-15", rank=1.0)


def _assert_a_task_untouched(store, user_a):
    row = store.get("T1")
    assert row is not None
    assert row.user_id == user_a.id and row.tasklist_id == MINE
    assert row.title == "a's" and row.status == "needsAction"
    assert row.due == date(2026, 6, 15) and row.rank == 1.0


def test_user_b_cannot_read_a_tasks(auth, store, user_a, user_b, a_task):
    store.add(user_b, f"{MINE}_{user_b.id}", "B1")
    resp = auth.as_user(user_b).get(
        "/tasks", params={"view": "flat", "show_completed": "true"}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert _all_task_ids(payload) == {"B1"}
    assert MINE not in {tl["id"] for tl in payload["task_lists"]}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", f"/tasks/{MINE}/T1/reschedule", {"due_date": None, "rank": 9.0}),
        ("post", f"/tasks/{MINE}/T1/move", {"target_list_id": FOLLOW}),
        ("patch", f"/tasks/{MINE}/T1", {"title": "hijacked"}),
        ("patch", f"/tasks/{MINE}/T1", {"status": "completed"}),
        ("delete", f"/tasks/{MINE}/T1", None),
    ],
)
def test_user_b_cannot_mutate_a_task(
    auth, store, user_a, user_b, a_task, method, path, body
):
    c = auth.as_user(user_b)
    kwargs = {} if body is None else {"json": body}
    resp = getattr(c, method)(path, **kwargs)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"
    _assert_a_task_untouched(store, user_a)


def test_user_b_cannot_move_own_task_into_a_list(auth, store, user_a, user_b, a_task):
    b_list = f"{MINE}_{user_b.id}"
    store.add(user_b, b_list, "B1")
    resp = auth.as_user(user_b).post(
        f"/tasks/{b_list}/B1/move", json={"target_list_id": MINE}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "list_not_found"
    assert store.get("B1").tasklist_id == b_list
    assert [t.id for t in store.tasks(user_a, MINE)] == ["T1"]


def test_user_b_cannot_create_in_a_list(auth, store, user_a, user_b, a_task):
    resp = auth.as_user(user_b).post(f"/tasks/{MINE}", json={"title": "sneaky"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "list_not_found"
    assert store.tasks(user_b) == []
    assert [t.id for t in store.tasks(user_a, MINE)] == ["T1"]


def test_user_b_cannot_rename_a_list(auth, client, user_a, user_b, a_task):
    resp = auth.as_user(user_b).patch(f"/lists/{MINE}", json={"title": "hijacked"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "list_not_found"
    titles = {
        tl["id"]: tl["title"]
        for tl in auth.as_user(user_a).get("/tasks").json()["task_lists"]
    }
    assert titles[MINE] == "My Tasks"
