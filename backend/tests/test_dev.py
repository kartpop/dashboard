"""Goal 12 — the Dev view: notes → synthesised issue drafts → one-click filing.

Covers the acceptance bar: source resolution (folders recurse at scan time), the
cursor + parser (process-once, same-minute boundary, advance-after-persist), batching &
synthesis (one LLM call across docs, merged provenance), the drafter dispose contract
(exact prompt field set, out-of-catalog fallback, no-action → no draft), filing
(create + project attach, partial-state retry, dismiss makes no call, per-owner token
routing), secrets (tokens write-only, one per resource owner), and gating (endpoints
403, cron scoped to flagged users).

Every Google/GitHub/LLM boundary is monkeypatched — no network, no real creds.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.auth.models import AllowedEmail
from app.dev import github as gh
from app.dev import service as dev_svc
from app.dev.models import (
    DISMISSED,
    DRAFT,
    FILED,
    KIND_COMMENT,
    KIND_ISSUE,
    SAVED,
    DevDocCursor,
    DevIssueDraft,
)
from app.dev.schema import (
    CommentDraftResult,
    DraftMatches,
    MatchResult,
    ProposedIssue,
    ProposedMatch,
    SourceRef,
    SynthesisResult,
)
from app.settings import notes_index
from app.settings import service as settings_svc
from tests.conftest import DummyCreds


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _github_offline(monkeypatch):
    """Goal 12b gave `run_scan` a live-GitHub match tail, so no test may leave GitHub
    HTTP reachable: any un-stubbed call raises `GithubError` at client construction —
    the scan's best-effort match phase then reports itself skipped instead of touching
    the network. Tests that exercise the HTTP layer monkeypatch `gh.httpx.AsyncClient`
    over this; tests that exercise matching stub the `gh` functions themselves."""

    def _no_network(**kwargs):
        raise gh.GithubError(0, "network disabled in tests")

    monkeypatch.setattr(gh.httpx, "AsyncClient", _no_network)


# ── Fixture builders ──────────────────────────────────────────────────────────


def _para(style: str, text: str) -> dict:
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [{"textRun": {"content": text + "\n"}}],
        }
    }


def _doc(*entries: tuple) -> dict:
    """Build a documents.get payload, newest-first. Each entry is
    (one_liner, ts_line, keywords|None, body)."""
    content = []
    for one_liner, ts_line, keywords, body in entries:
        content.append(_para("HEADING_3", one_liner))
        content.append(_para("HEADING_4", ts_line))
        if keywords:
            content.append(_para("HEADING_5", keywords))
        content.append(_para("NORMAL_TEXT", body))
        content.append(_para("NORMAL_TEXT", ""))  # delimiter
    return {"body": {"content": content}}


def _enable_dev(session, user) -> None:
    session.add(AllowedEmail(email=user.email, features=json.dumps({"dev": True})))
    session.commit()


def _seed_config(session, user, *, sources, repos, docs_forest) -> None:
    """Store a notes forest + dev config (PAT + sources + repos) so the scan is complete."""
    s = settings_svc.get_or_create(session, user.id)
    s.notes_index = notes_index.serialize(docs_forest)
    session.add(s)
    session.commit()
    # One token per resource owner the configured repos live under (derived like the
    # add-token endpoint does from the repos a PAT can see).
    owners = sorted({r["full_name"].split("/", 1)[0] for r in repos})
    dev_svc.add_token(session, user.id, "github_pat_TESTTOKEN1234", owners, "octocat")
    dev_svc.set_sources(session, user.id, sources)
    dev_svc.set_repos(session, user.id, repos)


_FOREST = [
    {
        "node_id": "f1",
        "name": "internal",
        "kind": "folder",
        "drive_id": "FOLDER",
        "children": [
            {
                "node_id": "d1",
                "name": "kaapi",
                "kind": "doc",
                "drive_id": "DOC1",
                "children": [],
            },
            {
                "node_id": "d2",
                "name": "standup",
                "kind": "doc",
                "drive_id": "DOC2",
                "children": [],
            },
        ],
    }
]

_REPOS = [
    {
        "full_name": "org/kaapi-backend",
        "description": "the backend",
        "is_default": True,
    },
    {"full_name": "org/kaapi-web", "description": "the frontend", "is_default": False},
]


def _patch_docs(monkeypatch, docs: dict) -> None:
    async def fake_get_document(creds, doc_id):
        return docs[doc_id]

    monkeypatch.setattr(dev_svc.docs_client, "get_document", fake_get_document)


def _patch_synth(monkeypatch, result: SynthesisResult, calls: list) -> None:
    async def fake_synth(entries, catalog, dnr):
        calls.append({"entries": entries, "catalog": catalog, "dnr": dnr})
        return result

    monkeypatch.setattr(dev_svc.synth, "synthesise", fake_synth)


# ── Source resolution ─────────────────────────────────────────────────────────


def test_selected_folder_resolves_all_docs_recursively():
    """A selected folder covers every Doc beneath it; a Doc added later is included
    without touching the selection."""
    leaves = notes_index.selected_doc_leaves(_FOREST, {"f1"})
    paths = {leaf["path"]: leaf["drive_id"] for leaf in leaves}
    assert paths == {"internal/kaapi": "DOC1", "internal/standup": "DOC2"}

    forest2 = json.loads(json.dumps(_FOREST))
    forest2[0]["children"].append(
        {
            "node_id": "d3",
            "name": "new",
            "kind": "doc",
            "drive_id": "DOC3",
            "children": [],
        }
    )
    leaves2 = notes_index.selected_doc_leaves(forest2, {"f1"})
    assert {leaf["drive_id"] for leaf in leaves2} == {"DOC1", "DOC2", "DOC3"}


def test_selected_doc_and_ancestor_folder_dedupe():
    """Selecting both a folder and a Doc under it yields that Doc once."""
    leaves = notes_index.selected_doc_leaves(_FOREST, {"f1", "d1"})
    assert len(leaves) == 2


# ── Cursor & parser ───────────────────────────────────────────────────────────


def test_scan_twice_sends_zero_on_second_pass(monkeypatch, session, user_a):
    _seed_config(session, user_a, sources=["f1"], repos=_REPOS, docs_forest=_FOREST)
    docs = {
        "DOC1": _doc(
            ("Fix login", "6-July-2026, 8:41 PM IST", "auth", "cannot log in")
        ),
        "DOC2": _doc(("Dark mode", "6-July-2026, 3:00 PM IST", None, "requested")),
    }
    _patch_docs(monkeypatch, docs)
    calls: list = []
    _patch_synth(monkeypatch, SynthesisResult(issues=[]), calls)

    t1 = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert t1["new_entries"] == 2
    assert len(calls) == 1  # one batched call

    t2 = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert t2["new_entries"] == 0
    assert len(calls) == 1  # no second LLM call — nothing new to synthesise


def test_synth_failure_leaves_cursor_unadvanced_for_retry(monkeypatch, session, user_a):
    """A failed synthesis (returns None — e.g. truncated at max_tokens) must NOT advance
    the cursor: the same entries re-scan next run rather than being silently consumed. A
    real result (even empty) is a success and would advance; only None holds the cursor."""
    _seed_config(session, user_a, sources=["f1"], repos=_REPOS, docs_forest=_FOREST)
    docs = {
        "DOC1": _doc(
            ("Fix login", "6-July-2026, 8:41 PM IST", "auth", "cannot log in")
        ),
        "DOC2": _doc(("Dark mode", "6-July-2026, 3:00 PM IST", None, "requested")),
    }
    _patch_docs(monkeypatch, docs)

    calls: list = []

    async def failing_synth(entries, catalog, dnr):
        calls.append(entries)
        return None  # synthesis failed (truncation / model error)

    monkeypatch.setattr(dev_svc.synth, "synthesise", failing_synth)

    t1 = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert t1["new_entries"] == 2
    assert len(calls) == 1
    # The tally distinguishes a failure from "the model found nothing worth drafting":
    # both leave drafts_created at 0, but only one means the batch is still queued.
    assert t1["drafts_created"] == 0 and t1["synthesis_failed"] is True

    # Cursor was NOT advanced — the same two entries are presented to the LLM again.
    t2 = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert t2["new_entries"] == 2
    assert len(calls) == 2


def test_same_minute_entry_captured_after_scan_processed_once(
    monkeypatch, session, user_a
):
    """Two entries share one minute-granular timestamp, split across two scans: the
    later-captured one is processed exactly once (not skipped, not doubled)."""
    _seed_config(session, user_a, sources=["d1"], repos=_REPOS, docs_forest=_FOREST)
    calls: list = []
    _patch_synth(monkeypatch, SynthesisResult(issues=[]), calls)

    # Scan 1: only entry A exists at 8:41.
    docs = {"DOC1": _doc(("Entry A", "6-July-2026, 8:41 PM IST", None, "a"))}
    _patch_docs(monkeypatch, docs)
    run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert calls[0]["entries"][0]["one_liner"] == "Entry A"

    # Scan 2: entry B captured at the SAME minute, prepended above A.
    docs["DOC1"] = _doc(
        ("Entry B", "6-July-2026, 8:41 PM IST", None, "b"),
        ("Entry A", "6-July-2026, 8:41 PM IST", None, "a"),
    )
    run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert len(calls) == 2
    second = [e["one_liner"] for e in calls[1]["entries"]]
    assert second == ["Entry B"]  # A not re-sent, B sent once


def test_cursor_advances_only_after_drafts_persist(monkeypatch, session, user_a):
    """A failure at the store step leaves the cursor unadvanced, so a rescan reprocesses
    (no entry lost)."""
    _seed_config(session, user_a, sources=["d1"], repos=_REPOS, docs_forest=_FOREST)
    _patch_docs(
        monkeypatch,
        {"DOC1": _doc(("E", "6-July-2026, 8:41 PM IST", None, "body"))},
    )

    async def ok_synth(entries, catalog, dnr):
        return SynthesisResult(issues=[])

    monkeypatch.setattr(dev_svc.synth, "synthesise", ok_synth)

    def boom(*a, **k):
        raise RuntimeError("store failed")

    monkeypatch.setattr(dev_svc, "_dispose_synthesis", boom)
    with pytest.raises(RuntimeError):
        run(dev_svc.run_scan(session, user_a, DummyCreds()))

    cursor = session.exec(
        select(DevDocCursor).where(DevDocCursor.doc_id == "DOC1")
    ).first()
    assert cursor is None  # never advanced


# ── Batching & synthesis ──────────────────────────────────────────────────────


def test_batch_gathers_multiple_docs_into_one_call(monkeypatch, session, user_a):
    _seed_config(session, user_a, sources=["f1"], repos=_REPOS, docs_forest=_FOREST)
    _patch_docs(
        monkeypatch,
        {
            "DOC1": _doc(("From kaapi", "6-July-2026, 8:41 PM IST", None, "x")),
            "DOC2": _doc(("From standup", "6-July-2026, 10:04 AM IST", None, "y")),
        },
    )
    calls: list = []
    _patch_synth(monkeypatch, SynthesisResult(issues=[]), calls)

    run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert len(calls) == 1
    paths = {e["doc_path"] for e in calls[0]["entries"]}
    assert paths == {"internal/kaapi", "internal/standup"}


def test_merged_sources_yields_one_draft_with_both(monkeypatch, session, user_a):
    """The same action item in two entries (two docs) → ONE draft citing BOTH sources."""
    _seed_config(session, user_a, sources=["f1"], repos=_REPOS, docs_forest=_FOREST)
    _patch_docs(
        monkeypatch,
        {
            "DOC1": _doc(
                ("login broke", "6-July-2026, 8:41 PM IST", None, "auth down")
            ),
            "DOC2": _doc(
                ("auth failing", "6-July-2026, 10:04 AM IST", None, "cannot sign in")
            ),
        },
    )
    merged = SynthesisResult(
        issues=[
            ProposedIssue(
                title="Fix broken authentication",
                body_markdown="Users cannot sign in.",
                repo="org/kaapi-backend",
                sources=[
                    SourceRef(
                        doc_path="internal/kaapi", entry_ts="2026-07-06T20:41:00"
                    ),
                    SourceRef(
                        doc_path="internal/standup", entry_ts="2026-07-06T10:04:00"
                    ),
                ],
            )
        ]
    )
    _patch_synth(monkeypatch, merged, [])
    tally = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert tally["drafts_created"] == 1

    drafts, _ = dev_svc.list_drafts(session, user_a.id)
    assert len(drafts) == 1
    sources = json.loads(drafts[0].sources)
    assert len(sources) == 2
    assert {s["doc_path"] for s in sources} == {"internal/kaapi", "internal/standup"}
    assert {s["doc_id"] for s in sources} == {"DOC1", "DOC2"}  # provenance resolved


# ── Drafter dispose / prompt-builder contract ─────────────────────────────────


def test_prompt_field_set_excludes_ids_and_tokens():
    """The serialized entry payload is EXACTLY the contract field set — no doc_id, no
    drive id, no token — even though the service threads doc_id alongside."""
    from app.dev import synth

    entries = [
        {
            "doc_id": "SECRET_DRIVE_ID",
            "doc_path": "internal/kaapi",
            "entry_ts": "2026-07-06T20:41:00",
            "one_liner": "fix it",
            "keywords": "auth",
            "body": "the body",
        }
    ]
    payload = synth.build_entry_payload(entries)
    assert set(payload[0].keys()) == set(synth.ENTRY_FIELDS)
    assert "doc_id" not in payload[0]

    _system, user = synth.build_prompt(
        payload,
        [{"full_name": "org/kaapi-backend", "description": "backend"}],
        ["An existing open draft title"],
    )
    assert "SECRET_DRIVE_ID" not in user
    assert "github_pat" not in user
    assert "internal/kaapi" in user
    assert "An existing open draft title" in user  # do-not-redraft context present


def test_out_of_catalog_repo_falls_back_to_default(monkeypatch, session, user_a):
    _seed_config(session, user_a, sources=["d1"], repos=_REPOS, docs_forest=_FOREST)
    _patch_docs(
        monkeypatch, {"DOC1": _doc(("x", "6-July-2026, 8:41 PM IST", None, "b"))}
    )
    result = SynthesisResult(
        issues=[
            ProposedIssue(
                title="Something",
                body_markdown="body",
                repo="org/not-in-catalog",
                sources=[],
            )
        ]
    )
    _patch_synth(monkeypatch, result, [])
    run(dev_svc.run_scan(session, user_a, DummyCreds()))
    draft = dev_svc.list_drafts(session, user_a.id)[0][0]
    assert draft.repo == "org/kaapi-backend"  # the configured default


def test_no_action_items_yields_no_draft(monkeypatch, session, user_a):
    _seed_config(session, user_a, sources=["d1"], repos=_REPOS, docs_forest=_FOREST)
    _patch_docs(
        monkeypatch,
        {"DOC1": _doc(("chat", "6-July-2026, 8:41 PM IST", None, "social"))},
    )
    _patch_synth(monkeypatch, SynthesisResult(issues=[]), [])
    tally = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert tally["drafts_created"] == 0
    # An entry WAS read and considered — the model just drew nothing from it. The tally
    # says so, so this can't be mistaken for a source Doc that never got read.
    assert tally["docs_read"] == 1
    assert tally["new_entries"] == 1
    assert tally["synthesis_failed"] is False
    assert dev_svc.list_drafts(session, user_a.id) == ([], None)


# ── Filing ────────────────────────────────────────────────────────────────────


def _make_draft(session, user, **over) -> DevIssueDraft:
    d = DevIssueDraft(
        user_id=user.id,
        title=over.pop("title", "Fix bug"),
        body=over.pop("body", "body"),
        repo=over.pop("repo", "org/kaapi-backend"),
        status=over.pop("status", DRAFT),
        sources="[]",
        project_node_id=over.pop("project_node_id", "PROJ_NODE"),
        project_title=over.pop("project_title", "Backlog"),
        **over,
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def test_file_creates_issue_and_attaches_project(monkeypatch, session, user_a):
    dev_svc.add_token(session, user_a.id, "github_pat_X", ["org"], "octocat")
    draft = _make_draft(session, user_a)
    calls: list = []

    async def fake_create(pat, owner, repo, title, body):
        calls.append("create")
        return {
            "number": 123,
            "url": "https://github.com/org/kaapi-backend/issues/123",
            "node_id": "ISSUE_NODE",
        }

    async def fake_attach(pat, project_node_id, issue_node_id):
        calls.append("attach")
        return "ITEM_ID"

    monkeypatch.setattr(dev_svc.github, "create_issue", fake_create)
    monkeypatch.setattr(dev_svc.github, "add_issue_to_project", fake_attach)

    out = run(dev_svc.file_draft(session, user_a.id, draft.id))
    assert out.status == FILED
    assert out.issue_number == 123
    assert out.issue_url.endswith("/123")
    assert out.project_attached is True
    assert calls == ["create", "attach"]


def test_dismiss_makes_no_github_call(monkeypatch, session, user_a):
    draft = _make_draft(session, user_a)

    def explode(*a, **k):
        raise AssertionError("no GitHub call on dismiss")

    monkeypatch.setattr(dev_svc.github, "create_issue", explode)
    monkeypatch.setattr(dev_svc.github, "add_issue_to_project", explode)
    out = dev_svc.dismiss_draft(session, user_a.id, draft.id)
    assert out.status == "dismissed"


def test_project_attach_failure_partial_then_retry(monkeypatch, session, user_a):
    """Create succeeds, attach fails → issue number recorded; retry re-runs ONLY the
    attach (no second issue created)."""
    dev_svc.add_token(session, user_a.id, "github_pat_X", ["org"], "octocat")
    draft = _make_draft(session, user_a)
    from app.dev.github import GithubError

    create_calls = {"n": 0}
    attach_calls = {"n": 0}

    async def fake_create(pat, owner, repo, title, body):
        create_calls["n"] += 1
        return {
            "number": 7,
            "url": "https://github.com/org/kaapi-backend/issues/7",
            "node_id": "ISSUE_NODE",
        }

    async def flaky_attach(pat, project_node_id, issue_node_id):
        attach_calls["n"] += 1
        if attach_calls["n"] == 1:
            raise GithubError(502, "project attach failed")
        return "ITEM_ID"

    monkeypatch.setattr(dev_svc.github, "create_issue", fake_create)
    monkeypatch.setattr(dev_svc.github, "add_issue_to_project", flaky_attach)

    from app.errors import ApiError

    with pytest.raises(ApiError):
        run(dev_svc.file_draft(session, user_a.id, draft.id))

    session.refresh(draft)
    assert draft.issue_number == 7  # partial state recorded
    assert draft.project_attached is False
    assert draft.status == FILED

    out = run(dev_svc.file_draft(session, user_a.id, draft.id))
    assert out.project_attached is True
    assert create_calls["n"] == 1  # never re-created
    assert attach_calls["n"] == 2


# ── Per-owner token routing ───────────────────────────────────────────────────


def test_file_routes_token_by_repo_owner(monkeypatch, session, user_a):
    """Two tokens (personal + org); filing an `org/…` draft uses the ORG token, never
    the personal one — a fine-grained PAT is bound to a single resource owner."""
    dev_svc.add_token(session, user_a.id, "PAT_PERSONAL", ["alice"], "alice")
    dev_svc.add_token(session, user_a.id, "PAT_ORG", ["org"], "alice")
    draft = _make_draft(session, user_a, repo="org/kaapi-backend")
    used: dict = {}

    async def fake_create(pat, owner, repo, title, body):
        used["pat"] = pat
        return {
            "number": 1,
            "url": "https://github.com/org/kaapi-backend/issues/1",
            "node_id": "N",
        }

    async def fake_attach(pat, project_node_id, issue_node_id):
        return "ITEM"

    monkeypatch.setattr(dev_svc.github, "create_issue", fake_create)
    monkeypatch.setattr(dev_svc.github, "add_issue_to_project", fake_attach)
    run(dev_svc.file_draft(session, user_a.id, draft.id))
    assert used["pat"] == "PAT_ORG"  # routed by the repo's owner


def test_file_without_a_token_for_owner_errors(monkeypatch, session, user_a):
    """A draft whose repo owner has no stored token can't be filed (and no issue is
    created)."""
    from app.errors import ApiError

    dev_svc.add_token(session, user_a.id, "PAT_PERSONAL", ["alice"], "alice")
    draft = _make_draft(session, user_a, repo="org/kaapi-backend")

    def explode(*a, **k):
        raise AssertionError("no GitHub call when no token routes to the owner")

    monkeypatch.setattr(dev_svc.github, "create_issue", explode)
    with pytest.raises(ApiError):
        run(dev_svc.file_draft(session, user_a.id, draft.id))


# ── The saved shelf (goal 12a) ────────────────────────────────────────────────


def _no_github(monkeypatch, why: str) -> None:
    """Arm every GitHub entry point to blow up — the spy that proves a status flip is
    purely local."""

    def explode(*a, **k):
        raise AssertionError(why)

    for fn in ("create_issue", "add_issue_to_project", "list_repos", "validate_pat"):
        monkeypatch.setattr(dev_svc.github, fn, explode)


def test_save_makes_no_github_call(monkeypatch, session, user_a):
    draft = _make_draft(session, user_a)
    _no_github(monkeypatch, "no GitHub call on save")
    out = dev_svc.save_draft(session, user_a.id, draft.id)
    assert out.status == SAVED


def test_save_and_unsave_are_idempotent(monkeypatch, session, user_a):
    draft = _make_draft(session, user_a)
    _no_github(monkeypatch, "no GitHub call on save/unsave")
    assert dev_svc.save_draft(session, user_a.id, draft.id).status == SAVED
    saved_again = dev_svc.save_draft(session, user_a.id, draft.id)
    assert saved_again.status == SAVED
    assert dev_svc.unsave_draft(session, user_a.id, draft.id).status == DRAFT
    assert dev_svc.unsave_draft(session, user_a.id, draft.id).status == DRAFT


def test_a_saved_draft_stays_fully_actionable(monkeypatch, session, user_a):
    """The shelf is not a freeze: a saved card is still editable, still dismissable, and
    still files through the unchanged `file_draft` path."""
    dev_svc.add_token(session, user_a.id, "github_pat_X", ["org"], "octocat")
    draft = _make_draft(session, user_a)
    dev_svc.save_draft(session, user_a.id, draft.id)

    edited = dev_svc.update_draft(session, user_a.id, draft.id, title="Edited on shelf")
    assert edited.title == "Edited on shelf"
    assert edited.status == SAVED

    async def fake_create(pat, owner, repo, title, body):
        return {"number": 7, "url": "https://github.com/org/x/issues/7", "node_id": "N"}

    async def fake_attach(pat, project_node_id, issue_node_id):
        return "ITEM_ID"

    monkeypatch.setattr(dev_svc.github, "create_issue", fake_create)
    monkeypatch.setattr(dev_svc.github, "add_issue_to_project", fake_attach)
    filed = run(dev_svc.file_draft(session, user_a.id, draft.id))
    assert filed.status == FILED and filed.issue_number == 7


def test_dismiss_reachable_from_the_saved_lane(monkeypatch, session, user_a):
    draft = _make_draft(session, user_a, status=SAVED)
    _no_github(monkeypatch, "no GitHub call on dismiss")
    assert dev_svc.dismiss_draft(session, user_a.id, draft.id).status == DISMISSED
    # …and the dismissed card can still climb back into review (the escape hatch).
    assert dev_svc.unsave_draft(session, user_a.id, draft.id).status == DRAFT


def test_save_unsave_endpoints_scoped_and_gated(auth, session, user_a, user_b):
    _enable_dev(session, user_a)
    _enable_dev(session, user_b)
    draft = _make_draft(session, user_a)

    client = auth.as_user(user_a)
    r = client.post(f"/dev/{draft.id}/save")
    assert r.status_code == 200 and r.json()["status"] == SAVED
    r = client.post(f"/dev/{draft.id}/unsave")
    assert r.status_code == 200 and r.json()["status"] == DRAFT

    # A second user cannot reach the first user's draft by id.
    other = auth.as_user(user_b)
    assert other.post(f"/dev/{draft.id}/save").status_code == 404
    assert other.post(f"/dev/{draft.id}/unsave").status_code == 404


def test_save_unsave_403_without_the_dev_flag(auth, session, user_a):
    draft = _make_draft(session, user_a)
    client = auth.as_user(user_a)  # user_a left unflagged
    assert client.post(f"/dev/{draft.id}/save").status_code == 403
    assert client.post(f"/dev/{draft.id}/unsave").status_code == 403


# ── Tabbed list + keyset pagination (goal 12a) ────────────────────────────────


def _seed_lane(session, user, status: str, n: int, *, minute0: int = 0) -> list[int]:
    """n drafts in one lane with strictly decreasing `updated_at` (newest first in the
    returned id list), so page order is deterministic."""
    ids = []
    for i in range(n):
        d = DevIssueDraft(
            user_id=user.id,
            title=f"{status}-{i}",
            body="",
            repo="org/kaapi-backend",
            status=status,
            sources="[]",
            updated_at=datetime(2026, 8, 1, 12, 0) - timedelta(minutes=minute0 + i),
        )
        session.add(d)
        session.commit()
        session.refresh(d)
        ids.append(d.id)
    return ids


def test_page_is_newest_first_and_ends_with_a_null_cursor(session, user_a):
    ids = _seed_lane(session, user_a, FILED, 3)
    items, next_cursor = dev_svc.list_drafts(session, user_a.id, status=FILED, limit=5)
    assert [d.id for d in items] == ids  # newest activity first
    assert next_cursor is None


def test_keyset_pagination_has_no_overlap_and_no_gap(session, user_a):
    """Follow the cursor across a >limit lane; a draft created between the two fetches
    must not shift or duplicate a row (keyset, not offset)."""
    ids = _seed_lane(session, user_a, FILED, 7)

    page1, cursor = dev_svc.list_drafts(session, user_a.id, status=FILED, limit=5)
    assert [d.id for d in page1] == ids[:5]
    assert cursor is not None

    # A newer row lands mid-scroll — with an offset this would push a row into page 2
    # twice; with a keyset it simply sits above the cursor.
    intruder = _seed_lane(session, user_a, FILED, 1, minute0=-10)[0]

    page2, cursor2 = dev_svc.list_drafts(
        session, user_a.id, status=FILED, limit=5, cursor=cursor
    )
    assert [d.id for d in page2] == ids[5:]  # exactly the remaining rows
    assert cursor2 is None  # last page
    seen = [d.id for d in page1] + [d.id for d in page2]
    assert len(seen) == len(set(seen)) == 7  # no overlap, no gap
    assert intruder not in seen  # the newcomer belongs above the cursor, not below it


def test_lane_filter_and_limit_clamp(session, user_a):
    _seed_lane(session, user_a, DRAFT, 2)
    _seed_lane(session, user_a, SAVED, 1, minute0=10)
    _seed_lane(session, user_a, DISMISSED, 1, minute0=20)
    review, _ = dev_svc.list_drafts(session, user_a.id, status=DRAFT, limit=100)
    assert len(review) == 2 and {d.status for d in review} == {DRAFT}
    # `limit` is clamped server-side, however large the caller asks for.
    assert len(dev_svc.list_drafts(session, user_a.id, limit=10_000)[0]) <= 50


def test_drafts_endpoint_serves_one_tab_at_a_time(auth, session, user_a):
    _enable_dev(session, user_a)
    ids = _seed_lane(session, user_a, FILED, 6)
    _seed_lane(session, user_a, DRAFT, 1, minute0=100)
    client = auth.as_user(user_a)

    r = client.get("/dev/drafts?status=filed&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert [d["id"] for d in body["items"]] == ids[:5]
    assert body["next_cursor"]

    r2 = client.get(f"/dev/drafts?status=filed&limit=5&cursor={body['next_cursor']}")
    page2 = r2.json()
    assert [d["id"] for d in page2["items"]] == ids[5:]
    assert page2["next_cursor"] is None

    # `review` maps to draft-status rows only.
    review = client.get("/dev/drafts?status=review").json()
    assert {d["status"] for d in review["items"]} == {DRAFT}
    assert client.get("/dev/drafts?status=nonsense").status_code == 400
    assert client.get("/dev/drafts?status=filed&cursor=notacursor").status_code == 400


def test_view_meta_carries_counts_and_no_draft_array(auth, session, user_a):
    _enable_dev(session, user_a)
    _seed_lane(session, user_a, DRAFT, 3)
    _seed_lane(session, user_a, SAVED, 2, minute0=10)
    _seed_lane(session, user_a, FILED, 1, minute0=20)
    client = auth.as_user(user_a)
    body = client.get("/dev").json()
    assert body["counts"] == {"review": 3, "saved": 2, "filed": 1, "dismissed": 0}
    assert "drafts" not in body
    assert "last_scan_at" in body and "config_complete" in body


# ── Secrets ───────────────────────────────────────────────────────────────────


def test_tokens_write_only_masked_in_config(monkeypatch, auth, session, user_a):
    _enable_dev(session, user_a)
    client = auth.as_user(user_a)

    async def fake_validate(pat):
        return {"login": "octocat", "name": "Octo"}

    async def fake_list_repos(pat, **kwargs):
        # The owner is derived from the repos the token can see.
        return [{"full_name": "octocat/notes", "description": "", "private": False}]

    monkeypatch.setattr("app.routers.dev.github.validate_pat", fake_validate)
    monkeypatch.setattr("app.routers.dev.github.list_repos", fake_list_repos)
    r = client.post("/dev/config/tokens", json={"pat": "github_pat_SECRETVALUE"})
    assert r.status_code == 200
    assert "SECRETVALUE" not in r.text
    assert r.json()["owners"] == ["octocat"]

    cfg = client.get("/dev/config")
    assert cfg.status_code == 200
    data = cfg.json()
    assert len(data["tokens"]) == 1
    tok = data["tokens"][0]
    assert tok["owner"] == "octocat"
    assert "SECRETVALUE" not in cfg.text
    assert tok["hint"] and "SECRET" not in tok["hint"]


# ── Gating & shell ────────────────────────────────────────────────────────────


def test_dev_endpoints_403_without_flag(auth, user_a):
    client = auth.as_user(user_a)
    assert client.get("/dev").status_code == 403
    assert client.post("/dev/scan-now").status_code == 403
    assert client.get("/dev/config").status_code == 403


def test_dev_endpoints_open_with_flag(auth, session, user_a):
    _enable_dev(session, user_a)
    client = auth.as_user(user_a)
    assert client.get("/dev").status_code == 200


def test_me_reports_dev_enabled(auth, session, user_a):
    client = auth.as_user(user_a)
    assert client.get("/auth/me").json()["dev_enabled"] is False
    _enable_dev(session, user_a)
    assert client.get("/auth/me").json()["dev_enabled"] is True


def test_scan_now_403_for_unflagged_user(auth, user_a):
    client = auth.as_user(user_a)
    assert client.post("/dev/scan-now").status_code == 403


def test_cron_scans_only_flagged_configured_users(
    monkeypatch, engine, session, user_a, user_b
):
    """The scheduled tick reads Docs + calls the LLM for flagged, configured users only —
    an unflagged user gets zero scans."""
    from app.dev import scheduler as dev_sched

    # Point the scheduler's Session at the test engine (it imports app.db.engine).
    monkeypatch.setattr(dev_sched, "engine", engine)

    _enable_dev(session, user_a)  # user_b left unflagged
    _seed_config(session, user_a, sources=["d1"], repos=_REPOS, docs_forest=_FOREST)

    scanned: list = []

    async def spy_run_scan(sess, user, creds):
        scanned.append(user.id)
        return {"docs_read": 0, "new_entries": 0, "drafts_created": 0}

    monkeypatch.setattr(dev_sched.dev_svc, "run_scan", spy_run_scan)
    monkeypatch.setattr(dev_sched, "_daily_due", lambda last: True)
    monkeypatch.setattr(
        dev_sched.google_auth, "load_credentials", lambda s, u: DummyCreds()
    )

    run(dev_sched._tick_all_users())
    assert scanned == [user_a.id]  # user_b (unflagged) never scanned


# ── Goal 12b: GitHub read path ────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.reason_phrase = "err"

    def json(self):
        return self._data


def _fake_httpx(monkeypatch, route, calls):
    """Point `github.py`'s httpx.AsyncClient at canned responses. `route(url, params)`
    returns a `_FakeResp`; every request is appended to `calls` for spy assertions."""

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            calls.append((url, dict(params or {})))
            return route(url, dict(params or {}))

        async def post(self, url, headers=None, json=None):
            calls.append((url, json))
            return route(url, json)

    monkeypatch.setattr(gh.httpx, "AsyncClient", _Client)


def test_list_open_issues_filters_prs_and_caps(monkeypatch):
    """The issues endpoint interleaves PRs (rows with a `pull_request` key — the known
    gotcha); those are filtered out, and the fetch caps at DEV_ISSUE_FETCH_CAP."""
    from app.dev import config as dev_config

    monkeypatch.setattr(dev_config, "DEV_ISSUE_FETCH_CAP", 3)
    rows = [
        {
            "number": 1,
            "title": "A",
            "labels": [{"name": "bug"}],
            "html_url": "u1",
            "updated_at": "t1",
        },
        {
            "number": 2,
            "title": "a PR in disguise",
            "pull_request": {"url": "x"},
            "html_url": "u2",
            "updated_at": "t2",
        },
        {"number": 3, "title": "B", "labels": [], "html_url": "u3", "updated_at": "t3"},
        {"number": 4, "title": "C", "labels": [], "html_url": "u4", "updated_at": "t4"},
        {"number": 5, "title": "D", "labels": [], "html_url": "u5", "updated_at": "t5"},
    ]
    calls: list = []
    _fake_httpx(monkeypatch, lambda url, p: _FakeResp(rows), calls)

    out = run(gh.list_open_issues("PAT", "org", "repo"))
    assert [i["number"] for i in out] == [1, 3, 4]  # PR row skipped, capped at 3
    assert out[0]["labels"] == ["bug"]
    assert set(out[0]) == {"number", "title", "labels", "html_url", "updated_at"}
    # The fetch asked for open issues, most recently updated first.
    url, params = calls[0]
    assert url.endswith("/repos/org/repo/issues")
    assert params["state"] == "open" and params["sort"] == "updated"


def test_list_recent_prs_keeps_open_and_merged_skips_abandoned(monkeypatch):
    from app.dev import config as dev_config

    monkeypatch.setattr(dev_config, "DEV_PR_FETCH_CAP", 10)
    rows = [
        {
            "number": 10,
            "title": "Open PR",
            "state": "open",
            "merged_at": None,
            "body": "x" * 1000,
            "html_url": "p1",
            "updated_at": "t",
        },
        {
            "number": 11,
            "title": "Merged PR",
            "state": "closed",
            "merged_at": "2026-01-01T00:00:00Z",
            "body": None,
            "html_url": "p2",
            "updated_at": "t",
        },
        {
            "number": 12,
            "title": "Abandoned PR",
            "state": "closed",
            "merged_at": None,
            "body": "gone",
            "html_url": "p3",
            "updated_at": "t",
        },
    ]
    calls: list = []
    _fake_httpx(monkeypatch, lambda url, p: _FakeResp(rows), calls)

    out = run(gh.list_recent_prs("PAT", "org", "repo"))
    assert [(p["number"], p["state"]) for p in out] == [(10, "open"), (11, "merged")]
    assert len(out[0]["description_excerpt"]) == 400  # truncated code-side
    assert set(out[0]) == {
        "number",
        "title",
        "state",
        "description_excerpt",
        "html_url",
        "updated_at",
    }


def test_candidate_fetch_makes_zero_commit_calls(monkeypatch):
    """Commit subjects are fetched ONLY for a matched PR at the drafter stage — the
    candidate fetches never call `/pulls/{n}/commits` (call-count spy)."""
    calls: list = []
    _fake_httpx(monkeypatch, lambda url, p: _FakeResp([]), calls)
    run(gh.list_open_issues("PAT", "org", "repo"))
    run(gh.list_recent_prs("PAT", "org", "repo"))
    assert calls  # both fetches happened…
    assert not any("/commits" in url for url, _ in calls)  # …and no commit call did


def test_list_pr_commit_subjects_first_lines_only(monkeypatch):
    calls: list = []
    rows = [
        {"commit": {"message": "feat: add login\n\nlong body\nwith details"}},
        {"commit": {"message": "fix typo"}},
    ]
    _fake_httpx(monkeypatch, lambda url, p: _FakeResp(rows), calls)
    out = run(gh.list_pr_commit_subjects("PAT", "org", "repo", 45))
    assert out == ["feat: add login", "fix typo"]  # subjects, never bodies
    assert calls[0][0].endswith("/pulls/45/commits")


# ── Goal 12b: matcher prompt contract + streaming ─────────────────────────────


def test_match_payload_pinned_fields_no_urls_ids_or_logins():
    """The matcher payload is EXACTLY the pinned field sets — candidate URLs, doc ids,
    tokens, and member logins never reach the prompt (the `ENTRY_FIELDS` pattern)."""
    from app.dev import synth

    drafts = [
        {"id": 77, "title": "T", "body": "B", "repo": "org/x", "doc_id": "SECRET_DOC"}
    ]
    issues = [
        {
            "number": 1,
            "title": "I",
            "labels": ["bug"],
            "html_url": "https://SECRET_ISSUE_URL",
            "updated_at": "ts",
        }
    ]
    prs = [
        {
            "number": 2,
            "title": "P",
            "state": "merged",
            "description_excerpt": "d",
            "html_url": "https://SECRET_PR_URL",
            "updated_at": "ts",
            "login": "SECRET_LOGIN",
        }
    ]

    d, i, p = synth.build_match_payload(drafts, issues, prs)
    assert set(d[0]) == set(synth.DRAFT_MATCH_FIELDS)
    assert set(i[0]) == set(synth.ISSUE_CANDIDATE_FIELDS)
    assert set(p[0]) == set(synth.PR_CANDIDATE_FIELDS)
    assert d[0]["draft_index"] == 0  # positional index, never the DB id

    _system, user = synth.build_match_prompt(d, i, p)
    for secret in (
        "SECRET_DOC",
        "SECRET_ISSUE_URL",
        "SECRET_PR_URL",
        "SECRET_LOGIN",
        "github_pat",
        '"id": 77',
    ):
        assert secret not in user
    assert "I" in user and "P" in user and "T" in user


class _FakeMessage:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason


def _fake_anthropic(monkeypatch, message, seen):
    from app.dev import synth

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_final_message(self):
            return message

    class _Messages:
        def stream(self, **kwargs):
            seen.update(kwargs)
            return _Stream()

    class _Client:
        def __init__(self):
            self.messages = _Messages()

    monkeypatch.setattr(synth.anthropic, "AsyncAnthropic", _Client)


def test_matcher_streams_with_its_own_model_and_budget(monkeypatch):
    """`match_issues` streams (the `5c6b48e` lesson: a 50+ draft backlog must not
    truncate) with DEV_MATCH_MODEL / DEV_MATCH_MAX_TOKENS, and a max_tokens stop is a
    FAILURE (None — drafts stay NULL and retry), not an empty answer."""
    from app.dev import config as dev_config
    from app.dev import synth

    parsed = MatchResult(drafts=[DraftMatches(draft_index=0, matches=[])])
    seen: dict = {}
    _fake_anthropic(monkeypatch, _FakeMessage(parsed), seen)
    out = run(synth.match_issues([{"title": "t", "body": "b"}], [], []))
    assert out is parsed
    assert seen["model"] == dev_config.DEV_MATCH_MODEL
    assert seen["max_tokens"] == dev_config.DEV_MATCH_MAX_TOKENS

    _fake_anthropic(monkeypatch, _FakeMessage(None, stop_reason="max_tokens"), {})
    assert run(synth.match_issues([{"title": "t", "body": "b"}], [], [])) is None


def test_comment_drafter_uses_dev_model_and_its_own_budget(monkeypatch):
    from app.dev import config as dev_config
    from app.dev import synth

    parsed = CommentDraftResult(has_new_info=False, comment_markdown=None)
    seen: dict = {}
    _fake_anthropic(monkeypatch, _FakeMessage(parsed), seen)
    out = run(
        synth.draft_comment(
            {"title": "t", "body": "b"},
            {"number": 1, "title": "i", "body": "x", "comments": []},
            [],
        )
    )
    assert out is parsed
    assert seen["model"] == dev_config.DEV_MODEL  # human-facing text → the opus knob
    assert seen["max_tokens"] == dev_config.DEV_COMMENT_MAX_TOKENS


# ── Goal 12b: match dispose + conversion ──────────────────────────────────────

_ISSUE_CANDS = [
    {
        "number": 12,
        "title": "Login is broken",
        "labels": ["bug"],
        "html_url": "https://github.com/org/kaapi-backend/issues/12",
        "updated_at": "t",
    },
]
_PR_CANDS = [
    {
        "number": 45,
        "title": "Fix login flow",
        "state": "merged",
        "description_excerpt": "fixes login",
        "html_url": "https://github.com/org/kaapi-backend/pull/45",
        "updated_at": "t",
    },
]


def _seed_matching(session, user) -> None:
    """Token + repo catalog — the match phase fetches candidates for every catalog
    repo (12b.1), so direct `_match_and_convert` tests need one configured."""
    dev_svc.add_token(session, user.id, "github_pat_X", ["org"], "octocat")
    dev_svc.set_repos(session, user.id, _REPOS)


def _patch_candidates(
    monkeypatch, issues=_ISSUE_CANDS, prs=_PR_CANDS, repo="org/kaapi-backend"
):
    """Stub the candidate fetches: only `repo` has issues/PRs, every other catalog
    repo comes back empty (mirrors the owner's real setup)."""

    async def fake_issues(pat, owner, name):
        return issues if f"{owner}/{name}" == repo else []

    async def fake_prs(pat, owner, name):
        return prs if f"{owner}/{name}" == repo else []

    monkeypatch.setattr(dev_svc.github, "list_open_issues", fake_issues)
    monkeypatch.setattr(dev_svc.github, "list_recent_prs", fake_prs)


def _patch_matcher(monkeypatch, result, calls=None):
    async def fake_match(drafts, issue_cands, pr_cands):
        if calls is not None:
            calls.append(drafts)
        return result

    monkeypatch.setattr(dev_svc.synth, "match_issues", fake_match)


def _matches(*pairs) -> MatchResult:
    """(draft_index, [(number, type, confidence[, repo]), …]) → a MatchResult. The
    repo defaults to the candidate-bearing test repo."""
    return MatchResult(
        drafts=[
            DraftMatches(
                draft_index=idx,
                matches=[
                    ProposedMatch(
                        repo=m[3] if len(m) > 3 else "org/kaapi-backend",
                        number=m[0],
                        type=m[1],
                        confidence=m[2],
                        reason="looks the same",
                    )
                    for m in ms
                ],
            )
            for idx, ms in pairs
        ]
    )


def test_match_dispose_drops_out_of_set_and_takes_urls_from_the_fetch(
    monkeypatch, session, user_a
):
    """Every LLM-returned (number, type) outside the fetched candidate set is dropped,
    and the stored url/title/type/state come from the code-fetched list — never from
    the model. The `Related:` body line is built code-side from validated matches."""
    _seed_matching(session, user_a)
    draft = _make_draft(session, user_a, project_node_id=None, project_title=None)
    _patch_candidates(monkeypatch)
    # A bogus number (999), a mistyped pair (45 as issue) and a right-number/wrong-repo
    # pair (12 claimed in kaapi-web, which has no candidates) — all must be dropped.
    _patch_matcher(
        monkeypatch,
        _matches(
            (
                0,
                [
                    (12, "issue", "medium"),
                    (45, "pr", "medium"),
                    (999, "issue", "high"),
                    (45, "issue", "high"),
                    (12, "issue", "high", "org/kaapi-web"),
                ],
            )
        ),
    )

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert tally == {"linked": 1, "converted": 0, "matching_skipped": False}

    session.refresh(draft)
    stored = json.loads(draft.related_issues)
    assert [(m["number"], m["type"]) for m in stored] == [(12, "issue"), (45, "pr")]
    assert stored[0]["url"] == _ISSUE_CANDS[0]["html_url"]  # provably from the fetch
    assert stored[0]["title"] == "Login is broken" and stored[0]["state"] == "open"
    assert stored[0]["repo"] == "org/kaapi-backend"  # from the fetch, not the model
    assert (
        stored[1]["url"] == _PR_CANDS[0]["html_url"] and stored[1]["state"] == "merged"
    )
    assert draft.body.endswith("**Related:** #12, PR #45 (merged)")
    assert draft.kind == KIND_ISSUE  # medium confidence never converts


def test_high_issue_match_with_new_info_converts_to_comment_draft(
    monkeypatch, session, user_a
):
    """The full conversion: thread fetched for the ONE matched issue, commit subjects
    for the ONE high-matched PR, drafter says has_new_info → kind=comment with target
    set from the validated match, body replaced, project cleared — and the draft stays
    in its lane (never auto-dismissed, never auto-filed)."""
    _seed_matching(session, user_a)
    draft = _make_draft(session, user_a)  # carries a project preselect
    _patch_candidates(monkeypatch)
    _patch_matcher(
        monkeypatch, _matches((0, [(12, "issue", "high"), (45, "pr", "high")]))
    )

    fetched: list = []

    async def fake_get_issue(pat, owner, repo, number):
        fetched.append(("issue", number))
        return {
            "number": number,
            "title": "Login is broken",
            "body": "old body",
            "state": "open",
            "html_url": _ISSUE_CANDS[0]["html_url"],
        }

    async def fake_comments(pat, owner, repo, number):
        fetched.append(("comments", number))
        return [{"author": "octocat", "body": "any news?", "created_at": "t"}]

    async def fake_subjects(pat, owner, repo, number):
        fetched.append(("commits", number))
        return ["fix: login flow"]

    monkeypatch.setattr(dev_svc.github, "get_issue", fake_get_issue)
    monkeypatch.setattr(dev_svc.github, "list_issue_comments", fake_comments)
    monkeypatch.setattr(dev_svc.github, "list_pr_commit_subjects", fake_subjects)

    drafter_in: dict = {}

    async def fake_draft_comment(d, thread, related_prs):
        drafter_in.update({"draft": d, "thread": thread, "prs": related_prs})
        return CommentDraftResult(
            has_new_info=True, comment_markdown="New repro: happens on Safari too."
        )

    monkeypatch.setattr(dev_svc.synth, "draft_comment", fake_draft_comment)

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert tally["linked"] == 1 and tally["converted"] == 1

    session.refresh(draft)
    assert draft.kind == KIND_COMMENT
    assert draft.target_issue_number == 12
    assert draft.target_issue_url == _ISSUE_CANDS[0]["html_url"]  # from the fetch
    assert draft.body.startswith("New repro: happens on Safari too.")
    assert "PR #45 (merged)" in draft.body  # secondary link still lands in the comment
    assert "#12" not in draft.body  # the target itself is excluded from Related
    assert draft.project_node_id is None and draft.project_title is None
    assert draft.status == DRAFT  # still the human's decision
    # Thread fetched once, commit subjects only for the matched PR (45).
    assert fetched == [("issue", 12), ("comments", 12), ("commits", 45)]
    assert drafter_in["prs"][0]["commit_subjects"] == ["fix: login flow"]


def test_pr_only_high_match_stays_a_linked_issue_draft(monkeypatch, session, user_a):
    """PRs are never comment targets: a draft whose only high match is a PR keeps
    kind=issue and no thread fetch or drafter call ever happens."""
    _seed_matching(session, user_a)
    draft = _make_draft(session, user_a)
    _patch_candidates(monkeypatch, issues=[])
    _patch_matcher(monkeypatch, _matches((0, [(45, "pr", "high")])))

    def explode(*a, **k):
        raise AssertionError("a PR-only match must not fetch threads or draft comments")

    monkeypatch.setattr(dev_svc.github, "get_issue", explode)
    monkeypatch.setattr(dev_svc.synth, "draft_comment", explode)

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert tally["linked"] == 1 and tally["converted"] == 0
    session.refresh(draft)
    assert draft.kind == KIND_ISSUE and draft.target_issue_number is None
    assert json.loads(draft.related_issues)[0]["type"] == "pr"


def test_nothing_new_flags_the_match_and_never_auto_dismisses(
    monkeypatch, session, user_a
):
    _seed_matching(session, user_a)
    draft = _make_draft(session, user_a)
    _patch_candidates(monkeypatch, prs=[])
    _patch_matcher(monkeypatch, _matches((0, [(12, "issue", "high")])))

    async def fake_get_issue(pat, owner, repo, number):
        return {
            "number": number,
            "title": "Login is broken",
            "body": "covers it all",
            "state": "open",
            "html_url": _ISSUE_CANDS[0]["html_url"],
        }

    async def fake_comments(pat, owner, repo, number):
        return []

    async def nothing_new(d, thread, related_prs):
        return CommentDraftResult(has_new_info=False, comment_markdown=None)

    monkeypatch.setattr(dev_svc.github, "get_issue", fake_get_issue)
    monkeypatch.setattr(dev_svc.github, "list_issue_comments", fake_comments)
    monkeypatch.setattr(dev_svc.synth, "draft_comment", nothing_new)

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert tally["converted"] == 0
    session.refresh(draft)
    assert draft.kind == KIND_ISSUE
    assert draft.status == DRAFT  # flagged, not auto-dismissed — the human decides
    top = json.loads(draft.related_issues)[0]
    assert top["number"] == 12 and top.get("nothing_new") is True


def test_matcher_scope_whole_unsettled_backlog_once_per_draft(
    monkeypatch, session, user_a
):
    """The matcher targets every non-settled draft (review + saved) whose
    related_issues is NULL — filed/dismissed never, already-matched never again."""
    _seed_matching(session, user_a)
    in_review = _make_draft(session, user_a, status=DRAFT)
    shelved = _make_draft(session, user_a, status=SAVED)
    _make_draft(session, user_a, status=FILED)
    _make_draft(session, user_a, status=DISMISSED)
    already = _make_draft(session, user_a, status=DRAFT, related_issues="[]")

    _patch_candidates(monkeypatch)
    calls: list = []
    _patch_matcher(monkeypatch, MatchResult(drafts=[]), calls)

    run(dev_svc._match_and_convert(session, user_a.id))
    assert len(calls) == 1
    assert len(calls[0]) == 2  # exactly the review + saved NULL drafts

    # Second pass: both got "[]" stored — nothing left to match, no LLM call.
    run(dev_svc._match_and_convert(session, user_a.id))
    assert len(calls) == 1
    session.refresh(in_review)
    session.refresh(shelved)
    session.refresh(already)
    assert in_review.related_issues == "[]" and shelved.related_issues == "[]"
    assert already.related_issues == "[]"


def test_scan_with_raising_github_read_still_persists_and_advances(
    monkeypatch, session, user_a
):
    """A GitHub read failure degrades the repo to 'no match info': drafts persist
    (related_issues NULL), the cursor advances, and the tally says matching was
    skipped — a read failure never blocks synthesis output."""
    _seed_config(session, user_a, sources=["d1"], repos=_REPOS, docs_forest=_FOREST)
    _patch_docs(
        monkeypatch, {"DOC1": _doc(("x", "6-July-2026, 8:41 PM IST", None, "b"))}
    )
    result = SynthesisResult(
        issues=[
            ProposedIssue(
                title="Fix login",
                body_markdown="body",
                repo="org/kaapi-backend",
                sources=[],
            )
        ]
    )
    _patch_synth(monkeypatch, result, [])

    async def raising_fetch(pat, owner, repo):
        raise gh.GithubError(500, "boom")

    monkeypatch.setattr(dev_svc.github, "list_open_issues", raising_fetch)
    monkeypatch.setattr(dev_svc.github, "list_recent_prs", raising_fetch)

    t1 = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert t1["drafts_created"] == 1
    assert t1["matching_skipped"] is True and t1["linked"] == 0

    drafts, _ = dev_svc.list_drafts(session, user_a.id)
    assert len(drafts) == 1 and drafts[0].related_issues is None  # retried next scan

    t2 = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert t2["new_entries"] == 0  # cursor advanced despite the failed match phase


def test_scan_tally_reports_linked_and_converted(monkeypatch, session, user_a):
    """The happy-path tail end-to-end through `run_scan`: the backlog draft gets
    linked, and the tally carries the new counts."""
    _seed_config(session, user_a, sources=["d1"], repos=_REPOS, docs_forest=_FOREST)
    _patch_docs(
        monkeypatch, {"DOC1": _doc(("x", "6-July-2026, 8:41 PM IST", None, "b"))}
    )
    _patch_synth(monkeypatch, SynthesisResult(issues=[]), [])
    _make_draft(session, user_a)  # a lingering backlog draft, NULL related_issues
    _patch_candidates(monkeypatch)
    _patch_matcher(monkeypatch, _matches((0, [(12, "issue", "medium")])))

    tally = run(dev_svc.run_scan(session, user_a, DummyCreds()))
    assert tally["linked"] == 1 and tally["converted"] == 0
    assert tally["matching_skipped"] is False


# ── Goal 12b: repo change + comment re-target guard ───────────────────────────


def test_repo_change_keeps_catalog_wide_matches(session, user_a):
    """12b.1: matches are judged against the WHOLE catalog, so correcting a
    synthesiser mis-tag must not throw them away (re-matching would also never fire —
    the NULL-guard is once per draft)."""
    stored = '[{"repo": "org/kaapi-backend", "number": 12}]'
    draft = _make_draft(session, user_a, related_issues=stored)
    out = dev_svc.update_draft(session, user_a.id, draft.id, repo="org/kaapi-web")
    assert out.repo == "org/kaapi-web"
    assert out.related_issues == stored  # still valid — catalog-wide
    # A body-only edit leaves the matches alone too.
    draft2 = _make_draft(session, user_a, related_issues="[]")
    out2 = dev_svc.update_draft(session, user_a.id, draft2.id, body="edited")
    assert out2.related_issues == "[]"


def test_comment_draft_cannot_be_retargeted(session, user_a):
    from app.errors import ApiError

    draft = _make_draft(
        session,
        user_a,
        kind=KIND_COMMENT,
        target_issue_number=12,
        target_issue_url="https://github.com/org/kaapi-backend/issues/12",
    )
    with pytest.raises(ApiError):
        dev_svc.update_draft(session, user_a.id, draft.id, repo="org/kaapi-web")
    # Same-repo (no-op) and body edits stay allowed — the card body is editable.
    out = dev_svc.update_draft(
        session, user_a.id, draft.id, repo=draft.repo, body="sharper comment"
    )
    assert out.body == "sharper comment" and out.kind == KIND_COMMENT


# ── Goal 12b: filing a comment draft (the third write) ────────────────────────


def _make_comment_draft(session, user, **over) -> DevIssueDraft:
    return _make_draft(
        session,
        user,
        kind=KIND_COMMENT,
        target_issue_number=over.pop("target_issue_number", 12),
        target_issue_url="https://github.com/org/kaapi-backend/issues/12",
        project_node_id=None,
        project_title=None,
        body=over.pop("body", "New repro detail."),
        **over,
    )


def test_filing_a_comment_draft_posts_exactly_one_comment(monkeypatch, session, user_a):
    """kind=comment files as ONE comments-endpoint call — zero create_issue, zero
    project attach — and stores the comment URL against the existing columns."""
    _seed_matching(session, user_a)
    draft = _make_comment_draft(session, user_a)

    def explode(*a, **k):
        raise AssertionError(
            "a comment draft must never create an issue or attach a project"
        )

    monkeypatch.setattr(dev_svc.github, "create_issue", explode)
    monkeypatch.setattr(dev_svc.github, "add_issue_to_project", explode)

    posted: list = []

    async def fake_comment(pat, owner, repo, number, body):
        posted.append((owner, repo, number, body))
        return {"url": "https://github.com/org/kaapi-backend/issues/12#issuecomment-9"}

    monkeypatch.setattr(dev_svc.github, "create_issue_comment", fake_comment)

    out = run(dev_svc.file_draft(session, user_a.id, draft.id))
    assert posted == [("org", "kaapi-backend", 12, "New repro detail.")]
    assert out.status == FILED
    assert out.issue_url.endswith("#issuecomment-9")  # the COMMENT's url
    assert out.issue_number == 12

    # A re-click never double-posts (the already-posted guard).
    run(dev_svc.file_draft(session, user_a.id, draft.id))
    assert len(posted) == 1


def test_comment_filing_failure_leaves_the_draft_for_retry(
    monkeypatch, session, user_a
):
    from app.errors import ApiError

    _seed_matching(session, user_a)
    draft = _make_comment_draft(session, user_a)

    async def failing_comment(pat, owner, repo, number, body):
        raise gh.GithubError(502, "comment failed")

    monkeypatch.setattr(dev_svc.github, "create_issue_comment", failing_comment)
    with pytest.raises(ApiError):
        run(dev_svc.file_draft(session, user_a.id, draft.id))
    session.refresh(draft)
    assert draft.status == DRAFT and draft.issue_url is None  # untouched, retryable


def test_comment_file_endpoint_gated_and_scoped(
    monkeypatch, auth, session, user_a, user_b
):
    draft = _make_comment_draft(session, user_a)
    # No dev flag → 403 before anything else.
    assert auth.as_user(user_a).post(f"/dev/{draft.id}/file").status_code == 403
    _enable_dev(session, user_a)
    _enable_dev(session, user_b)
    # A second user cannot reach the first user's draft by id.
    assert auth.as_user(user_b).post(f"/dev/{draft.id}/file").status_code == 404


# ── Goal 12b: @-mention members endpoint ──────────────────────────────────────


def test_members_endpoint_routes_token_and_degrades_to_empty(
    monkeypatch, auth, session, user_a
):
    client = auth.as_user(user_a)
    assert client.get("/dev/config/members?repo=org/kaapi-backend").status_code == 403

    _enable_dev(session, user_a)
    assert client.get("/dev/config/members?repo=notarepo").status_code == 400

    # No token stored for the owner → empty list, not an error.
    r = client.get("/dev/config/members?repo=org/kaapi-backend")
    assert r.status_code == 200 and r.json()["members"] == []

    dev_svc.add_token(session, user_a.id, "PAT_PERSONAL", ["alice"], "alice")
    dev_svc.add_token(session, user_a.id, "PAT_ORG", ["org"], "alice")
    used: dict = {}

    async def fake_assignees(pat, owner, repo):
        used["pat"] = pat
        return [{"login": "teammate", "name": "Team Mate"}]

    monkeypatch.setattr("app.routers.dev.github.list_assignees", fake_assignees)
    r = client.get("/dev/config/members?repo=org/kaapi-backend")
    assert r.json()["members"] == [{"login": "teammate", "name": "Team Mate"}]
    assert used["pat"] == "PAT_ORG"  # routed by the repo's owner

    async def forbidden(pat, owner, repo):
        raise gh.GithubError(403, "resource not accessible")

    monkeypatch.setattr("app.routers.dev.github.list_assignees", forbidden)
    r = client.get("/dev/config/members?repo=org/kaapi-backend")
    assert (
        r.status_code == 200 and r.json()["members"] == []
    )  # typeahead offers nothing


def test_draft_serializer_carries_kind_target_and_matches(auth, session, user_a):
    _enable_dev(session, user_a)
    _make_comment_draft(
        session,
        user_a,
        related_issues='[{"number": 12, "type": "issue", "state": "open", "url": "u", "title": "t", "confidence": "high", "reason": "r"}]',
    )
    client = auth.as_user(user_a)
    d = client.get("/dev/drafts?status=review").json()["items"][0]
    assert d["kind"] == "comment"
    assert d["target_issue_number"] == 12 and d["target_issue_url"]
    assert d["related_issues"][0]["number"] == 12
    # An unmatched draft reports null (not yet matched), not [].
    _make_draft(session, user_a, title="unmatched")
    items = client.get("/dev/drafts?status=review").json()["items"]
    fresh = next(i for i in items if i["title"] == "unmatched")
    assert fresh["related_issues"] is None


def test_matcher_chunks_a_large_backlog_and_a_failed_chunk_retries(
    monkeypatch, session, user_a
):
    """The prod-backlog lesson (2026-08-20): 78 drafts in ONE matcher call truncated at
    the output budget and matched nothing. Drafts are chunked across calls (candidates
    repeated, matches merged); a failed chunk skips ONLY itself — its drafts stay NULL
    and are the only ones re-sent next scan."""
    from app.dev import config as dev_config

    monkeypatch.setattr(dev_config, "DEV_MATCH_DRAFT_CHUNK", 2)
    _seed_matching(session, user_a)
    drafts = [_make_draft(session, user_a, title=f"draft-{i}") for i in range(5)]
    _patch_candidates(monkeypatch)

    calls: list = []

    async def fake_match(chunk, issue_cands, pr_cands):
        calls.append(chunk)
        if len(calls) == 2:
            return None  # this chunk truncated / errored
        return _matches((0, [(12, "issue", "medium")]))

    monkeypatch.setattr(dev_svc.synth, "match_issues", fake_match)

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert [len(c) for c in calls] == [2, 2, 1]  # 5 drafts → chunks of 2, 2, 1
    assert tally["matching_skipped"] is True  # the failed chunk is reported…
    assert tally["linked"] == 2  # …but the other chunks landed (index 0 of each)

    for d in drafts:
        session.refresh(d)
    assert drafts[0].related_issues is not None and drafts[1].related_issues == "[]"
    assert drafts[2].related_issues is None and drafts[3].related_issues is None
    assert drafts[4].related_issues is not None

    # Next scan: only the failed chunk's drafts are still NULL — exactly they re-send.
    run(dev_svc._match_and_convert(session, user_a.id))
    assert [len(c) for c in calls] == [2, 2, 1, 2]
    assert {c["title"] for c in calls[3]} == {"draft-2", "draft-3"}


# ── Goal 12b.1: catalog-wide matching (mis-tagged drafts) ─────────────────────


def test_cross_repo_match_links_with_full_reference(monkeypatch, session, user_a):
    """A mis-tagged draft (the synthesiser fell back to the wrong repo) still gets
    linked: candidates are catalog-wide, and the Related line uses GitHub's cross-repo
    `owner/repo#N` form so the reference auto-links from the wrong repo too."""
    _seed_matching(session, user_a)
    draft = _make_draft(session, user_a, repo="org/kaapi-web")  # the wrong tag
    _patch_candidates(monkeypatch)  # issues/PRs exist only in org/kaapi-backend
    _patch_matcher(
        monkeypatch, _matches((0, [(12, "issue", "medium"), (45, "pr", "medium")]))
    )

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert tally["linked"] == 1
    session.refresh(draft)
    stored = json.loads(draft.related_issues)
    assert stored[0]["repo"] == "org/kaapi-backend"
    assert draft.body.endswith(
        "**Related:** org/kaapi-backend#12, PR org/kaapi-backend#45 (merged)"
    )
    assert draft.repo == "org/kaapi-web"  # linking alone never re-tags


def test_cross_repo_conversion_retags_the_draft(monkeypatch, session, user_a):
    """The prod mis-tag scenario end-to-end: the real issue lives in another catalog
    repo; a high-confidence match fetches THAT repo's thread, converts the draft, and
    re-tags it to the target's repo — the comment files where the issue lives."""
    _seed_matching(session, user_a)
    draft = _make_draft(session, user_a, repo="org/kaapi-web")
    _patch_candidates(monkeypatch, prs=[])
    _patch_matcher(monkeypatch, _matches((0, [(12, "issue", "high")])))

    async def fake_get_issue(pat, owner, repo, number):
        assert f"{owner}/{repo}" == "org/kaapi-backend"  # the MATCH's repo, not the tag
        return {
            "number": number,
            "title": "Login is broken",
            "body": "x",
            "state": "open",
            "html_url": _ISSUE_CANDS[0]["html_url"],
        }

    async def fake_comments(pat, owner, repo, number):
        return []

    async def fake_draft_comment(d, thread, related_prs):
        return CommentDraftResult(has_new_info=True, comment_markdown="Adding a repro.")

    monkeypatch.setattr(dev_svc.github, "get_issue", fake_get_issue)
    monkeypatch.setattr(dev_svc.github, "list_issue_comments", fake_comments)
    monkeypatch.setattr(dev_svc.synth, "draft_comment", fake_draft_comment)

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert tally["converted"] == 1
    session.refresh(draft)
    assert draft.kind == KIND_COMMENT
    assert draft.repo == "org/kaapi-backend"  # re-tagged: comment lives with the issue
    assert draft.target_issue_number == 12
    assert draft.target_issue_url == _ISSUE_CANDS[0]["html_url"]


def test_issues_disabled_repo_is_excluded_but_matching_proceeds(
    monkeypatch, session, user_a
):
    """410 on /issues (issues disabled) or 404 (renamed / out of the PAT's grant) is
    PERMANENT — retrying can't fix it, so that list is excluded (reported via
    matching_skipped) and matching still runs for everything else."""
    _seed_matching(session, user_a)
    draft = _make_draft(session, user_a)

    async def issues_or_410(pat, owner, name):
        if f"{owner}/{name}" == "org/kaapi-backend":
            return _ISSUE_CANDS
        raise gh.GithubError(410, "Issues are disabled for this repo")

    async def prs_empty(pat, owner, name):
        return _PR_CANDS if f"{owner}/{name}" == "org/kaapi-backend" else []

    monkeypatch.setattr(dev_svc.github, "list_open_issues", issues_or_410)
    monkeypatch.setattr(dev_svc.github, "list_recent_prs", prs_empty)
    _patch_matcher(monkeypatch, _matches((0, [(12, "issue", "medium")])))

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert tally["linked"] == 1  # matching ran despite the disabled-issues repo
    assert tally["matching_skipped"] is True  # …and the exclusion is reported
    session.refresh(draft)
    assert draft.related_issues is not None


def test_transient_fetch_failure_aborts_the_whole_match_phase(
    monkeypatch, session, user_a
):
    """A 5xx/network failure is transient: the phase aborts so NO draft settles
    matched-empty while its true match's repo was unreachable — everything stays NULL
    and retries next scan."""
    _seed_matching(session, user_a)
    draft = _make_draft(session, user_a)
    _patch_candidates(monkeypatch)  # kaapi-backend fetch succeeds…

    async def prs_boom(pat, owner, name):
        if f"{owner}/{name}" == "org/kaapi-web":
            raise gh.GithubError(502, "bad gateway")  # …then kaapi-web dies
        return _PR_CANDS

    monkeypatch.setattr(dev_svc.github, "list_recent_prs", prs_boom)

    def no_matcher(*a, **k):
        raise AssertionError("the matcher must not run after an aborted fetch")

    monkeypatch.setattr(dev_svc.synth, "match_issues", no_matcher)

    tally = run(dev_svc._match_and_convert(session, user_a.id))
    assert tally == {"linked": 0, "converted": 0, "matching_skipped": True}
    session.refresh(draft)
    assert draft.related_issues is None  # still queued — retried next scan


# ── Filing failures: a granular code + an actionable message ──────────────────


@pytest.mark.parametrize(
    "status,reason,code,expect_in",
    [
        (
            0,
            "Could not reach GitHub: ConnectTimeout",
            "github_unreachable",
            "ConnectTimeout",
        ),
        (401, "Bad credentials", "github_token_invalid", "Bad credentials"),
        (403, "API rate limit exceeded", "github_rate_limited", "rate limit"),
        (
            403,
            "Resource not accessible by personal access token",
            "github_no_permission",
            "selected repositories",
        ),
        (404, "Not Found", "github_no_permission", "selected repositories"),
        (410, "Issues are disabled", "github_issues_disabled", "turned off"),
        (422, "Validation Failed", "github_rejected", "Validation Failed"),
        (500, "Server Error", "github_server_error", "GitHub itself failed"),
    ],
)
def test_filing_failure_maps_to_a_granular_code(
    monkeypatch, session, user_a, status, reason, code, expect_in
):
    """Every filing failure stays HTTP 502 (a 401 would sign the browser out over a
    stale GitHub token), so the CODE is what distinguishes them — and GitHub's own words
    survive in the message."""
    from app.errors import ApiError

    dev_svc.add_token(session, user_a.id, "github_pat_X", ["org"], "octocat")
    draft = _make_draft(session, user_a)

    async def failing_create(pat, owner, repo, title, body):
        raise gh.GithubError(status, reason)

    monkeypatch.setattr(dev_svc.github, "create_issue", failing_create)

    with pytest.raises(ApiError) as ei:
        run(dev_svc.file_draft(session, user_a.id, draft.id))
    assert ei.value.status_code == 502
    assert ei.value.detail["code"] == code
    assert expect_in in ei.value.detail["message"]


def test_permission_message_names_the_step_s_own_permission(
    monkeypatch, session, user_a
):
    """The remedy differs per step: creating an issue wants Issues:write, the project
    attach wants Projects:write. One blanket "GitHub write failed" loses exactly that."""
    from app.errors import ApiError

    dev_svc.add_token(session, user_a.id, "github_pat_X", ["org"], "octocat")
    draft = _make_draft(session, user_a)

    async def ok_create(pat, owner, repo, title, body):
        return {"number": 9, "url": "https://github.com/org/x/issues/9", "node_id": "N"}

    async def denied_attach(pat, project_node_id, issue_node_id):
        raise gh.GithubError(422, "Your token has not been granted the required scopes")

    monkeypatch.setattr(dev_svc.github, "create_issue", ok_create)
    monkeypatch.setattr(dev_svc.github, "add_issue_to_project", denied_attach)

    with pytest.raises(ApiError) as ei:
        run(dev_svc.file_draft(session, user_a.id, draft.id))
    msg = ei.value.detail["message"]
    assert ei.value.detail["code"] == "github_no_permission"
    assert "Projects: Read and write" in msg  # not Issues — this was the attach step
    assert "org/kaapi-backend" in msg  # and it names the repo to go check


def test_file_endpoint_returns_the_reason_in_the_error_envelope(
    monkeypatch, auth, session, user_a
):
    """The browser's only view of the failure is `error.message` — assert it arrives
    there, since a bare 502 in the access log is what made this invisible before."""
    _enable_dev(session, user_a)
    dev_svc.add_token(session, user_a.id, "github_pat_X", ["org"], "octocat")
    draft = _make_draft(session, user_a)

    async def failing_create(pat, owner, repo, title, body):
        raise gh.GithubError(403, "Resource not accessible by personal access token")

    monkeypatch.setattr(dev_svc.github, "create_issue", failing_create)

    resp = auth.as_user(user_a).post(f"/dev/{draft.id}/file")
    assert resp.status_code == 502
    err = resp.json()["error"]
    assert err["code"] == "github_no_permission"
    assert "Issues: Read and write" in err["message"]
