"""Goal 17 — the one-time Google Tasks → local store import, its triggers, and the
"no Google Tasks calls at steady state" pin.

Google is a fake `fetch_for_import` (raw lists with Google-shaped tasks); the
importer runs for real against the in-memory DB. Service functions are async, so
sync tests drive them with `asyncio.run`.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import select

from tests.conftest import DummyCreds

from app.auth.models import User
from app.overlay.models import TaskGroup, TaskOverlay
from app.tasks_store import importer
from app.tasks_store import scheduler as import_scheduler
from app.tasks_store.models import Task, TaskList
from app.threads.models import Thread, ThreadStep

_APP = Path(__file__).resolve().parents[1] / "app"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


NOW = datetime.now(timezone.utc)
RECENT = _iso(NOW - timedelta(days=3))
OLD = _iso(NOW - timedelta(days=45))


def _g(tid, title, pos, *, status="needsAction", due=None, completed=None, notes=None):
    return {
        "id": tid,
        "title": title,
        "status": status,
        "due": due,
        "notes": notes,
        "completed": completed,
        "position": f"{pos:020d}",
    }


def _google_lists() -> list[dict]:
    return [
        {
            "id": "G_MINE",
            "title": "My Tasks",
            "tasks": [
                _g("g2", "second", 2, due="2026-10-07T00:00:00.000Z"),
                _g("g1", "first", 1, notes="note"),
                _g("g3", "done recently", 3, status="completed", completed=RECENT),
                _g("g4", "done long ago", 4, status="completed", completed=OLD),
            ],
        },
        {
            "id": "G_OTHER",
            "title": "Groceries",
            "tasks": [_g("g5", "milk", 1)],
        },
    ]


class FakeGoogle:
    def __init__(self, lists=None, error: Exception | None = None):
        self.lists = lists if lists is not None else _google_lists()
        self.error = error
        self.calls = 0

    async def fetch_for_import(self, creds):
        self.calls += 1
        if self.error:
            raise self.error
        return self.lists


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr("app.google.tasks.fetch_for_import", fake.fetch_for_import)
    return fake


@pytest.fixture
def pending(session) -> User:
    """A user whose tasks have NOT been imported yet."""
    user = User(google_sub="sub-p", email="p@example.com", tasks_imported_at=None)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _run(coro):
    return asyncio.run(coro)


def _tasks(session, user_id) -> dict[str, Task]:
    session.expire_all()
    return {
        t.id: t for t in session.exec(select(Task).where(Task.user_id == user_id)).all()
    }


def _lists(session, user_id) -> dict[str, TaskList]:
    return {
        tl.id: tl
        for tl in session.exec(
            select(TaskList).where(TaskList.user_id == user_id)
        ).all()
    }


# ── Content ──────────────────────────────────────────────────────────────────


def test_import_copies_lists_open_and_recent_completed(session, pending, google):
    report = _run(importer.import_user(session, DummyCreds(), pending.id))

    tasks = _tasks(session, pending.id)
    assert set(tasks) == {"g1", "g2", "g3", "g5"}  # g4 (45 days ago) skipped
    assert (report.open, report.completed, report.skipped_completed) == (3, 1, 1)
    assert tasks["g2"].due == date(2026, 10, 7)
    assert tasks["g1"].notes == "note"
    assert tasks["g3"].status == "completed" and tasks["g3"].completed_at
    # Google's manual order is kept: g1 (pos 1) before g2 (pos 2).
    assert tasks["g1"].position < tasks["g2"].position

    session.refresh(pending)
    assert pending.tasks_imported_at is not None


def test_import_creates_missing_pinned_list(session, pending, google):
    report = _run(importer.import_user(session, DummyCreds(), pending.id))
    titles = sorted(tl.title for tl in _lists(session, pending.id).values())
    assert titles == ["Follow-ups", "Groceries", "My Tasks"]
    assert report.pinned_created == 1


def test_import_carries_overlay_rank_and_group(session, pending, google):
    grp = TaskGroup(
        user_id=pending.id, tasklist_id="G_MINE", bucket_key="NO_DATE", name="Errands"
    )
    session.add(grp)
    session.commit()
    session.add(
        TaskOverlay(
            user_id=pending.id,
            tasklist_id="G_MINE",
            task_id="g1",
            rank=7.5,
            group_id=grp.id,
        )
    )
    session.commit()

    report = _run(importer.import_user(session, DummyCreds(), pending.id))

    g1 = _tasks(session, pending.id)["g1"]
    assert (g1.rank, g1.group_id) == (7.5, grp.id)
    assert report.overlay_matched == 1


def test_thread_links_survive_and_missing_open_steps_are_reported(
    session, pending, google
):
    thread = Thread(user_id=pending.id, title="A thread")
    session.add(thread)
    session.commit()
    session.add_all(
        [
            ThreadStep(
                thread_id=thread.id,
                user_id=pending.id,
                position=1.0,
                kind="next",
                label="first",
                tasklist_id="G_MINE",
                task_id="g1",
            ),
            ThreadStep(
                thread_id=thread.id,
                user_id=pending.id,
                position=2.0,
                kind="next",
                label="gone",
                tasklist_id="G_MINE",
                task_id="g-deleted",
            ),
        ]
    )
    session.commit()

    report = _run(importer.import_user(session, DummyCreds(), pending.id))

    # Ids are kept, so the linked step still points at a real task.
    assert "g1" in _tasks(session, pending.id)
    assert report.open_steps_unlinked == 1


# ── Guarantees ───────────────────────────────────────────────────────────────


def test_failure_rolls_back_and_stays_pending(session, pending, monkeypatch):
    # A list containing a task without an id blows up mid-import.
    bad = _google_lists()
    bad[1]["tasks"].append({"title": "broken", "status": "needsAction"})
    fake = FakeGoogle(lists=bad)
    monkeypatch.setattr("app.google.tasks.fetch_for_import", fake.fetch_for_import)

    with pytest.raises(Exception):
        _run(importer.import_user(session, DummyCreds(), pending.id))

    assert _tasks(session, pending.id) == {}
    assert _lists(session, pending.id) == {}
    session.refresh(pending)
    assert pending.tasks_imported_at is None


def test_google_error_leaves_user_pending(session, pending, monkeypatch):
    fake = FakeGoogle(error=RuntimeError("429 quota"))
    monkeypatch.setattr("app.google.tasks.fetch_for_import", fake.fetch_for_import)
    with pytest.raises(RuntimeError):
        _run(importer.import_user(session, DummyCreds(), pending.id))
    session.refresh(pending)
    assert pending.tasks_imported_at is None


def test_second_run_is_noop_and_force_reimports(session, pending, google):
    _run(importer.import_user(session, DummyCreds(), pending.id))
    assert google.calls == 1

    _run(importer.import_user(session, DummyCreds(), pending.id))
    assert google.calls == 1  # already imported → skipped before any Google call

    google.lists[0]["tasks"].append(_g("g9", "new in google", 9))
    _run(importer.import_user(session, DummyCreds(), pending.id, force=True))
    assert set(_tasks(session, pending.id)) == {"g1", "g2", "g3", "g5", "g9"}


def test_dry_run_writes_nothing(session, pending, google):
    report = _run(importer.import_user(session, DummyCreds(), pending.id, dry_run=True))
    assert report.dry_run and report.open == 3
    assert _tasks(session, pending.id) == {}
    session.refresh(pending)
    assert pending.tasks_imported_at is None


def test_import_is_user_scoped(session, pending, user_a, store, google):
    store.lists(user_a)
    store.add(user_a, "L_MINE", "a1", "user A's task")

    _run(importer.import_user(session, DummyCreds(), pending.id, force=True))

    assert store.get("a1") is not None
    assert set(_tasks(session, user_a.id)) == {"a1"}
    assert "a1" not in _tasks(session, pending.id)


# ── Triggers ─────────────────────────────────────────────────────────────────


def test_first_load_imports_pending_user(auth, session, pending, google, monkeypatch):
    monkeypatch.setattr(
        "app.tasks_store.deps.get_current_credentials", lambda u, s: DummyCreds()
    )
    r = auth.as_user(pending).get("/tasks")
    assert r.status_code == 200, r.text
    titles = {tl["title"] for tl in r.json()["task_lists"]}
    assert {"My Tasks", "Follow-ups", "Groceries"} <= titles


def test_first_load_failure_is_503_and_retry_works(auth, pending, monkeypatch):
    monkeypatch.setattr(
        "app.tasks_store.deps.get_current_credentials", lambda u, s: DummyCreds()
    )
    fake = FakeGoogle(error=RuntimeError("429 quota"))
    monkeypatch.setattr("app.google.tasks.fetch_for_import", fake.fetch_for_import)
    client = auth.as_user(pending)

    r = client.get("/tasks")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "tasks_import_pending"

    fake.error = None
    assert client.get("/tasks").status_code == 200


def test_imported_user_never_loads_creds(client, store, user_a, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("credentials must not load for an imported user")

    monkeypatch.setattr("app.tasks_store.deps.get_current_credentials", boom)
    store.lists(user_a)
    assert client.get("/tasks").status_code == 200


def test_sweep_imports_pending_users(engine, session, pending, google, monkeypatch):
    pending.refresh_token_encrypted = "enc"
    session.add(pending)
    session.commit()
    monkeypatch.setattr(import_scheduler, "engine", engine)
    monkeypatch.setattr(
        import_scheduler.google_auth, "load_credentials", lambda s, u: DummyCreds()
    )

    tally = _run(import_scheduler.sweep())

    assert tally["imported"] == 1
    session.expire_all()
    assert session.get(User, pending.id).tasks_imported_at is not None
    assert _run(import_scheduler.sweep())["imported"] == 0  # nothing left


def test_threads_skip_reconcile_before_import(auth, session, pending):
    thread = Thread(user_id=pending.id, title="A thread")
    session.add(thread)
    session.commit()
    session.add(
        ThreadStep(
            thread_id=thread.id,
            user_id=pending.id,
            position=1.0,
            kind="next",
            label="linked",
            tasklist_id="G_MINE",
            task_id="g1",
        )
    )
    session.commit()

    r = auth.as_user(pending).get("/threads")
    assert r.status_code == 200
    steps = r.json()["threads"][0]["steps"]
    assert [s["label"] for s in steps] == ["linked"]  # not reconciled away


# ── Steady state: no Google Tasks calls ──────────────────────────────────────


def _imports_google_tasks(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app.google.tasks":
                return True
            if node.module == "app.google" and any(
                a.name == "tasks" for a in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(
            a.name == "app.google.tasks" for a in node.names
        ):
            return True
    return False


def test_only_the_importer_imports_google_tasks():
    users = sorted(
        str(p.relative_to(_APP)) for p in _APP.rglob("*.py") if _imports_google_tasks(p)
    )
    assert users == ["tasks_store/importer.py"]
