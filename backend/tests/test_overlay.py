"""Tests for the overlay layer: merge/bucket logic, rank/group writes, group CRUD.

Since goal 17 `rank` / `group_id` are columns on the local `Task` row (no separate
TaskOverlay join): `get_merged_task_lists` reads them off each raw task dict, and
`upsert_overlay` updates the Task row (None → 404 when the user has no such task).

Covers:
- the goal-4a Overdue rollup: past-due tasks collapse into a single synthetic
  "Overdue" bucket at the top instead of scattering across past dates;
- merge ordering by rank and group placement;
- `PATCH /tasks/{list}/{task}/overlay` (rank, group, explicit ungroup, no fields,
  unknown task → 404);
- group CRUD (create / duplicate 409 / update / delete nulls members' group_id);
- per-user isolation (goal 8): a user never reads or writes another user's
  task rank/group or groups.
"""

from __future__ import annotations

from app.overlay import service as overlay_svc
from app.overlay.models import TaskGroup
from app.tasks_store import service as store_svc
from tests.conftest import FOLLOW, MINE

# A safely-past date and a safely-future date relative to any real run date.
PAST_1 = "2020-01-01T00:00:00.000Z"
PAST_2 = "2020-02-01T00:00:00.000Z"
FUTURE = "2099-12-31T00:00:00.000Z"


def _task(task_id, due, rank=None, group_id=None):
    return {
        "id": task_id,
        "title": task_id,
        "status": "needsAction",
        "due": due,
        "notes": None,
        "rank": rank,
        "group_id": group_id,
    }


# ── Merge / buckets ─────────────────────────────────────────────────────────────


def test_overdue_rollup_at_top(session, user_a):
    raw = [
        {
            "id": "L1",
            "title": "My Tasks",
            "tasks": [
                _task("past-a", PAST_1),
                _task("past-b", PAST_2),
                _task("future", FUTURE),
                _task("nodate", None),
            ],
        }
    ]
    merged = overlay_svc.get_merged_task_lists(session, user_a.id, raw, view="grouped")
    buckets = merged[0]["buckets"]

    # First bucket is the rollup, holding both past tasks (oldest first).
    assert buckets[0]["key"] == "OVERDUE"
    assert buckets[0]["label"] == "Overdue"
    overdue_ids = [it["id"] for it in buckets[0]["items"]]
    assert overdue_ids == ["past-a", "past-b"]

    # No separate past-date buckets remain; future + NO_DATE still present.
    keys = [b["key"] for b in buckets]
    assert "2020-01-01" not in keys and "2020-02-01" not in keys
    assert "NO_DATE" in keys


def test_no_overdue_bucket_when_nothing_past(session, user_a):
    raw = [
        {
            "id": "L1",
            "title": "My Tasks",
            "tasks": [_task("future", FUTURE), _task("nodate", None)],
        }
    ]
    merged = overlay_svc.get_merged_task_lists(session, user_a.id, raw, view="grouped")
    keys = [b["key"] for b in merged[0]["buckets"]]
    assert "OVERDUE" not in keys


def test_merge_reads_rank_from_task_dict(session, user_a):
    raw = [
        {
            "id": "L1",
            "title": "My Tasks",
            "tasks": [
                _task("unranked", None),
                _task("second", None, rank=2.0),
                _task("first", None, rank=1.0),
            ],
        }
    ]
    merged = overlay_svc.get_merged_task_lists(session, user_a.id, raw, view="flat")
    assert [t["id"] for t in merged[0]["tasks"]] == ["first", "second", "unranked"]
    assert merged[0]["tasks"][0]["rank"] == 1.0


def test_merge_places_grouped_task_under_its_group(session, user_a, store):
    store.lists(user_a)
    grp = overlay_svc.create_group(
        session, user_a.id, MINE, bucket_key="NO_DATE", name="g", rank=1.0
    )
    store.add(user_a, MINE, "IN", group_id=grp.id, rank=1.0)
    store.add(user_a, MINE, "OUT", rank=2.0)

    raw = store_svc.get_task_lists(session, user_a.id)
    merged = overlay_svc.get_merged_task_lists(session, user_a.id, raw, view="grouped")
    mine = next(tl for tl in merged if tl["id"] == MINE)
    (bucket,) = [b for b in mine["buckets"] if b["key"] == "NO_DATE"]
    group_items = [it for it in bucket["items"] if it.get("type") == "group"]
    assert len(group_items) == 1
    assert [t["id"] for t in group_items[0]["items"]] == ["IN"]
    standalone = [it["id"] for it in bucket["items"] if it.get("type") == "task"]
    assert standalone == ["OUT"]


# ── PATCH overlay (rank / group on the Task row) ───────────────────────────────


def test_patch_overlay_rank(client, store, user_a):
    store.lists(user_a)
    store.add(user_a, MINE, "T1", rank=1.0)
    resp = client.patch(f"/tasks/{MINE}/T1/overlay", json={"rank": 42.0})
    assert resp.status_code == 200
    assert resp.json() == {
        "tasklist_id": MINE,
        "task_id": "T1",
        "rank": 42.0,
        "group_id": None,
    }
    assert store.get("T1").rank == 42.0


def test_patch_overlay_group_then_ungroup(client, store, session, user_a):
    store.lists(user_a)
    grp = overlay_svc.create_group(
        session, user_a.id, MINE, bucket_key="NO_DATE", name="g", rank=1.0
    )
    store.add(user_a, MINE, "T1", rank=5.0)

    resp = client.patch(f"/tasks/{MINE}/T1/overlay", json={"group_id": grp.id})
    assert resp.status_code == 200
    assert resp.json()["group_id"] == grp.id
    row = store.get("T1")
    assert row.group_id == grp.id and row.rank == 5.0  # rank untouched

    # Explicit null ungroups; rank-only patch would leave group alone.
    resp = client.patch(f"/tasks/{MINE}/T1/overlay", json={"rank": 6.0})
    assert resp.status_code == 200
    assert store.get("T1").group_id == grp.id
    resp = client.patch(f"/tasks/{MINE}/T1/overlay", json={"group_id": None})
    assert resp.status_code == 200
    assert store.get("T1").group_id is None


def test_patch_overlay_no_fields_400(client, store, user_a):
    store.lists(user_a)
    store.add(user_a, MINE, "T1")
    resp = client.patch(f"/tasks/{MINE}/T1/overlay", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_fields"


def test_patch_overlay_unknown_task_404(client, store, user_a):
    store.lists(user_a)
    resp = client.patch(f"/tasks/{MINE}/missing/overlay", json={"rank": 1.0})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


def test_patch_overlay_wrong_list_404(client, store, user_a):
    store.lists(user_a)
    store.add(user_a, MINE, "T1", rank=1.0)
    resp = client.patch(f"/tasks/{FOLLOW}/T1/overlay", json={"rank": 9.0})
    assert resp.status_code == 404
    assert store.get("T1").rank == 1.0


# ── Group CRUD ──────────────────────────────────────────────────────────────────


def test_group_create_update_delete(client, store, session, user_a):
    store.lists(user_a)
    resp = client.post(
        f"/tasks/{MINE}/groups",
        json={"name": "Errands", "bucket_key": "2026-06-15", "rank": 3.0},
    )
    assert resp.status_code == 201
    grp = resp.json()
    assert grp["tasklist_id"] == MINE and grp["bucket_key"] == "2026-06-15"
    assert grp["name"] == "Errands" and grp["rank"] == 3.0
    gid = grp["id"]

    resp = client.patch(f"/tasks/{MINE}/groups/{gid}", json={"name": "Chores"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Chores" and resp.json()["rank"] == 3.0

    resp = client.patch(f"/tasks/{MINE}/groups/{gid}", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_fields"

    # Deleting the group ungroups its members (tasks survive).
    store.add(user_a, MINE, "M1", due="2026-06-15", group_id=gid)
    store.add(user_a, MINE, "M2", due="2026-06-15", group_id=gid)
    resp = client.delete(f"/tasks/{MINE}/groups/{gid}")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    session.expire_all()
    assert session.get(TaskGroup, gid) is None
    assert store.get("M1").group_id is None
    assert store.get("M2").group_id is None


def test_group_duplicate_name_409(client, store, user_a):
    store.lists(user_a)
    body = {"name": "Dup", "bucket_key": "NO_DATE"}
    assert client.post(f"/tasks/{MINE}/groups", json=body).status_code == 201
    resp = client.post(f"/tasks/{MINE}/groups", json=body)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "group_exists"


def test_group_unknown_404(client, store, user_a):
    store.lists(user_a)
    assert (
        client.patch(f"/tasks/{MINE}/groups/999", json={"name": "x"}).status_code == 404
    )
    assert client.delete(f"/tasks/{MINE}/groups/999").status_code == 404


# ── Per-user isolation (goal 8) ─────────────────────────────────────────────────


def test_upsert_overlay_is_user_scoped(session, store, user_a, user_b):
    """A user can only set rank/group on their own task; B targeting A's task by
    id gets None and A's row is untouched."""
    store.lists(user_a)
    store.add(user_a, MINE, "T1", rank=1.0)

    assert overlay_svc.upsert_overlay(session, user_b.id, MINE, "T1", rank=99.0) is None
    assert store.get("T1").rank == 1.0

    row = overlay_svc.upsert_overlay(session, user_a.id, MINE, "T1", rank=2.0)
    assert row is not None and row.rank == 2.0
    assert store.get("T1").rank == 2.0


def test_patch_overlay_other_users_task_404(auth, store, user_a, user_b):
    store.lists(user_a)
    store.add(user_a, MINE, "T1", rank=1.0)
    resp = auth.as_user(user_b).patch(f"/tasks/{MINE}/T1/overlay", json={"rank": 9.0})
    assert resp.status_code == 404
    assert store.get("T1").rank == 1.0


def test_get_merged_task_lists_filters_groups_by_user(session, store, user_a, user_b):
    """B's merge never sees A's groups, even for a list id they'd both name."""
    overlay_svc.create_group(
        session, user_a.id, "L1", bucket_key="NO_DATE", name="a-group", rank=1.0
    )
    raw = [{"id": "L1", "title": "My Tasks", "tasks": [_task("T1", None)]}]
    merged_b = overlay_svc.get_merged_task_lists(
        session, user_b.id, raw, view="grouped"
    )
    items_b = [it for b in merged_b[0]["buckets"] for it in b["items"]]
    assert all(it.get("type") != "group" for it in items_b)


def test_user_b_cannot_touch_a_groups(auth, store, session, user_a, user_b):
    store.lists(user_a)
    grp = overlay_svc.create_group(
        session, user_a.id, MINE, bucket_key="NO_DATE", name="g", rank=1.0
    )
    store.add(user_a, MINE, "T1", group_id=grp.id)
    c = auth.as_user(user_b)
    assert (
        c.patch(f"/tasks/{MINE}/groups/{grp.id}", json={"name": "x"}).status_code == 404
    )
    assert c.delete(f"/tasks/{MINE}/groups/{grp.id}").status_code == 404
    session.expire_all()
    assert session.get(TaskGroup, grp.id).name == "g"
    assert store.get("T1").group_id == grp.id
