"""Tests for the goal-5 auto-router: route/dispose logic, the create-only guardrail,
route-once idempotency, the review queue, and the pure eval scorer.

Both the classifier (the runtime LLM) and Google are fully mocked — no API key, no
network. The guardrail tests are the gate-critical ones: they prove routing can NEVER
reach a destructive Google writer (statically via AST, and dynamically by recording
every call across every routing path).

Goal 8: `route_entry`/`confirm_review` take the full `User` + live `creds`; every
Google client fn takes `creds` first; scratch/review rows are user-scoped; and a note's
Doc is the user's own — auto-bootstrapped via `settings_svc.ensure_notes_target`
(create_folder → create_doc_in_folder), not an env var. Service functions are async;
sync tests drive them with `run(...)` (asyncio.run) so we need no async-pytest plugin.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json

import pytest
from sqlmodel import select

from tests.conftest import DummyCreds

from app.errors import ApiError
from app.google import docs as docs_mod
from app.router import service as router_svc
from app.router.models import (
    IN_REVIEW,
    KEPT_NOTE,
    PENDING,
    RESOLVED,
    ROUTED_TASK,
    ROUTING,
    UNROUTED,
    ReviewItem,
    ScratchEntry,
)
from app.router.schema import RouterClassification, RouterFields


def run(coro):
    return asyncio.run(coro)


# ── Fixtures ──────────────────────────────────────────────────────────────────


class Google:
    """Records every Google write so we can assert what routing did (and didn't) touch.

    Every wrapper takes `creds` first (goal 8); it is dropped from the recorded tuple."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.tasks: dict[tuple[str, str], dict] = {}
        self._next_id = 0

    async def get_task_lists(self, creds):
        self.calls.append(("get_task_lists",))
        return [
            {"id": "L1", "title": "My Tasks", "tasks": []},
            {"id": "L2", "title": "Follow-ups", "tasks": []},
        ]

    async def get_task(self, creds, list_id, task_id):
        self.calls.append(("get_task", list_id, task_id))
        return self.tasks.get((list_id, task_id))

    async def insert_task(self, creds, list_id, body):
        self.calls.append(("insert_task", list_id, body))
        self._next_id += 1
        tid = f"new-{self._next_id}"
        task = {
            "id": tid,
            "title": body.get("title", ""),
            "status": body.get("status", "needsAction"),
            "due": body.get("due"),
            "notes": body.get("notes"),
            "parent": None,
        }
        self.tasks[(list_id, tid)] = task
        return task

    async def update_due_date(self, creds, list_id, task_id, due):
        self.calls.append(("update_due_date", list_id, task_id, due))

    async def delete_task(self, creds, list_id, task_id):  # must NEVER be routed
        self.calls.append(("delete_task", list_id, task_id))

    async def update_task_content(self, creds, list_id, task_id, **fields):  # never
        self.calls.append(("update_task_content", list_id, task_id, fields))
        return self.tasks.get((list_id, task_id), {"id": task_id})

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def google(monkeypatch):
    rec = Google()
    monkeypatch.setattr("app.google.tasks.get_task_lists", rec.get_task_lists)
    monkeypatch.setattr("app.google.tasks.get_task", rec.get_task)
    monkeypatch.setattr("app.google.tasks.insert_task", rec.insert_task)
    monkeypatch.setattr("app.google.tasks.update_due_date", rec.update_due_date)
    monkeypatch.setattr("app.google.tasks.delete_task", rec.delete_task)
    monkeypatch.setattr("app.google.tasks.update_task_content", rec.update_task_content)
    return rec


@pytest.fixture
def fake_classify(monkeypatch):
    """Patch the runtime LLM with a canned classification (set via `_set`)."""

    holder = {"result": RouterClassification(destination="unknown", confidence=0.0)}

    async def _classify(text: str, doc_paths=None):
        holder["doc_paths"] = doc_paths  # let tests assert the injected hierarchy
        return holder["result"]

    monkeypatch.setattr("app.router.service.classify", _classify)
    return holder


@pytest.fixture
def notes(monkeypatch):
    """Mock the goal-8 Docs/Drive surface + the notes-target bootstrap.

    A note's Doc is the user's own, auto-created on first need: the router calls
    `settings_svc.ensure_notes_target`, which — with unset UserSettings ids — calls
    `create_folder` then `create_doc_in_folder`, then `append_note` runs its ancestry
    gate (`get_parents`) + `insert_note`. We record inserts and make ancestry pass
    (parents of the created doc include the created folder)."""
    from app.settings import service as settings_svc
    from app.writes import service as writes_svc

    writes_svc._ancestry_ok.clear()
    settings_svc._verified_targets.clear()
    rec = {"insert": [], "folder_id": "FOLDER-boot", "doc_id": "DOC-boot"}

    async def _create_folder(creds, name):
        return rec["folder_id"]

    async def _file_accessible(creds, file_id):
        # Goal-8a self-heal probe: the bootstrapped ids are always still ours here.
        return True

    async def _create_doc_in_folder(creds, title, folder_id):
        return rec["doc_id"]

    async def _get_parents(creds, file_id):
        # The bootstrapped doc lives directly in the bootstrapped folder.
        if file_id == rec["doc_id"]:
            return [rec["folder_id"]]
        return []

    async def _insert_note(creds, doc_id, heading, body, summary=None, keywords=None):
        rec["insert"].append((doc_id, heading, body, summary, keywords))

    monkeypatch.setattr("app.google.docs.create_folder", _create_folder)
    monkeypatch.setattr("app.google.docs.create_doc_in_folder", _create_doc_in_folder)
    monkeypatch.setattr("app.google.docs.get_parents", _get_parents)
    monkeypatch.setattr("app.google.docs.insert_note", _insert_note)
    monkeypatch.setattr("app.google.docs.file_accessible", _file_accessible)
    return rec


def _set(holder, destination, confidence, **fields):
    holder["result"] = RouterClassification(
        destination=destination, confidence=confidence, fields=RouterFields(**fields)
    )


def _entry(session, user, text="something"):
    e = ScratchEntry(user_id=user.id, text=text)
    session.add(e)
    session.commit()
    session.refresh(e)
    return e


# ── Dispose: each destination ─────────────────────────────────────────────────


def test_high_conf_task_creates_one_task(session, user_a, google, fake_classify):
    _set(fake_classify, "task", 0.95, title="call plumber", due_date="2026-06-20")
    state = run(
        router_svc.route_entry(
            session,
            user_a,
            DummyCreds(),
            _entry(session, user_a, "call plumber friday"),
        )
    )
    assert state == ROUTED_TASK
    assert google.names().count("insert_task") == 1
    assert "update_due_date" in google.names()  # due via reschedule (metadata)
    assert "delete_task" not in google.names()
    assert "update_task_content" not in google.names()


def test_unhinted_task_targets_my_tasks_not_first_list(
    session, user_a, google, fake_classify, monkeypatch
):
    """Regression: an unhinted task must land in the dashboard's pinned "My Tasks"
    list, NOT Google's first-returned list. On accounts where the first list isn't
    "My Tasks" (e.g. a pre-existing default + hand-created pinned lists), filing into
    raw_lists[0] created the task successfully but the dashboard never rendered it."""

    async def reordered_lists(creds):
        # "My Tasks" is NOT first here — the buggy fallback would pick "Personal".
        return [
            {"id": "L0", "title": "Personal", "tasks": []},
            {"id": "L1", "title": "My Tasks", "tasks": []},
            {"id": "L2", "title": "Follow-ups", "tasks": []},
        ]

    monkeypatch.setattr("app.google.tasks.get_task_lists", reordered_lists)
    _set(fake_classify, "task", 0.95, title="scold aayush", due_date=None)
    state = run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "scold aayush")
        )
    )
    assert state == ROUTED_TASK
    inserts = [c for c in google.calls if c[0] == "insert_task"]
    assert len(inserts) == 1
    assert inserts[0][1] == "L1"  # "My Tasks", not "L0" (Personal)


def test_task_targeting_followups_lands_in_followups(
    session, user_a, google, fake_classify
):
    """A task the classifier tags target_list="Follow-ups" is filed into that list
    (L2), never My Tasks — the router honours the LLM's two-way list choice."""
    _set(fake_classify, "task", 0.95, title="ping Ravi", target_list="Follow-ups")
    state = run(
        router_svc.route_entry(
            session,
            user_a,
            DummyCreds(),
            _entry(session, user_a, "follow up with ravi"),
        )
    )
    assert state == ROUTED_TASK
    inserts = [c for c in google.calls if c[0] == "insert_task"]
    assert len(inserts) == 1
    assert inserts[0][1] == "L2"  # "Follow-ups"


def test_task_routing_leaves_entry_unrouted_when_no_pinned_lists(
    session, user_a, google, fake_classify, monkeypatch
):
    """Opinionated: the router files ONLY into the two pinned lists. If an account has
    neither, routing raises (never dumps into a third list) and the entry stays
    re-routable — surfacing the two-list prerequisite instead of silently misfiling."""

    async def other_lists(creds):
        return [{"id": "LX", "title": "Personal", "tasks": []}]

    monkeypatch.setattr("app.google.tasks.get_task_lists", other_lists)
    _set(fake_classify, "task", 0.95, title="buy milk", due_date=None)
    entry = _entry(session, user_a, "buy milk")
    with pytest.raises(ApiError):
        run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    assert "insert_task" not in google.names()
    session.expire_all()
    assert session.get(ScratchEntry, entry.id).routing_state == UNROUTED


def test_high_conf_note_bootstraps_doc_and_writes_verbatim(
    session, user_a, google, fake_classify, notes
):
    """Goal 8/9: with no notes Doc yet, the router bootstraps the user's own folder+Doc
    (`ensure_notes_target`), then writes exactly one Docs insert under an H3 timestamp
    — no task write. The body is the prefix-stripped `note_text` (here, no prefix, so
    it equals the raw verbatim capture)."""
    body = "- strategy idea\n  - sub point\n- another line"
    _set(fake_classify, "note", 0.95, note_text=body)
    state = run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, body)
        )
    )
    assert state == KEPT_NOTE
    assert len(notes["insert"]) == 1
    doc_id, heading, written, _summary, _kw = notes["insert"][0]
    assert doc_id == notes["doc_id"]  # the app-bootstrapped Doc
    assert written == body  # verbatim — bullets/indentation preserved
    assert heading.endswith("IST")
    assert "insert_task" not in google.names()


def test_note_ancestry_gate_rejects_doc_outside_folder(
    session, user_a, google, fake_classify, notes, monkeypatch
):
    """A doc whose parents don't reach the notes folder is rejected fail-closed — no
    insert, entry left re-routable."""

    async def _bad_parents(creds, file_id):
        return ["SOME_OTHER_FOLDER"]

    monkeypatch.setattr("app.google.docs.get_parents", _bad_parents)
    _set(fake_classify, "note", 0.95, note_text="x")
    entry = _entry(session, user_a, "note outside the folder")
    with pytest.raises(ApiError):
        run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    assert notes["insert"] == []
    session.expire_all()
    assert session.get(ScratchEntry, entry.id).routing_state == UNROUTED


def test_note_docs_failure_leaves_entry_unrouted(
    session, user_a, google, fake_classify, notes, monkeypatch
):
    """A Docs write failure surfaces (never swallowed) and leaves the entry
    re-routable — route-once marks routed only on a successful append."""

    async def _boom(creds, doc_id, heading, body, summary=None, keywords=None):
        raise RuntimeError("docs down")

    monkeypatch.setattr("app.google.docs.insert_note", _boom)
    _set(fake_classify, "note", 0.95, note_text="x")
    entry = _entry(session, user_a, "boom note")
    with pytest.raises(ApiError):
        run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    session.expire_all()
    assert session.get(ScratchEntry, entry.id).routing_state == UNROUTED


def test_event_goes_to_review_no_writes(session, user_a, google, fake_classify):
    _set(fake_classify, "event", 0.95, title="lunch", event_datetime="thu 1pm")
    state = run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "lunch with Tejas")
        )
    )
    assert state == IN_REVIEW
    assert google.calls == []
    rows = session.exec(select(ReviewItem)).all()
    assert len(rows) == 1 and rows[0].status == PENDING


def test_unknown_goes_to_review(session, user_a, google, fake_classify):
    _set(fake_classify, "unknown", 0.1)
    assert (
        run(
            router_svc.route_entry(
                session, user_a, DummyCreds(), _entry(session, user_a, "huh")
            )
        )
        == IN_REVIEW
    )
    assert google.calls == []


def test_low_confidence_task_goes_to_review_not_written(
    session, user_a, google, fake_classify
):
    _set(fake_classify, "task", 0.4, title="maybe ping someone")
    assert (
        run(
            router_svc.route_entry(
                session, user_a, DummyCreds(), _entry(session, user_a, "ping?")
            )
        )
        == IN_REVIEW
    )
    assert "insert_task" not in google.names()


def test_review_item_note_text_guarded_at_creation(client, google, fake_classify):
    """Goal 10: a review item built from a mangled/short `note_text` stores the RAW
    capture verbatim in `fields_json` (the truncation guard moved server-side), so the
    editor prefill sees the un-mangled text — not the low-conf extraction it declined
    to auto-file."""
    raw = "a long verbatim capture that must survive into the review editor intact"
    _set(fake_classify, "note", 0.3, note_text="x")  # far under 50% of raw → guarded
    client.post("/scratch", json={"text": raw})
    item = client.get("/review").json()["items"][0]
    fields = json.loads(item["fields"])
    assert fields["note_text"] == raw  # raw wins, not the mangled "x"


# ── Route-once idempotency ────────────────────────────────────────────────────


def test_route_once_does_not_recreate(session, user_a, google, fake_classify):
    _set(fake_classify, "task", 0.95, title="buy milk")
    entry = _entry(session, user_a, "buy milk")
    run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    state2 = run(router_svc.route_entry(session, user_a, DummyCreds(), entry))  # no-op
    assert state2 == ROUTED_TASK
    assert google.names().count("insert_task") == 1


def test_injected_classification_skips_the_llm(session, user_a, google, fake_classify):
    """An injected classification (from the capture undo-window pre-classify) is used
    verbatim and the runtime LLM is NOT called again — dispose stays deterministic."""
    # The LLM, if consulted, would say "unknown" → review (never a task write).
    _set(fake_classify, "unknown", 0.0)
    entry = _entry(session, user_a, "call plumber")
    injected = RouterClassification(
        destination="task",
        confidence=0.95,
        fields=RouterFields(title="call plumber"),
    )
    state = run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), entry, classification=injected
        )
    )
    # Injected proposal won → a task was filed; the "unknown" LLM path was skipped.
    assert state == ROUTED_TASK
    assert google.names().count("insert_task") == 1
    assert "doc_paths" not in fake_classify  # classify() was never awaited


def test_classify_text_does_not_dispose(session, user_a, google, fake_classify):
    """`classify_text` is pure: it returns the proposal and writes nothing."""
    _set(fake_classify, "task", 0.95, title="buy milk")
    result = run(router_svc.classify_text(session, user_a.id, "buy milk"))
    assert result.destination == "task"
    assert "insert_task" not in google.names()


def test_route_unrouted_tally_then_noop(session, user_a, google, fake_classify, notes):
    _set(fake_classify, "note", 0.95, note_text="x")
    for _ in range(3):
        _entry(session, user_a, "a note")
    tally = run(router_svc.route_unrouted(session, user_a, DummyCreds()))
    assert tally["kept_note"] == 3
    assert run(router_svc.route_unrouted(session, user_a, DummyCreds())) == {
        "routed_task": 0,
        "kept_note": 0,
        "in_review": 0,
        "failed": 0,
    }


# ── THE GUARDRAIL ─────────────────────────────────────────────────────────────


def test_router_never_calls_delete_or_status(
    session, user_a, google, fake_classify, notes
):
    """Drive every routing destination; assert delete_task and the status/complete
    write are NEVER called — the insert-only blast-radius contract, dynamically."""
    scenarios = [
        ("task", 0.95, {"title": "t", "due_date": "2026-06-20"}),
        ("task", 0.3, {"title": "t"}),
        ("note", 0.95, {"note_text": "n"}),
        ("note", 0.2, {"note_text": "n"}),
        ("event", 0.95, {"title": "e"}),
        ("unknown", 0.05, {}),
    ]
    # Neutral capture texts (no routing-header keyword) so each scenario's confidence
    # governs its disposition — the goal-10 header forcing is exercised separately.
    for i, (dest, conf, fields) in enumerate(scenarios):
        _set(fake_classify, dest, conf, **fields)
        run(
            router_svc.route_entry(
                session, user_a, DummyCreds(), _entry(session, user_a, f"capture {i}")
            )
        )

    forbidden = {"delete_task", "update_task_content"}
    assert forbidden.isdisjoint(google.names()), google.names()
    assert google.names().count("insert_task") == 1  # the one high-conf task


def test_router_write_dependency_set_is_insert_only():
    """Statically: every `writes_svc.<fn>(...)` call reachable in the router service
    is in {create_task, reschedule, append_note} (goal 7). No destructive writer —
    no delete_task, status write, update_content, or Docs overwrite — is referenced."""
    tree = ast.parse(inspect.getsource(router_svc))
    called = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "writes_svc"
        ):
            called.add(node.func.attr)
    assert called == {"create_task", "reschedule", "append_note"}, called


def _calls_in_function(mod, fn_name: str) -> set[str]:
    """Method names called (obj.method(...)) inside a single function of a module."""
    tree = ast.parse(inspect.getsource(mod))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
    )
    return {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


def test_docs_module_write_surface_is_insert_only():
    """Statically: the Docs/Drive client never deletes a file, and its ONLY
    `files().update` is the goal-9 metadata rename living in `_rename_file` (never a
    content overwrite). The other mutations are the insert-only `documents().batchUpdate`
    and the sanctioned `files().create`. Drive-access-scoping ADR, layer 5."""
    from app.google import docs as docs_mod

    tree = ast.parse(inspect.getsource(docs_mod))
    called_methods = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "delete" not in called_methods, called_methods
    assert "batchUpdate" in called_methods  # the insert-only note write
    assert "create" in called_methods  # the sanctioned file-create (bootstrap)
    # `update` may appear now — but ONLY inside `_rename_file` (metadata rename).
    assert "update" in _calls_in_function(docs_mod, "_rename_file")
    for other in ("_insert_note", "_create_folder", "_create_doc_in_folder"):
        assert "update" not in _calls_in_function(docs_mod, other), other


def test_docs_get_document_is_read_only():
    """Goal 12: the ONE sanctioned Docs read path — `documents().get` inside
    `_get_document` — carries no write method (no batchUpdate/create/update/delete), so
    the dev-view entry scan reads app-created Docs without touching the write surface."""
    from app.google import docs as docs_mod

    read_calls = _calls_in_function(docs_mod, "_get_document")
    assert "get" in read_calls
    for write in ("batchUpdate", "create", "update", "delete"):
        assert write not in read_calls, write


def test_rename_file_body_is_name_only(monkeypatch):
    """The rename request body is EXACTLY `{"name": ...}` — never content, parents,
    or trashed (goal 9 pinned unit test)."""
    from app.google import docs as docs_mod

    captured: dict = {}

    class FakeFiles:
        def update(self, fileId, body, fields):
            captured["fileId"] = fileId
            captured["body"] = body
            return self

        def execute(self):
            return {"id": captured["fileId"]}

    class FakeDrive:
        def files(self):
            return FakeFiles()

    monkeypatch.setattr(docs_mod, "_drive_service", lambda _creds: FakeDrive())
    run(docs_mod.rename_file(DummyCreds(), "FILE-1", "New Name"))
    assert captured["fileId"] == "FILE-1"
    assert captured["body"] == {"name": "New Name"}


def test_router_never_reaches_rename_file():
    """The metadata rename is a settings-path-only caller — the router service never
    references `rename_file` (goal 9; keeps the router's mutation surface exactly the
    insert-only set)."""
    src = inspect.getsource(router_svc)
    assert "rename_file" not in src


# ── Goal 10: the routing-header contract (LLM-interpreted, code-enforced) ──────


def _ist_offset_iso(days: int) -> str:
    import datetime
    import zoneinfo

    base = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata")).date()
    return f"{(base + datetime.timedelta(days=days)).isoformat()}T00:00:00.000Z"


def test_header_order_insensitive_task_date_backstop(
    session, user_a, google, fake_classify
):
    """'tomorrow task pay the plumber' ≡ 'task tomorrow pay the plumber': both file a
    task under tomorrow's bucket even when the classifier leaves due_date null — the
    header date word backstops the null date (order-insensitive)."""
    for text in ("tomorrow task pay the plumber", "task tomorrow pay the plumber"):
        _set(fake_classify, "task", 0.95, title="pay the plumber", due_date=None)
        state = run(
            router_svc.route_entry(
                session, user_a, DummyCreds(), _entry(session, user_a, text)
            )
        )
        assert state == ROUTED_TASK
    updates = [c for c in google.calls if c[0] == "update_due_date"]
    assert len(updates) == 2
    assert all(c[3] == _ist_offset_iso(1) for c in updates)  # both due tomorrow


def test_keyword_task_header_forces_task_past_confidence_gate(
    session, user_a, google, fake_classify
):
    """An explicit 'task' header files a task even at LOW confidence — a keyword is user
    intent, not a probability, and must never bounce to review (the confidence gate is
    bypassed for the destination)."""
    _set(fake_classify, "task", 0.2, title="file the report")
    state = run(
        router_svc.route_entry(
            session,
            user_a,
            DummyCreds(),
            _entry(session, user_a, "task file the report"),
        )
    )
    assert state == ROUTED_TASK
    assert google.names().count("insert_task") == 1
    assert session.exec(select(ReviewItem)).all() == []


def test_note_header_forces_note_over_wrong_destination(
    session, user_a, google, fake_classify, notes
):
    """A 'notes …' header over a task-looking body the LLM classified as a TASK still
    files a note — degraded safe: body = raw minus the header, no summary/keywords,
    default Doc. No task is ever created."""
    _set(fake_classify, "task", 0.95, title="do the thing")  # LLM disagrees
    entry = _entry(
        session, user_a, "notes daily syncup - action items: do the thing and more"
    )
    state = run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    assert state == KEPT_NOTE
    assert "insert_task" not in google.names()
    _doc, _heading, body, summary, kw = notes["insert"][0]
    assert body == "action items: do the thing and more"  # raw minus the header
    assert summary is None and kw is None


def test_note_header_low_conf_files_note_never_review(
    session, user_a, google, fake_classify, notes
):
    """The observed bug: 'notes <leaf> - <MOM>' the LLM classified a note but UNDER
    threshold. The header forces it past the gate — filed, never bounced. The LLM did
    produce note fields, so its note_text + summary are used (not degraded)."""
    _set(
        fake_classify,
        "note",
        0.3,
        note_text="the whole meeting summary kept verbatim here",
        summary="sync",
    )
    entry = _entry(
        session,
        user_a,
        "notes daily syncup - the whole meeting summary kept verbatim here",
    )
    state = run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    assert state == KEPT_NOTE
    assert session.exec(select(ReviewItem)).all() == []
    _doc, _heading, body, summary, _kw = notes["insert"][0]
    assert body == "the whole meeting summary kept verbatim here"
    assert summary == "sync"


def test_header_date_backstop_does_not_override_llm_due(
    session, user_a, google, fake_classify
):
    """The backstop fills a NULL classifier due; a non-null LLM due is left untouched."""
    _set(fake_classify, "task", 0.95, title="x", due_date="2026-08-01")
    run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "tomorrow task x")
        )
    )
    updates = [c for c in google.calls if c[0] == "update_due_date"]
    assert (
        updates[-1][3] == "2026-08-01T00:00:00.000Z"
    )  # LLM due wins over the backstop


def test_no_header_long_capture_uses_body_inference(
    session, user_a, google, fake_classify
):
    """A capture with no header (a long leading segment, no keyword) routes purely by
    the LLM's body inference + the confidence gate — goal-9 behaviour, unchanged."""
    text = "please remember to eventually call the plumber about the leak downstairs"
    _set(fake_classify, "task", 0.2, title="call plumber")  # low conf, no header force
    state = run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, text)
        )
    )
    assert state == IN_REVIEW  # gate applies (no keyword to force it through)
    assert "insert_task" not in google.names()


# ── Write-failure leaves the entry re-routable ────────────────────────────────


def test_write_failure_leaves_entry_unrouted(
    session, user_a, monkeypatch, google, fake_classify
):
    _set(fake_classify, "task", 0.95, title="boom")

    async def _boom(creds, list_id, body):
        raise RuntimeError("google down")

    monkeypatch.setattr("app.google.tasks.insert_task", _boom)
    entry = _entry(session, user_a, "boom")
    with pytest.raises(ApiError):
        run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    session.expire_all()
    assert session.get(ScratchEntry, entry.id).routing_state == UNROUTED


# ── Review queue dispositions ─────────────────────────────────────────────────


def test_confirm_task_review_fires_one_create(session, user_a, google, fake_classify):
    _set(fake_classify, "event", 0.95, title="lunch")  # lands in review
    run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "lunch maybe")
        )
    )
    item = session.exec(select(ReviewItem)).first()
    res = run(
        router_svc.confirm_review(
            session,
            user_a,
            DummyCreds(),
            item.id,
            destination="task",
            fields=RouterFields(title="lunch with Tejas", due_date="2026-06-20"),
        )
    )
    assert res["entry_state"] == ROUTED_TASK
    assert google.names().count("insert_task") == 1


def test_dismiss_writes_nothing(session, user_a, google, fake_classify):
    _set(fake_classify, "unknown", 0.1)
    entry = _entry(session, user_a, "huh")
    run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    item = session.exec(select(ReviewItem)).first()
    res = run(router_svc.dismiss_review(session, user_a, item.id))
    assert res["status"] == "dismissed"
    assert google.calls == []
    session.expire_all()
    assert session.get(ScratchEntry, entry.id).routing_state == RESOLVED


def test_confirm_event_acknowledges_no_write(session, user_a, google, fake_classify):
    _set(fake_classify, "event", 0.95, title="standup")
    run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "standup 10am")
        )
    )
    item = session.exec(select(ReviewItem)).first()
    res = run(router_svc.confirm_review(session, user_a, DummyCreds(), item.id))
    assert res["entry_state"] == RESOLVED
    assert "insert_task" not in google.names()


# ── Endpoints ─────────────────────────────────────────────────────────────────


def test_capture_persists_and_lists(client, google, fake_classify):
    """Capture persists + is listed. It also routes inline (goal 7c) — an event
    proposal lands in review, but the raw entry is stored append-only regardless."""
    _set(fake_classify, "event", 0.95, title="lunch")
    r = client.post("/scratch", json={"text": "  a thought  "})
    assert r.status_code == 201
    body = r.json()
    assert body["text"] == "a thought"
    assert client.get("/scratch").json()["entries"][0]["id"] == body["id"]


def test_capture_routes_inline(client, google, fake_classify):
    """Goal 7c: the POST response carries the routed state — a high-confidence task
    is created in Google without waiting for any scheduler tick."""
    _set(fake_classify, "task", 0.95, title="buy milk")
    r = client.post("/scratch", json={"text": "buy milk"})
    assert r.status_code == 201
    assert r.json()["routing_state"] == ROUTED_TASK
    assert google.names().count("insert_task") == 1


def test_capture_inline_failure_leaves_unrouted(
    client, google, fake_classify, monkeypatch
):
    """A Google failure during inline routing still returns 2xx (capture never
    lost) and leaves the entry UNROUTED for the scheduler backstop to retry."""

    async def _boom(creds, list_id, body):
        raise RuntimeError("google down")

    monkeypatch.setattr("app.google.tasks.insert_task", _boom)
    _set(fake_classify, "task", 0.95, title="boom")
    r = client.post("/scratch", json={"text": "boom"})
    assert r.status_code == 201
    assert r.json()["routing_state"] == UNROUTED


def test_capture_empty_400(client):
    assert client.post("/scratch", json={"text": "   "}).status_code == 400


def test_route_now_endpoint_is_backstop_noop(client, google, fake_classify, notes):
    """Capture routes inline now, so route-now (the backstop) finds nothing to do."""
    _set(fake_classify, "note", 0.95, note_text="x")
    client.post("/scratch", json={"text": "a note"})  # routes inline → kept_note
    r = client.post("/scratch/route-now")
    assert r.status_code == 200
    assert r.json()["tally"] == {
        "routed_task": 0,
        "kept_note": 0,
        "in_review": 0,
        "failed": 0,
    }


def test_review_confirm_endpoint(client, google, fake_classify):
    _set(fake_classify, "event", 0.95, title="lunch")
    client.post("/scratch", json={"text": "lunch with Sam thursday"})  # → review inline
    items = client.get("/review").json()["items"]
    assert len(items) == 1
    r = client.post(
        f"/review/{items[0]['id']}/confirm",
        json={"destination": "task", "fields": {"title": "lunch with Sam"}},
    )
    assert r.status_code == 200 and r.json()["entry_state"] == ROUTED_TASK
    assert google.names().count("insert_task") == 1
    assert client.get("/review").json()["items"] == []  # left the queue


# ── Two-user isolation (goal 8 headline AC) ─────────────────────────────────────


def test_scratch_list_is_per_user(auth, user_a, user_b, session, google, fake_classify):
    """B's GET /scratch returns only B's entries — A's captures are invisible."""
    _set(fake_classify, "unknown", 0.1)  # everything lands in review, no writes
    client_a = auth.as_user(user_a)
    client_a.post("/scratch", json={"text": "a-secret"})
    client_b = auth.as_user(user_b)
    client_b.post("/scratch", json={"text": "b-thought"})

    texts_b = [e["text"] for e in client_b.get("/scratch").json()["entries"]]
    assert texts_b == ["b-thought"]
    texts_a = [
        e["text"] for e in auth.as_user(user_a).get("/scratch").json()["entries"]
    ]
    assert texts_a == ["a-secret"]


def test_cannot_confirm_or_dismiss_other_users_review(
    auth, user_a, user_b, session, google, fake_classify
):
    """B cannot confirm or dismiss A's review item — 404 (no cross-tenant read by id)."""
    _set(fake_classify, "event", 0.95, title="lunch")  # → review inline for A
    client_a = auth.as_user(user_a)
    client_a.post("/scratch", json={"text": "a's lunch"})
    item_id = client_a.get("/review").json()["items"][0]["id"]

    client_b = auth.as_user(user_b)
    assert (
        client_b.post(
            f"/review/{item_id}/confirm", json={"destination": "task"}
        ).status_code
        == 404
    )
    assert client_b.post(f"/review/{item_id}/dismiss").status_code == 404
    # A's item is still pending + no Google write leaked.
    assert len(auth.as_user(user_a).get("/review").json()["items"]) == 1
    assert "insert_task" not in google.names()


def test_review_queries_are_user_scoped(session, user_a, user_b, google, fake_classify):
    """Service-level: a review item created for A is invisible to B (404 on lookup)."""
    _set(fake_classify, "unknown", 0.1)
    run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "a thought")
        )
    )
    item = session.exec(select(ReviewItem)).first()
    # B cannot dismiss A's item.
    with pytest.raises(ApiError):
        run(router_svc.dismiss_review(session, user_b, item.id))
    # A can.
    res = run(router_svc.dismiss_review(session, user_a, item.id))
    assert res["status"] == "dismissed"


# ── Pure eval scorer (no API) ─────────────────────────────────────────────────


def test_eval_score_perfect_passes():
    from app.router.evals.runner import load_cases, score

    results = []
    for c in load_cases():
        amb = bool(c.get("ambiguous"))
        results.append(
            {
                "text": c["text"],
                "expected": c["destination"],
                "ambiguous": amb,
                "predicted": c["destination"] if not amb else "unknown",
                "confidence": 0.4 if amb else 0.95,
                "fields": {
                    "title": c["text"],
                    "due_date": "2026-06-20" if c.get("expects_due") else None,
                    "target_list": c.get("target_list"),
                    # A perfect run also nails the goal-9 doc-path + prefix strip.
                    "target_doc_path": c.get("doc_path_expect") or None,
                    "note_text": c.get("strip_expect"),
                },
                "case": c,
            }
        )
    card = score(results)
    assert card["clear_accuracy"] == 1.0
    assert card["task_false_positives"] == 0
    assert card["passed"] is True


def test_eval_score_catches_task_false_positive():
    from app.router.evals.runner import score

    results = [
        {
            "text": "remember x",
            "expected": "note",
            "ambiguous": False,
            "predicted": "task",  # a wrong auto-write
            "confidence": 0.95,
            "fields": {},
            "case": {"text": "remember x", "destination": "note"},
        }
    ]
    card = score(results)
    assert card["task_false_positives"] == 1
    assert card["passed"] is False


def test_eval_score_doc_path_gate_fails_below_threshold():
    """Goal 9: mostly-wrong doc-path routing on the clear hierarchy subset fails the
    gate even when destination accuracy is perfect."""
    from app.router.evals.runner import score

    results = [
        {
            "text": f"john growth note {i}",
            "expected": "note",
            "ambiguous": False,
            "predicted": "note",
            "confidence": 0.95,
            # Every one mis-routes to the wrong Doc.
            "fields": {"target_doc_path": "ideas"},
            "case": {
                "text": f"john growth note {i}",
                "destination": "note",
                "doc_path_expect": "conversations/john/growth",
            },
        }
        for i in range(5)
    ]
    card = score(results)
    assert card["doc_path_accuracy"] == 0.0
    assert card["passed"] is False


# ── Notes writer: heading + insert-at-top (goal 7, no API) ─────────────────────


def test_format_note_heading_locked_format():
    from datetime import datetime

    from app.writes.service import format_note_heading

    assert (
        format_note_heading(datetime(2026, 7, 6, 20, 41)) == "6-July-2026, 8:41 PM IST"
    )
    assert (
        format_note_heading(datetime(2026, 1, 9, 0, 5))
        == "9-January-2026, 12:05 AM IST"
    )
    assert (
        format_note_heading(datetime(2026, 12, 25, 12, 0))
        == "25-December-2026, 12:00 PM IST"
    )


def test_insert_note_puts_block_at_top_placeholder_h3(monkeypatch):
    """The batchUpdate inserts at index 1 (top of body) — newest note lands above
    everything else. With no one-liner, the H3 headline is a placeholder and the
    timestamp stays H4 (stable levels for search). `insert_note` takes `creds` first."""
    from app.google import docs as docs_mod

    captured = {}

    class FakeDocs:
        def documents(self):
            return self

        def batchUpdate(self, documentId, body):
            captured["documentId"] = documentId
            captured["requests"] = body["requests"]
            return self

        def execute(self):
            return {}

    monkeypatch.setattr(docs_mod, "_docs_service", lambda _creds: FakeDocs())
    run(
        docs_mod.insert_note(
            DummyCreds(), "DOC", "6-July-2026, 8:41 PM IST", "a plain body line"
        )
    )

    reqs = captured["requests"]
    assert captured["documentId"] == "DOC"
    assert reqs[0]["insertText"]["location"]["index"] == 1
    # placeholder H3 headline, then the timestamp as H4 (never promoted to H3).
    assert reqs[0]["insertText"]["text"].startswith(
        f"{docs_mod._NO_SUMMARY_PLACEHOLDER}\n6-July-2026, 8:41 PM IST\n"
    )
    # verbatim body, then a trailing empty paragraph (the delimiter, goal 7a)
    assert reqs[0]["insertText"]["text"].endswith("a plain body line\n\n")
    assert _para_styles(reqs)[:3] == ["HEADING_3", "HEADING_4", "NORMAL_TEXT"]
    assert reqs[1]["updateParagraphStyle"]["range"]["startIndex"] == 1
    # The last request styles the empty delimiter paragraph with a light-gray
    # borderBottom + spacing — insert-only, no HR request exists.
    delim = reqs[-1]["updateParagraphStyle"]
    assert "borderBottom" in delim["paragraphStyle"]
    assert "borderBottom" in delim["fields"]


def _capture_batchupdate(monkeypatch):
    """Patch the Docs service to capture the batchUpdate requests; returns the box."""
    from app.google import docs as docs_mod

    captured: dict = {}

    class FakeDocs:
        def documents(self):
            return self

        def batchUpdate(self, documentId, body):
            captured["documentId"] = documentId
            captured["requests"] = body["requests"]
            return self

        def execute(self):
            return {}

    monkeypatch.setattr(docs_mod, "_docs_service", lambda _creds: FakeDocs())
    return captured


def _para_styles(reqs):
    """Ordered (namedStyleType) of every updateParagraphStyle request."""
    return [
        r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
        for r in reqs
        if "updateParagraphStyle" in r
    ]


def test_insert_note_h3_h4_h5_full_shape(monkeypatch):
    """Goal 9 entry shape: H3 one-liner → H4 timestamp → H5 keywords → verbatim
    body → delimiter, in order. Named heading styles replace the goal-7c bold line;
    no updateTextStyle bolding anymore."""
    from app.google import docs as docs_mod

    captured = _capture_batchupdate(monkeypatch)
    run(
        docs_mod.insert_note(
            DummyCreds(),
            "DOC",
            "6-July-2026, 8:41 PM IST",
            "a plain body",
            "milk + eggs",
            ["milk", "eggs"],
        )
    )
    reqs = captured["requests"]
    text = reqs[0]["insertText"]["text"]
    # one-liner headline, timestamp, keyword line, verbatim body, then delimiter.
    assert text.startswith("milk + eggs\n6-July-2026, 8:41 PM IST\nmilk, eggs\n")
    assert text.endswith("a plain body\n\n")
    # Named styles: H3 (one-liner) → H4 (timestamp) → H5 → body → delimiter (NORMAL).
    styles = _para_styles(reqs)
    assert styles[:4] == ["HEADING_3", "HEADING_4", "HEADING_5", "NORMAL_TEXT"]
    # No bold text-style requests for a plain body (goal-9 shape unchanged).
    assert not any("updateTextStyle" in r for r in reqs)
    assert "borderBottom" in reqs[-1]["updateParagraphStyle"]["paragraphStyle"]


def test_insert_note_summary_no_keywords_skips_h5(monkeypatch):
    """A one-liner but no keywords → H3 one-liner → H4 timestamp → body → delimiter."""
    from app.google import docs as docs_mod

    captured = _capture_batchupdate(monkeypatch)
    run(
        docs_mod.insert_note(
            DummyCreds(), "DOC", "6-July-2026, 8:41 PM IST", "a line", "one liner", []
        )
    )
    reqs = captured["requests"]
    assert (
        reqs[0]["insertText"]["text"]
        == "one liner\n6-July-2026, 8:41 PM IST\na line\n\n"
    )
    assert _para_styles(reqs)[:3] == ["HEADING_3", "HEADING_4", "NORMAL_TEXT"]


def test_insert_note_blank_summary_uses_placeholder_h3_stable_timestamp(monkeypatch):
    """A missing/blank summary → a placeholder H3 headline (never blocking the write),
    with the timestamp staying H4. Levels are stable so a later "all H4s" search yields
    every timestamp regardless of whether the one-liner was present."""
    from app.google import docs as docs_mod

    captured = _capture_batchupdate(monkeypatch)
    run(
        docs_mod.insert_note(
            DummyCreds(), "DOC", "6-July-2026, 8:41 PM IST", "a line", "   "
        )
    )
    reqs = captured["requests"]
    assert reqs[0]["insertText"]["text"] == (
        f"{docs_mod._NO_SUMMARY_PLACEHOLDER}\n6-July-2026, 8:41 PM IST\na line\n\n"
    )
    assert _para_styles(reqs)[:3] == ["HEADING_3", "HEADING_4", "NORMAL_TEXT"]
    assert not any("updateTextStyle" in r for r in reqs)


# ── Goal 10: the structured-body markdown renderer ────────────────────────────


def _bullet_reqs(reqs):
    return [r for r in reqs if "createParagraphBullets" in r]


def _text_style_reqs(reqs):
    return [r for r in reqs if "updateTextStyle" in r]


def test_insert_note_body_markdown_headings_never_heading_style(monkeypatch):
    """THE goal-10 invariant: a markdown heading in the body renders as a **bold
    NORMAL_TEXT** line — never a `HEADING_*` paragraph — so the H3/H4/H5 chrome stays
    the ONLY heading structure. The heading words survive verbatim minus the marker."""
    captured = _capture_batchupdate(monkeypatch)
    run(
        docs_mod.insert_note(
            DummyCreds(),
            "DOC",
            "6-July-2026, 8:41 PM IST",
            "## Agenda\n### Action items\nregular line",
            "mom",
        )
    )
    reqs = captured["requests"]
    text = reqs[0]["insertText"]["text"]
    # Marker stripped, words kept verbatim; the '#'s are gone.
    assert "Agenda\nAction items\nregular line\n" in text
    assert "#" not in text
    # The ONLY HEADING_* styles are the three chrome lines (H3 one-liner, H4 timestamp).
    styles = _para_styles(reqs)
    assert [s for s in styles if s.startswith("HEADING_")] == ["HEADING_3", "HEADING_4"]
    # The heading lines are bold NORMAL_TEXT (bold text-style requests exist for them).
    assert _text_style_reqs(reqs)


def test_insert_note_body_bullets_become_nested_bullets(monkeypatch):
    """`- `/`  - ` lines become real Docs bullets (`createParagraphBullets`) with the
    nesting carried by leading tabs; no HEADING_* comes from the body."""
    captured = _capture_batchupdate(monkeypatch)
    run(
        docs_mod.insert_note(
            DummyCreds(),
            "DOC",
            "6-July-2026, 8:41 PM IST",
            "- top\n  - nested\n- back",
        )
    )
    reqs = captured["requests"]
    text = reqs[0]["insertText"]["text"]
    # The nested bullet carries one leading tab (its level); markers stripped.
    assert "top\n\tnested\nback\n" in text
    bullets = _bullet_reqs(reqs)
    assert len(bullets) == 1  # one contiguous unordered run
    assert bullets[0]["createParagraphBullets"]["bulletPreset"] == (
        "BULLET_DISC_CIRCLE_SQUARE"
    )
    # Body contributes no heading style.
    assert [s for s in _para_styles(reqs) if s.startswith("HEADING_")] == [
        "HEADING_3",
        "HEADING_4",
    ]


def test_insert_note_numbered_list_uses_ordered_preset(monkeypatch):
    """`1. ` lines become an ORDERED bullet run (a different preset from unordered)."""
    captured = _capture_batchupdate(monkeypatch)
    run(
        docs_mod.insert_note(
            DummyCreds(), "DOC", "6-July-2026, 8:41 PM IST", "1. first\n2. second"
        )
    )
    bullets = _bullet_reqs(captured["requests"])
    assert len(bullets) == 1
    assert bullets[0]["createParagraphBullets"]["bulletPreset"] == (
        "NUMBERED_DECIMAL_ALPHA_ROMAN"
    )


def test_insert_note_inline_bold_becomes_bold_run(monkeypatch):
    """Inline `**bold**` → a bold text run, the `**` markers consumed from the text."""
    captured = _capture_batchupdate(monkeypatch)
    run(
        docs_mod.insert_note(
            DummyCreds(), "DOC", "6-July-2026, 8:41 PM IST", "some **strong** word"
        )
    )
    reqs = captured["requests"]
    assert "some strong word\n" in reqs[0]["insertText"]["text"]
    assert "**" not in reqs[0]["insertText"]["text"]
    bolds = _text_style_reqs(reqs)
    assert len(bolds) == 1
    rng = bolds[0]["updateTextStyle"]["range"]
    # The bold run spans exactly "strong" within the inserted text.
    body = reqs[0]["insertText"]["text"]
    assert body[rng["startIndex"] - 1 : rng["endIndex"] - 1] == "strong"


def test_insert_note_plain_body_byte_identical_to_pre_goal10(monkeypatch):
    """A plain one-liner note produces the exact pre-goal-10 request shape: one
    insertText, three chrome/body paragraph styles, one delimiter — no bullets, no
    bold, no extra requests."""
    captured = _capture_batchupdate(monkeypatch)
    run(
        docs_mod.insert_note(
            DummyCreds(), "DOC", "6-July-2026, 8:41 PM IST", "just a plain note"
        )
    )
    reqs = captured["requests"]
    # insertText + H3 + H4 + body NORMAL_TEXT + delimiter = exactly 5 requests.
    assert len(reqs) == 5
    assert "insertText" in reqs[0]
    assert _para_styles(reqs) == [
        "HEADING_3",
        "HEADING_4",
        "NORMAL_TEXT",
        "NORMAL_TEXT",
    ]
    assert not _bullet_reqs(reqs) and not _text_style_reqs(reqs)
    assert reqs[0]["insertText"]["text"] == (
        f"{docs_mod._NO_SUMMARY_PLACEHOLDER}\n6-July-2026, 8:41 PM IST\n"
        "just a plain note\n\n"
    )


def test_auto_route_note_passes_summary_through(
    session, user_a, google, fake_classify, notes
):
    """A high-confidence note carries the classifier's summary into the Doc write."""
    _set(fake_classify, "note", 0.95, note_text="x", summary="entropy video")
    state = run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "raw verbatim text")
        )
    )
    assert state == KEPT_NOTE
    doc_id, heading, body, summary, _kw = notes["insert"][0]
    assert body == "raw verbatim text"  # note_text "x" too short → raw wins (guard)
    assert summary == "entropy video"  # the LLM one-liner rides alongside


def test_confirm_as_note_review_writes_to_doc(
    session, user_a, google, fake_classify, notes
):
    """Confirm-as-note in review fires exactly one Docs append (the panel copy now
    promises this)."""
    _set(fake_classify, "unknown", 0.1)  # lands in review
    run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "- a stray thought")
        )
    )
    item = session.exec(select(ReviewItem)).first()
    res = run(
        router_svc.confirm_review(
            session, user_a, DummyCreds(), item.id, destination="note"
        )
    )
    assert res["entry_state"] == KEPT_NOTE
    assert len(notes["insert"]) == 1
    assert notes["insert"][0][2] == "- a stray thought"  # verbatim entry text


def test_confirm_as_note_uses_edited_body_and_one_liner(
    session, user_a, google, fake_classify, notes
):
    """Goal 7c: review edits win — a user-edited note body + one-liner are what
    land in the Doc, not the raw captured text."""
    _set(fake_classify, "unknown", 0.1)  # lands in review
    run(
        router_svc.route_entry(
            session, user_a, DummyCreds(), _entry(session, user_a, "raw capture text")
        )
    )
    item = session.exec(select(ReviewItem)).first()
    res = run(
        router_svc.confirm_review(
            session,
            user_a,
            DummyCreds(),
            item.id,
            destination="note",
            fields=RouterFields(note_text="cleaned body", summary="a headline"),
        )
    )
    assert res["entry_state"] == KEPT_NOTE
    _doc, _heading, body, summary, _kw = notes["insert"][0]
    assert body == "cleaned body"  # the edit, not the raw capture
    assert summary == "a headline"


def test_review_note_endpoint_overrides(client, google, fake_classify, notes):
    """The /confirm endpoint threads note_text + summary overrides through to the
    Doc write (task | note only in the UI; the endpoint honors both)."""
    _set(fake_classify, "event", 0.95, title="lunch")  # → review inline
    client.post("/scratch", json={"text": "some thought"})
    item = client.get("/review").json()["items"][0]
    r = client.post(
        f"/review/{item['id']}/confirm",
        json={
            "destination": "note",
            "fields": {"note_text": "edited note", "summary": "one-liner"},
        },
    )
    assert r.status_code == 200 and r.json()["entry_state"] == KEPT_NOTE
    assert notes["insert"][0][2] == "edited note"
    assert notes["insert"][0][3] == "one-liner"


# ── Goal 10a: the long-capture classifier blowout ─────────────────────────────
#
# Observed: every capture over ~8k chars came back `unknown`/0.0 while every short
# one classified fine. Cause: `note_text` is a VERBATIM echo, so the response scales
# with the input — a 10k-char meeting-notes paste needs ~2.5k output tokens and
# truncates mid-JSON, collapsing the whole classification. The user saw it as three
# separate bugs (wrong Doc, no one-liner, no keywords); all three are this.


def test_long_capture_is_excerpted_for_classification():
    """Long captures are shown to the model head-only. Measured: given the whole thing
    the model burns its output budget retyping the body and returns summary/doc_path/
    keywords as null — bounding the input is what buys those fields back."""
    from app.router import config
    from app.router.classifier import _excerpt

    short = "note t4d test\n\n- a short capture"
    long_text = "note t4d test\n\n" + ("- a line of meeting notes\n" * 200)

    assert _excerpt(short) == (short, False)  # goal-9 path untouched
    shown, truncated = _excerpt(long_text)
    assert truncated
    assert len(shown) < len(long_text)
    assert shown.startswith("note t4d test")  # the routing header always survives
    assert len(shown) <= config.CLASSIFY_MAX_CHARS + 100  # + the truncation marker


def test_echo_of_a_truncated_capture_is_discarded(monkeypatch):
    """The model echoes even when told not to (measured), and an echo of an excerpt is
    a body missing its tail. Code — not the prompt — is what refuses it."""
    from app.router import classifier

    long_text = "note t4d test\n\n" + ("- a line of meeting notes\n" * 200)
    seen = {}

    class _Resp:
        def __init__(self, parsed):
            self.parsed_output = parsed

    async def _parse(**kw):
        seen["system"] = kw["system"]
        seen["user"] = kw["messages"][0]["content"]
        # A disobedient model: echoes an ABRIDGED body anyway.
        return _Resp(
            RouterClassification(
                destination="note",
                confidence=0.95,
                fields=RouterFields(note_text="- a line of meeting notes\n" * 20),
            )
        )

    class _Client:
        messages = type("M", (), {"parse": staticmethod(_parse)})()

    monkeypatch.setattr(classifier.anthropic, "AsyncAnthropic", lambda: _Client())

    result = run(classifier.classify(long_text))
    assert result.fields.note_text is None, "an abridged echo must never reach the Doc"
    assert result.destination == "note" and result.confidence == 0.95
    assert "TRUNCATED CAPTURE" in seen["system"]
    assert len(seen["user"]) < len(long_text)


def test_long_note_body_is_verbatim_minus_header_when_llm_omits_echo(
    session, user_a, google, fake_classify, notes
):
    """The long path end-to-end: the LLM returns note_text=None (as instructed) and
    code supplies the body — the user's words verbatim, minus only the routing header,
    with the one-liner + keywords the truncated response used to lose."""
    body = "\n".join(f"- meeting point {i}" for i in range(400))
    raw = f"note t4d test\n\n{body}"
    _set(
        fake_classify,
        "note",
        0.95,
        note_text=None,  # suppressed by _echo_section
        summary="T4D sync",
        keywords=["t4d", "sync"],
    )
    entry = _entry(session, user_a, raw)
    run(router_svc.route_entry(session, user_a, DummyCreds(), entry))

    written_body, summary, keywords = notes["insert"][0][2:5]
    assert written_body == body  # verbatim, header stripped, nothing lost
    assert "note t4d test" not in written_body
    assert summary == "T4D sync"  # the H3 one-liner survives
    assert keywords == ["t4d", "sync"]  # the H5 keywords survive


def test_long_note_falls_back_to_raw_when_there_is_no_header(
    session, user_a, google, fake_classify, notes
):
    """No header to strip → the fallback is the whole raw capture, verbatim. The
    guard must never drop words just because the echo was suppressed."""
    raw = "\n".join(f"line {i}" for i in range(400))
    _set(fake_classify, "note", 0.95, note_text=None)
    entry = _entry(session, user_a, raw)
    run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    assert notes["insert"][0][2] == raw


# ── Goal 10a: route-once is atomic (the double-append) ────────────────────────


def test_concurrent_routers_append_a_note_exactly_once(
    session, engine, user_a, google, fake_classify, notes
):
    """Observed: one capture, two identical Doc entries. Route-once was a
    check-then-act and inline routing holds the entry UNROUTED for ~25s (classifier +
    Docs append), so the scheduler backstop / a "Route now" click / a retried POST
    could read the same row and file it again. Two routers, one entry, ONE append."""
    from sqlmodel import Session as SQLSession

    _set(fake_classify, "note", 0.95, note_text="the meeting notes")
    entry = _entry(session, user_a, "note t4d test\n\nthe meeting notes")
    entry_id = entry.id

    async def router():
        # Its own session — the inline-POST vs backstop collision is cross-session.
        with SQLSession(engine) as s:
            e = s.get(ScratchEntry, entry_id)
            return await router_svc.route_entry(s, user_a, DummyCreds(), e)

    async def both():
        return await asyncio.gather(router(), router())

    states = run(both())
    assert len(notes["insert"]) == 1, "the note was appended to the Doc twice"
    assert KEPT_NOTE in states  # exactly one winner; the loser saw the claim


def test_failed_route_releases_the_claim_so_the_backstop_can_retry(
    session, user_a, google, fake_classify, notes, monkeypatch
):
    """A claim must never become a grave: a Docs failure hands the entry back to
    UNROUTED so the backstop still retries it (the goal-5 re-routable contract)."""

    async def _boom(*a, **k):
        raise ApiError(502, "docs_down", "boom")

    monkeypatch.setattr("app.google.docs.insert_note", _boom)
    _set(fake_classify, "note", 0.95, note_text="x")
    entry = _entry(session, user_a, "notes - x")

    with pytest.raises(ApiError):
        run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    session.refresh(entry)
    assert entry.routing_state == UNROUTED


def test_backstop_reclaims_a_dead_claim(session, user_a, google, fake_classify, notes):
    """A router that died mid-route leaves a stale ROUTING row. The backstop exists
    for exactly that crash-recovery case, so it must reclaim it rather than skip it
    forever."""
    from datetime import timedelta

    _set(fake_classify, "note", 0.95, note_text="x")
    entry = _entry(session, user_a, "notes - x")
    entry.routing_state = ROUTING
    entry.routed_at = router_svc._now() - timedelta(
        seconds=router_svc._STALE_CLAIM_SECONDS + 60
    )
    session.add(entry)
    session.commit()

    run(router_svc.route_unrouted(session, user_a, DummyCreds()))
    session.refresh(entry)
    assert entry.routing_state == KEPT_NOTE


def test_backstop_leaves_a_live_claim_alone(
    session, user_a, google, fake_classify, notes
):
    """The flip side: a fresh claim is someone else's in-flight route. Stealing it is
    the double-append bug, so the backstop must not touch it."""
    _set(fake_classify, "note", 0.95, note_text="x")
    entry = _entry(session, user_a, "notes - x")
    entry.routing_state = ROUTING
    entry.routed_at = router_svc._now()
    session.add(entry)
    session.commit()

    run(router_svc.route_unrouted(session, user_a, DummyCreds()))
    session.refresh(entry)
    assert entry.routing_state == ROUTING
    assert notes["insert"] == []


def test_content_first_line_is_never_mistaken_for_a_header(
    session, user_a, google, fake_classify, notes
):
    """The body fallback may only strip a leading segment that is PROVABLY routing
    words (a task/note keyword or a date word). A short first line of real content is
    not a header — stripping it would silently eat the user's first line, which is the
    one thing the verbatim guard exists to prevent."""
    raw = "the offsite venue\nwas the lakeside resort\nand everyone liked it"
    _set(fake_classify, "note", 0.95, note_text=None)
    entry = _entry(session, user_a, raw)
    run(router_svc.route_entry(session, user_a, DummyCreds(), entry))
    assert notes["insert"][0][2] == raw  # nothing stripped, nothing lost

    hdr = router_svc._parse_header(raw)
    assert not hdr.determinate
    assert router_svc._parse_header("note t4d test\n\nbody").determinate
    assert router_svc._parse_header("task tomorrow - pay the plumber").determinate
