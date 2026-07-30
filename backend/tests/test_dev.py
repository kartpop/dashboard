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

import pytest
from sqlmodel import select

from app.auth.models import AllowedEmail
from app.dev import service as dev_svc
from app.dev.models import DRAFT, FILED, DevDocCursor, DevIssueDraft
from app.dev.schema import ProposedIssue, SourceRef, SynthesisResult
from app.settings import notes_index
from app.settings import service as settings_svc
from tests.conftest import DummyCreds


def run(coro):
    return asyncio.run(coro)


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

    drafts = dev_svc.list_drafts(session, user_a.id)
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
    draft = dev_svc.list_drafts(session, user_a.id)[0]
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
    assert dev_svc.list_drafts(session, user_a.id) == []


# ── Filing ────────────────────────────────────────────────────────────────────


def _make_draft(session, user, **over) -> DevIssueDraft:
    d = DevIssueDraft(
        user_id=user.id,
        title="Fix bug",
        body="body",
        repo=over.pop("repo", "org/kaapi-backend"),
        status=DRAFT,
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
