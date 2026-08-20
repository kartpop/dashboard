"""Dev-view HTTP surface (goal 12; tabs + pagination in 12a).

View metadata (`GET /dev`: last scan, config completeness, per-tab counts), one paged
tab of drafts at a time (`GET /dev/drafts`), inline edits, approve-and-file, the three
local status flips (dismiss / save / unsave), a manual scan-now, and the in-view config
(PAT + repos + projects + source Docs). Every endpoint is gated by `require_dev_enabled`
(403 for non-enabled users, so the whole resource is invisible to them) and scoped to
`current_user`. Only `scan-now` takes Google creds (the Docs read); the filing path takes
none (it uses the stored PAT).

LLM-proposes / code-disposes: nothing here lets the model touch GitHub — a GitHub write
happens only on the explicit `/{id}/file` approve, through `service.file_draft`. Save,
unsave and dismiss are local status flips and make no GitHub call at all.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends
from google.oauth2.credentials import Credentials
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.auth.deps import get_current_credentials, get_current_user
from app.auth.models import User
from app.db import get_session
from app.dev import gating, github
from app.dev import service as dev_svc
from app.dev.models import DevIssueDraft
from app.errors import ApiError
from app.settings import service as settings_svc

router = APIRouter(prefix="/dev", tags=["dev"])


def require_dev_enabled(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> User:
    """Gate every dev endpoint behind the per-user Dev flag (goal 12). A non-enabled
    user gets 403 on every /dev endpoint and no rail entry (the frontend reads
    `dev_enabled` from /auth/me). The scheduled scan is gated the same way."""
    if not gating.is_dev_enabled(session, user):
        raise ApiError(403, "dev_not_enabled", "Dev is not enabled for your account.")
    return user


# ── Serializers ───────────────────────────────────────────────────────────────


def _draft_out(draft: DevIssueDraft) -> dict:
    try:
        sources = json.loads(draft.sources or "[]")
    except json.JSONDecodeError:
        sources = []
    # related_issues is nullable by design: null = not yet matched, [] = matched with
    # nothing found. The card renders the Similar line from this (validated matches
    # only — every url/title here came from the fetched candidate list, never the LLM).
    related = None
    if draft.related_issues is not None:
        try:
            related = json.loads(draft.related_issues)
        except json.JSONDecodeError:
            related = None
    return {
        "id": draft.id,
        "title": draft.title,
        "body": draft.body,
        "repo": draft.repo,
        "status": draft.status,
        "kind": draft.kind,
        "target_issue_number": draft.target_issue_number,
        "target_issue_url": draft.target_issue_url,
        "related_issues": related,
        "sources": [
            {"doc_path": s.get("doc_path"), "entry_ts": s.get("entry_ts")}
            for s in sources
            if isinstance(s, dict)
        ],
        "project_node_id": draft.project_node_id,
        "project_title": draft.project_title,
        "issue_url": draft.issue_url,
        "issue_number": draft.issue_number,
        "project_attached": draft.project_attached,
        "created_at": draft.created_at.isoformat(),
    }


def _tree_out(node: dict) -> dict:
    """Minimal source-picker node — no drive ids/urls (the picker selects by node_id)."""
    return {
        "node_id": node.get("node_id"),
        "name": node.get("name"),
        "kind": node.get("kind"),
        "children": [_tree_out(c) for c in (node.get("children") or [])],
    }


# ── Drafts ────────────────────────────────────────────────────────────────────


@router.get("")
async def get_view_meta(
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Dev-view metadata only (goal 12a): the last-scan time, whether config is complete
    (drives the empty-state hint), and the per-tab draft counts that feed the tab badges.
    The drafts themselves come one page at a time from `/dev/drafts` — this endpoint no
    longer carries the (unbounded) draft array."""
    cfg = dev_svc.get_or_create_config(session, user.id)
    return {
        "last_scan_at": cfg.last_scan_at.isoformat() if cfg.last_scan_at else None,
        "config_complete": dev_svc.is_config_complete(session, cfg),
        "counts": dev_svc.draft_counts(session, user.id),
    }


@router.get("/drafts")
async def get_drafts(
    status: str = "review",
    limit: int = dev_svc.DEFAULT_PAGE_LIMIT,
    cursor: Optional[str] = None,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """One tab's page of drafts, newest activity first (goal 12a).

    `status` is a tab name (`review|saved|filed|dismissed`; `review` = pending drafts).
    `next_cursor` is an opaque keyset token over `(updated_at, id)` — follow it for the
    next page. `limit` is clamped server-side, so the payload stays bounded however the
    settled lanes pile up."""
    if status not in dev_svc.TAB_STATUS:
        raise ApiError(
            400,
            "bad_status",
            f"status must be one of {', '.join(dev_svc.TAB_STATUS)}.",
        )
    items, next_cursor = dev_svc.list_drafts(
        session,
        user.id,
        status=dev_svc.TAB_STATUS[status],
        limit=limit,
        cursor=cursor,
    )
    return {"items": [_draft_out(d) for d in items], "next_cursor": next_cursor}


@router.post("/scan-now")
async def scan_now(
    user: User = Depends(require_dev_enabled),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    """Run the notes → drafts scan now (the manual trigger; same code path as the
    scheduled daily job). The after-a-meeting affordance."""
    cfg = dev_svc.get_or_create_config(session, user.id)
    if not dev_svc.is_config_complete(session, cfg):
        raise ApiError(
            400,
            "config_incomplete",
            "Add a GitHub token, at least one source Doc, and a target repo first.",
        )
    tally = await dev_svc.run_scan(session, user, creds)
    return {"tally": tally}


class DraftUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    repo: Optional[str] = None
    project_node_id: Optional[str] = None
    project_title: Optional[str] = None


@router.patch("/{draft_id}")
async def patch_draft(
    draft_id: int,
    body: DraftUpdate,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Persist an inline edit (title/body/repo/project) on a pending draft."""
    draft = dev_svc.update_draft(
        session,
        user.id,
        draft_id,
        title=body.title,
        body=body.body,
        repo=body.repo,
        project_node_id=body.project_node_id,
        project_title=body.project_title,
    )
    return _draft_out(draft)


@router.post("/{draft_id}/file")
async def file_draft(
    draft_id: int,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Approve & file — the only path to a GitHub write. Creates the issue and attaches
    it to the chosen project; partial-state idempotent (retry re-runs only the attach)."""
    draft = await dev_svc.file_draft(session, user.id, draft_id)
    return _draft_out(draft)


@router.post("/{draft_id}/dismiss")
async def dismiss_draft(
    draft_id: int,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Decline a draft — a local status flip, zero GitHub calls."""
    draft = dev_svc.dismiss_draft(session, user.id, draft_id)
    return _draft_out(draft)


@router.post("/{draft_id}/save")
async def save_draft(
    draft_id: int,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Save for later (goal 12a) — shelve a real-but-not-now draft. Like dismiss, a
    local status flip with zero GitHub calls; filing stays the only GitHub write."""
    draft = dev_svc.save_draft(session, user.id, draft_id)
    return _draft_out(draft)


@router.post("/{draft_id}/unsave")
async def unsave_draft(
    draft_id: int,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Move back to review — the escape hatch out of the saved shelf (and out of the
    dismissed lane). A local status flip, zero GitHub calls."""
    draft = dev_svc.unsave_draft(session, user.id, draft_id)
    return _draft_out(draft)


# ── Config ────────────────────────────────────────────────────────────────────


@router.get("/config")
async def get_config(
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """The stored config: the GitHub tokens (masked, one per resource owner), source
    selections + the notes tree to pick from, the repo catalog, and the per-repo default
    projects. No GitHub network call — the live repo/project lists come from `refresh` /
    `projects` on demand."""
    cfg = dev_svc.get_or_create_config(session, user.id)
    forest = settings_svc.get_notes_index(session, user.id)
    return {
        "tokens": dev_svc.token_summaries(session, user.id),
        "sources": dev_svc.get_sources(cfg),
        "notes_tree": [_tree_out(n) for n in forest],
        "repos": dev_svc.get_repos(cfg),
        "projects": dev_svc.get_projects(cfg),
        "last_scan_at": cfg.last_scan_at.isoformat() if cfg.last_scan_at else None,
        "config_complete": dev_svc.is_config_complete(session, cfg),
    }


class PatUpdate(BaseModel):
    pat: str


def _owners_from_repos(repos: list[dict]) -> list[str]:
    """The distinct resource owners a token covers, derived from the repos it can see
    (`owner/name` → `owner`). A fine-grained PAT is scoped to one owner, so this is
    normally a single entry; a classic token can span several."""
    owners = {
        (r.get("full_name") or "").split("/", 1)[0]
        for r in repos
        if r.get("full_name") and "/" in r["full_name"]
    }
    return sorted(o for o in owners if o)


@router.post("/config/tokens")
async def add_token(
    body: PatUpdate,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Add (or replace) a GitHub token (write-only). Validated with a viewer ping, then
    the repos it can see are listed to derive its resource owner(s) — the token is stored
    (encrypted) under each. A token that can reach no repos is rejected (its owner can't
    be inferred and it could file nothing). The response carries only the owners it now
    covers + the login — never the token."""
    pat = (body.pat or "").strip()
    if not pat:
        raise ApiError(400, "empty_pat", "Paste a GitHub personal access token.")
    try:
        who = await github.validate_pat(pat)
        repos = await github.list_repos(pat)
    except github.GithubError as exc:
        raise ApiError(
            400, "invalid_pat", f"GitHub rejected the token: {exc.message}"
        ) from exc
    owners = _owners_from_repos(repos)
    if not owners:
        raise ApiError(
            400,
            "token_no_repos",
            "This token can't see any repositories — grant it access to at least one "
            "repo (its resource owner is inferred from the repos it can reach).",
        )
    dev_svc.add_token(session, user.id, pat, owners, who.get("login"))
    return {"owners": owners, "login": who.get("login")}


@router.delete("/config/tokens/{owner}")
async def delete_token(
    owner: str,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Remove the token for a resource owner (local only — nothing is revoked on GitHub).
    Drafts targeting that owner can't be filed until a token is re-added."""
    dev_svc.remove_token(session, user.id, owner)
    return {"tokens": dev_svc.token_summaries(session, user.id)}


class SourcesUpdate(BaseModel):
    node_ids: list[str] = Field(default_factory=list)


@router.put("/config/sources")
async def put_sources(
    body: SourcesUpdate,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    cfg = dev_svc.set_sources(session, user.id, body.node_ids)
    return {"sources": dev_svc.get_sources(cfg)}


class RepoIn(BaseModel):
    full_name: str
    description: str = ""
    is_default: bool = False


class ReposUpdate(BaseModel):
    repos: list[RepoIn] = Field(default_factory=list)


@router.put("/config/repos")
async def put_repos(
    body: ReposUpdate,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    cfg = dev_svc.set_repos(session, user.id, [r.model_dump() for r in body.repos])
    return {"repos": dev_svc.get_repos(cfg)}


class ProjectsUpdate(BaseModel):
    # {repo_full_name: {node_id, title}}
    projects: dict = Field(default_factory=dict)


@router.put("/config/projects")
async def put_projects(
    body: ProjectsUpdate,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    cfg = dev_svc.set_projects(session, user.id, body.projects)
    return {"projects": dev_svc.get_projects(cfg)}


@router.post("/config/refresh")
async def refresh_github(
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """Re-pull the repos across ALL stored tokens (the tickable set is the union — one
    owner's token contributes that owner's repos). Newly granted repos appear without
    re-entering a token. Best-effort: a single failing token is skipped rather than
    blocking the others; if every token fails, surface the error."""
    pats = dev_svc.distinct_token_pats(session, user.id)
    if not pats:
        raise ApiError(400, "no_token", "Add a GitHub token first.")
    by_name: dict[str, dict] = {}
    last_error: github.GithubError | None = None
    ok = False
    for pat in pats:
        try:
            for r in await github.list_repos(pat):
                if r.get("full_name"):
                    by_name[r["full_name"]] = r
            ok = True
        except github.GithubError as exc:
            last_error = exc
    if not ok and last_error is not None:
        raise ApiError(502, "github_unavailable", last_error.message) from last_error
    repos = sorted(by_name.values(), key=lambda r: (r["full_name"] or "").lower())
    return {"repos": repos}


@router.get("/config/members")
async def list_members(
    repo: str,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """The repo's assignable users (`list_assignees`) — the @-mention typeahead's
    source (goal 12b). Same per-owner token routing as `GET /config/projects`. A PAT
    that can't list assignees degrades to an EMPTY list, not an error: the typeahead
    just offers nothing, and typing `@login` by hand still works (it's plain text).
    Member logins are never fed to any LLM — this list exists for the human's editor
    only."""
    if "/" not in repo:
        raise ApiError(400, "bad_repo", "Repo must be 'owner/name'.")
    owner, name = repo.split("/", 1)
    pat = dev_svc.get_pat_for_owner(session, user.id, owner)
    if not pat:
        return {"members": []}
    try:
        members = await github.list_assignees(pat, owner, name)
    except github.GithubError:
        members = []
    return {"members": members}


@router.get("/config/projects")
async def list_projects(
    repo: str,
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """The ProjectsV2 projects linked to `repo` (owner/name), fetched via the API — the
    per-repo default picker and the per-card project dropdown both read this. Uses the
    token that files under the repo's owner."""
    if "/" not in repo:
        raise ApiError(400, "bad_repo", "Repo must be 'owner/name'.")
    owner, name = repo.split("/", 1)
    pat = dev_svc.get_pat_for_owner(session, user.id, owner)
    if not pat:
        raise ApiError(
            400, "no_token_for_owner", f"No GitHub token stored for '{owner}'."
        )
    try:
        projects = await github.list_projects_for_repo(pat, owner, name)
    except github.GithubError as exc:
        raise ApiError(502, "github_unavailable", exc.message) from exc
    return {"projects": projects}
