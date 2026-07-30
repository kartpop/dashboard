"""Dev-view HTTP surface (goal 12).

The issue-draft list, inline edits, approve-and-file, dismiss, a manual scan-now, and
the in-view config (PAT + repos + projects + source Docs). Every endpoint is gated by
`require_dev_enabled` (403 for non-enabled users, so the whole resource is invisible to
them) and scoped to `current_user`. Only `scan-now` takes Google creds (the Docs read);
the filing path takes none (it uses the stored PAT).

LLM-proposes / code-disposes: nothing here lets the model touch GitHub — a GitHub write
happens only on the explicit `/{id}/file` approve, through `service.file_draft`.
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
    return {
        "id": draft.id,
        "title": draft.title,
        "body": draft.body,
        "repo": draft.repo,
        "status": draft.status,
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
async def get_drafts(
    user: User = Depends(require_dev_enabled),
    session: Session = Depends(get_session),
):
    """The issue-draft list (pending first, then filed/dismissed) + the last-scan time
    and whether config is complete (drives the empty-state hint)."""
    cfg = dev_svc.get_or_create_config(session, user.id)
    drafts = dev_svc.list_drafts(session, user.id)
    return {
        "drafts": [_draft_out(d) for d in drafts],
        "last_scan_at": cfg.last_scan_at.isoformat() if cfg.last_scan_at else None,
        "config_complete": dev_svc.is_config_complete(session, cfg),
    }


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
