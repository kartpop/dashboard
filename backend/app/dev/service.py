"""Dev-view orchestration — deterministic dispose around the synthesiser LLM (goal 12).

This is the dev counterpart to `news.service` and `router.service`: the LLM (`synth`)
*proposes* de-duplicated issue drafts, this module *disposes* — it resolves the source
Docs from config, reads + parses only the not-yet-processed entries (per-doc cursor),
gathers the whole day into one batch, validates every proposed repo against the
configured catalog, stores drafts verbatim, and advances the cursor **only after drafts
persist**. It also owns config (the Fernet-encrypted PAT, repo catalog, project
defaults, source selections) and the GitHub filing on human approve.

The synthesiser has no GitHub access and files nothing; only `file_draft` — reached
solely from the human-approve endpoint — writes to GitHub, and it records partial state
(the issue number the moment creation succeeds) so a retry never double-creates. Every
query is scoped by `user_id` (goal 8).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from app.dev import config, github, synth
from app.dev.models import (
    DISMISSED,
    DRAFT,
    FILED,
    DevConfig,
    DevDocCursor,
    DevIssueDraft,
    DevPat,
)
from app.dev.parser import Entry, parse_entries
from app.errors import ApiError
from app.google import auth as google_auth
from app.google import docs as docs_client
from app.settings import notes_index
from app.settings import service as settings_svc

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

    from app.auth.models import User

_log = logging.getLogger("dev.service")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Config accessors ──────────────────────────────────────────────────────────


def get_or_create_config(session: Session, user_id: int) -> DevConfig:
    row = session.get(DevConfig, user_id)
    if row is None:
        row = DevConfig(user_id=user_id)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


# ── GitHub tokens (one per resource owner) ────────────────────────────────────
#
# A fine-grained PAT is bound to a single GitHub resource owner at mint time, so filing
# into both a personal account and an org needs one token each. Tokens live in `dev_pat`
# keyed by owner; the owner(s) a token covers are derived from the repos it can see
# (`add_token` is called with them), never hand-typed. Filing routes by the target
# repo's owner (`get_pat_for_owner`).


def list_tokens(session: Session, user_id: int) -> list[DevPat]:
    """All stored tokens for the user, ordered by owner (config display + routing)."""
    return session.exec(
        select(DevPat).where(DevPat.user_id == user_id).order_by(DevPat.owner)
    ).all()


def token_summaries(session: Session, user_id: int) -> list[dict]:
    """Masked token rows for the config UI — owner + a masked hint + the login that
    created it. Never the token itself (write-only)."""
    return [
        {"owner": t.owner, "hint": "••••••••", "login": t.login}
        for t in list_tokens(session, user_id)
    ]


def has_any_token(session: Session, user_id: int) -> bool:
    return (
        session.exec(select(DevPat).where(DevPat.user_id == user_id)).first()
        is not None
    )


def _get_token_row(session: Session, user_id: int, owner: str) -> DevPat | None:
    return session.exec(
        select(DevPat).where(DevPat.user_id == user_id).where(DevPat.owner == owner)
    ).first()


def get_pat_for_owner(session: Session, user_id: int, owner: str) -> str | None:
    """Decrypt the token that files under `owner`, or None if none is stored. The
    plaintext PAT never leaves this process except in an outbound GitHub Authorization
    header."""
    row = _get_token_row(session, user_id, owner)
    if row is None:
        return None
    try:
        return google_auth.decrypt_token(row.pat_encrypted)
    except Exception:
        _log.exception(
            "dev: failed to decrypt PAT for user %s owner %s", user_id, owner
        )
        return None


def add_token(
    session: Session, user_id: int, pat: str, owners: list[str], login: str | None
) -> None:
    """Store one Fernet-encrypted token per owner it covers (upsert per owner). Validation
    + owner derivation are the caller's job (the router pings GitHub and lists the token's
    repos); this only persists."""
    enc = google_auth.encrypt_token(pat)
    for owner in owners:
        o = (owner or "").strip()
        if not o:
            continue
        row = _get_token_row(session, user_id, o)
        if row is None:
            row = DevPat(user_id=user_id, owner=o)
        row.pat_encrypted = enc
        row.login = login
        row.updated_at = _now()
        session.add(row)
    session.commit()


def remove_token(session: Session, user_id: int, owner: str) -> None:
    """Drop the token for a resource owner (drafts targeting it can no longer be filed
    until a token is re-added). Local delete only — nothing is revoked on GitHub."""
    row = _get_token_row(session, user_id, owner)
    if row is not None:
        session.delete(row)
        session.commit()


def distinct_token_pats(session: Session, user_id: int) -> list[str]:
    """The unique decrypted tokens across all owners — for the repo-refresh union (one
    token can, for a classic PAT, cover several owners; dedupe so it lists once)."""
    seen: set[str] = set()
    out: list[str] = []
    for row in list_tokens(session, user_id):
        try:
            pat = google_auth.decrypt_token(row.pat_encrypted)
        except Exception:
            continue
        if pat not in seen:
            seen.add(pat)
            out.append(pat)
    return out


def get_sources(row: DevConfig) -> list[str]:
    try:
        ids = json.loads(row.sources_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(i) for i in ids if isinstance(i, str) and i.strip()]


def set_sources(session: Session, user_id: int, node_ids: list[str]) -> DevConfig:
    seen: list[str] = []
    for nid in node_ids:
        n = (nid or "").strip()
        if n and n not in seen:
            seen.append(n)
    row = get_or_create_config(session, user_id)
    row.sources_json = json.dumps(seen)
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_repos(row: DevConfig) -> list[dict]:
    try:
        repos = json.loads(row.repos_json or "[]")
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for r in repos:
        if isinstance(r, dict) and r.get("full_name"):
            out.append(
                {
                    "full_name": r["full_name"],
                    "description": (r.get("description") or "").strip(),
                    "is_default": bool(r.get("is_default")),
                }
            )
    return out


def default_repo(row: DevConfig) -> str | None:
    repos = get_repos(row)
    for r in repos:
        if r["is_default"]:
            return r["full_name"]
    return repos[0]["full_name"] if repos else None


def set_repos(session: Session, user_id: int, repos: list[dict]) -> DevConfig:
    """Persist the selected repo catalog. Exactly one is marked default (the first
    flagged, else the first repo); descriptions are kept verbatim (they feed the LLM)."""
    cleaned: list[dict] = []
    default_seen = False
    for r in repos:
        full = (r.get("full_name") or "").strip()
        if not full:
            continue
        is_def = bool(r.get("is_default")) and not default_seen
        if is_def:
            default_seen = True
        cleaned.append(
            {
                "full_name": full,
                "description": (r.get("description") or "").strip(),
                "is_default": is_def,
            }
        )
    if cleaned and not default_seen:
        cleaned[0]["is_default"] = True
    row = get_or_create_config(session, user_id)
    row.repos_json = json.dumps(cleaned)
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_projects(row: DevConfig) -> dict:
    try:
        projects = json.loads(row.projects_json or "{}")
    except json.JSONDecodeError:
        return {}
    out: dict = {}
    if isinstance(projects, dict):
        for repo, proj in projects.items():
            if isinstance(proj, dict) and proj.get("node_id"):
                out[repo] = {
                    "node_id": proj["node_id"],
                    "title": proj.get("title") or "",
                }
    return out


def set_projects(session: Session, user_id: int, projects: dict) -> DevConfig:
    row = get_or_create_config(session, user_id)
    row.projects_json = json.dumps(get_projects_from_incoming(projects))
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_projects_from_incoming(projects: dict) -> dict:
    out: dict = {}
    if isinstance(projects, dict):
        for repo, proj in projects.items():
            if isinstance(proj, dict) and proj.get("node_id"):
                out[str(repo)] = {
                    "node_id": str(proj["node_id"]),
                    "title": str(proj.get("title") or ""),
                }
    return out


def is_config_complete(session: Session, row: DevConfig) -> bool:
    """The cost gate for the scan: ≥1 GitHub token AND ≥1 source AND ≥1 target repo.

    Presence (not a live network ping every scheduler tick) is the cheap check — the
    `dev` flag is the real cost switch, and the scan skips an incomplete config the same
    way it skips an unflagged user."""
    return (
        has_any_token(session, row.user_id)
        and bool(get_sources(row))
        and bool(get_repos(row))
    )


# ── Source resolution ─────────────────────────────────────────────────────────


def resolve_source_docs(session: Session, user_id: int) -> list[dict]:
    """Resolve the config's selected node ids → concrete `[{drive_id, path}]` Doc leaves
    (folders expand recursively), against the LIVE hierarchy index — so a Doc added under
    a selected folder after selection is picked up automatically. Ids always come from
    the stored index, never from LLM output."""
    row = get_or_create_config(session, user_id)
    forest = settings_svc.get_notes_index(session, user_id)
    return notes_index.selected_doc_leaves(forest, set(get_sources(row)))


# ── The scan (cursor-scoped read → batch synth → dispose → advance) ───────────


def _get_cursor(session: Session, user_id: int, doc_id: str) -> DevDocCursor | None:
    return session.exec(
        select(DevDocCursor)
        .where(DevDocCursor.user_id == user_id)
        .where(DevDocCursor.doc_id == doc_id)
    ).first()


def _new_entries(cursor: DevDocCursor | None, entries: list[Entry]) -> list[Entry]:
    """Entries strictly newer than the cursor, PLUS same-minute entries not already in
    the boundary keys (so a same-minute entry captured after a scan is caught once)."""
    if cursor is None or cursor.last_processed_entry_ts is None:
        return list(entries)
    last = cursor.last_processed_entry_ts
    try:
        boundary = set(json.loads(cursor.boundary_entry_keys or "[]"))
    except json.JSONDecodeError:
        boundary = set()
    kept: list[Entry] = []
    for e in entries:
        if e.ts > last:
            kept.append(e)
        elif e.ts == last and e.key not in boundary:
            kept.append(e)
    return kept


def _advance_cursor(
    session: Session, user_id: int, doc_id: str, entries: list[Entry]
) -> None:
    """Move the per-doc cursor to the newest entry timestamp present, recording every
    entry key at exactly that minute as the boundary. Idempotent: a doc with no new
    entries re-writes the same values. Called only AFTER drafts persist."""
    if not entries:
        return
    newest = max(e.ts for e in entries)
    boundary = [e.key for e in entries if e.ts == newest]
    cursor = _get_cursor(session, user_id, doc_id)
    if cursor is None:
        cursor = DevDocCursor(user_id=user_id, doc_id=doc_id)
    cursor.last_processed_entry_ts = newest
    cursor.boundary_entry_keys = json.dumps(boundary)
    cursor.updated_at = _now()
    session.add(cursor)
    session.commit()


def _do_not_redraft_titles(session: Session, user_id: int) -> list[str]:
    """Titles of still-open drafts + filed issues — do-not-redraft context for the LLM
    (dedup across scans, newest first, capped)."""
    rows = session.exec(
        select(DevIssueDraft)
        .where(DevIssueDraft.user_id == user_id)
        .where(DevIssueDraft.status != DISMISSED)
        .order_by(DevIssueDraft.created_at.desc())
    ).all()
    titles = [r.title for r in rows if r.title]
    return titles[: config.DO_NOT_REDRAFT_LIMIT]


def _dispose_synthesis(
    session: Session,
    user_id: int,
    result,
    doc_id_by_path: dict[str, str],
    default: str | None,
    catalog_names: set[str],
) -> int:
    """Persist the LLM's proposed issues as drafts (code-disposes the proposal).

    - A proposed `repo` not in the configured catalog falls back to the default repo.
    - `sources` map back to `{doc_id, doc_path, entry_ts}` provenance rows via the
      path→doc_id map the batch was built from (an unknown path keeps just its path).
    - The repo's configured default project is preselected on the draft.
    Returns the number of drafts stored."""
    projects = get_projects(get_or_create_config(session, user_id))
    stored = 0
    for issue in result.issues:
        title = (issue.title or "").strip()
        if not title:
            continue
        repo = issue.repo if issue.repo in catalog_names else (default or "")
        sources = [
            {
                "doc_id": doc_id_by_path.get(s.doc_path),
                "doc_path": s.doc_path,
                "entry_ts": s.entry_ts,
            }
            for s in issue.sources
        ]
        proj = projects.get(repo) or {}
        draft = DevIssueDraft(
            user_id=user_id,
            title=title,
            body=issue.body_markdown or "",
            repo=repo,
            status=DRAFT,
            sources=json.dumps(sources),
            project_node_id=proj.get("node_id"),
            project_title=proj.get("title"),
        )
        session.add(draft)
        stored += 1
    session.commit()
    return stored


async def run_scan(session: Session, user: "User", creds: "Credentials") -> dict:
    """Read the new entries across all resolved source Docs, synthesise them into
    de-duplicated drafts in ONE LLM call, persist, then advance the cursors.

    Order is deliberate: drafts persist BEFORE any cursor advances, so a crash between
    the LLM and the store re-scans the same entries (no entry lost, at worst a re-draft
    the do-not-redraft context suppresses). A doc that fails to read is skipped, not
    fatal. Returns a tally."""
    cfg = get_or_create_config(session, user.id)
    sources = resolve_source_docs(session, user.id)

    # Gather the cursor-new entries across every source Doc into one batch.
    batch: list[dict] = []
    per_doc: dict[str, list[Entry]] = {}
    doc_id_by_path: dict[str, str] = {}
    docs_read = 0
    for src in sources:
        doc_id, path = src["drive_id"], src["path"]
        doc_id_by_path[path] = doc_id
        try:
            document = await docs_client.get_document(creds, doc_id)
        except Exception:
            _log.exception("dev scan: failed to read doc %s (user %s)", doc_id, user.id)
            continue
        docs_read += 1
        entries = parse_entries(document)
        per_doc[doc_id] = entries
        cursor = _get_cursor(session, user.id, doc_id)
        for e in _new_entries(cursor, entries):
            batch.append(
                {
                    "doc_id": doc_id,
                    "doc_path": path,
                    "entry_ts": e.ts.isoformat(),
                    "one_liner": e.one_liner,
                    "keywords": e.keywords,
                    "body": e.body,
                }
            )

    stored = 0
    synth_failed = False
    if batch:
        catalog = get_repos(cfg)
        catalog_names = {r["full_name"] for r in catalog}
        result = await synth.synthesise(
            batch,
            [
                {"full_name": r["full_name"], "description": r["description"]}
                for r in catalog
            ],
            _do_not_redraft_titles(session, user.id),
        )
        if result is None:
            # Synthesis failed (errored or truncated at max_tokens). Do NOT advance any
            # cursor — leaving them put means these entries are re-scanned next run rather
            # than silently consumed. A returned result (even empty) is a real answer.
            synth_failed = True
            _log.warning(
                "dev scan: synthesis failed for user %s — cursors left unadvanced, "
                "%d new entries will re-scan",
                user.id,
                len(batch),
            )
        else:
            stored = _dispose_synthesis(
                session,
                user.id,
                result,
                doc_id_by_path,
                default_repo(cfg),
                catalog_names,
            )

    # Advance every read doc's cursor AFTER drafts persisted (idempotent for no-delta docs).
    # Skipped entirely on a synthesis failure so the batch is retried, not lost.
    if not synth_failed:
        for doc_id, entries in per_doc.items():
            _advance_cursor(session, user.id, doc_id, entries)

    cfg.last_scan_at = _now()
    session.add(cfg)
    session.commit()

    return {
        "docs_read": docs_read,
        "new_entries": len(batch),
        "drafts_created": stored,
    }


# ── Drafts view + edits ───────────────────────────────────────────────────────


def list_drafts(session: Session, user_id: int) -> list[DevIssueDraft]:
    """All drafts for the user — pending (draft) first, then filed/dismissed, newest
    first within each group."""
    order = {DRAFT: 0, FILED: 1, DISMISSED: 2}
    rows = session.exec(
        select(DevIssueDraft).where(DevIssueDraft.user_id == user_id)
    ).all()
    return sorted(rows, key=lambda d: (order.get(d.status, 3), -(d.id or 0)))


def _owned_draft(session: Session, user_id: int, draft_id: int) -> DevIssueDraft:
    draft = session.get(DevIssueDraft, draft_id)
    if draft is None or draft.user_id != user_id:
        raise ApiError(404, "draft_not_found", "No draft with that id.")
    return draft


def update_draft(
    session: Session,
    user_id: int,
    draft_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    repo: str | None = None,
    project_node_id: str | None = None,
    project_title: str | None = None,
) -> DevIssueDraft:
    """Persist an inline edit (title/body/repo/project). A filed draft is frozen — its
    text already lives on GitHub, so edits only apply while status is `draft`."""
    draft = _owned_draft(session, user_id, draft_id)
    if draft.status != DRAFT:
        raise ApiError(409, "draft_not_editable", "Only a pending draft can be edited.")
    if title is not None:
        draft.title = title.strip()
    if body is not None:
        draft.body = body
    if repo is not None:
        draft.repo = repo.strip()
    if project_node_id is not None:
        draft.project_node_id = project_node_id or None
        draft.project_title = (project_title or "").strip() or None
    draft.updated_at = _now()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


def dismiss_draft(session: Session, user_id: int, draft_id: int) -> DevIssueDraft:
    """Decline a draft — a local status flip, zero GitHub calls."""
    draft = _owned_draft(session, user_id, draft_id)
    if draft.status == FILED:
        raise ApiError(409, "already_filed", "A filed draft cannot be dismissed.")
    draft.status = DISMISSED
    draft.updated_at = _now()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


# ── GitHub filing (the human-approved write; partial-state idempotent) ────────


async def file_draft(session: Session, user_id: int, draft_id: int) -> DevIssueDraft:
    """Create the issue on GitHub and attach it to the chosen project (human-approved).

    Idempotent with partial-state recording: the issue number/url is stored the moment
    creation succeeds, so if the project-attach fails the retry re-runs ONLY the GraphQL
    step — an issue is never double-created. Filing needs a valid PAT; a GitHub failure
    surfaces as an `ApiError` (rollback-not-blind-retry)."""
    draft = _owned_draft(session, user_id, draft_id)
    if draft.status == DISMISSED:
        raise ApiError(409, "draft_dismissed", "A dismissed draft cannot be filed.")

    if "/" not in (draft.repo or ""):
        raise ApiError(400, "no_repo", "This draft has no valid target repo.")
    owner, repo_name = draft.repo.split("/", 1)

    # Route to the token that files under this repo's owner (a fine-grained PAT is
    # scoped to one owner — personal vs org need separate tokens).
    pat = get_pat_for_owner(session, user_id, owner)
    if not pat:
        raise ApiError(
            400,
            "no_token_for_owner",
            f"No GitHub token stored for '{owner}'. Add one in the Dev config first.",
        )

    # Step 1 — create the issue (skipped if a prior attempt already created it).
    if not draft.issue_url:
        try:
            created = await github.create_issue(
                pat, owner, repo_name, draft.title, draft.body
            )
        except github.GithubError as exc:
            raise ApiError(502, "github_create_failed", exc.message) from exc
        draft.issue_number = created["number"]
        draft.issue_url = created["url"]
        draft.issue_node_id = created["node_id"]
        draft.status = FILED
        draft.updated_at = _now()
        session.add(draft)
        session.commit()  # partial state persisted — retry never re-creates
        session.refresh(draft)

    # Step 2 — attach to the project (only step a retry re-runs).
    if draft.project_node_id and not draft.project_attached and draft.issue_node_id:
        try:
            await github.add_issue_to_project(
                pat, draft.project_node_id, draft.issue_node_id
            )
        except github.GithubError as exc:
            raise ApiError(502, "github_project_attach_failed", exc.message) from exc
        draft.project_attached = True
        draft.updated_at = _now()
        session.add(draft)
        session.commit()
        session.refresh(draft)

    return draft
