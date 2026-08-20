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

import base64
import binascii
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from app.dev import config, github, synth
from app.dev.models import (
    DISMISSED,
    DRAFT,
    FILED,
    KIND_COMMENT,
    SAVED,
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

    # Match-and-convert tail (goal 12b): dedup the whole unfiled backlog — this scan's
    # newborns plus anything lingering in review/saved — against live GitHub. Strictly
    # best-effort: any failure leaves plain issue drafts (related_issues NULL retries
    # next scan) and never blocks the scan or the cursor, which advanced above.
    match_tally = {"linked": 0, "converted": 0, "matching_skipped": False}
    try:
        match_tally = await _match_and_convert(session, user.id)
    except Exception:
        _log.exception("dev scan: match phase failed for user %s", user.id)
        match_tally["matching_skipped"] = True

    cfg.last_scan_at = _now()
    session.add(cfg)
    session.commit()

    return {
        "docs_read": docs_read,
        "new_entries": len(batch),
        "drafts_created": stored,
        # Distinguishes "the model found nothing new to draft" from "the call failed and
        # the batch is still queued" — both leave drafts_created at 0, but only one of
        # them means the entries were actually considered. The UI says which.
        "synthesis_failed": synth_failed,
        # Goal 12b: drafts newly linked to existing GitHub issues/PRs, drafts converted
        # into comment drafts, and whether any repo's match phase was skipped (reported,
        # not hidden — a skip means related_issues stays NULL and retries next scan).
        "linked": match_tally["linked"],
        "converted": match_tally["converted"],
        "matching_skipped": match_tally["matching_skipped"],
    }


# ── Live-GitHub dedup (goal 12b): match, link, convert ────────────────────────


def _unmatched_drafts(session: Session, user_id: int) -> list[DevIssueDraft]:
    """The matcher's scope: every NON-SETTLED draft (review + saved) whose
    `related_issues` is still NULL — regardless of which scan synthesised it, so the
    first post-deploy scan processes the whole lingering backlog. Dismissed and filed
    drafts are never matched; an already-matched draft (even "[]") is never re-matched
    (the NULL-guard). A flat list: matching is CATALOG-wide (12b.1), not per-repo —
    the draft's own repo tag is the synthesiser's guess and may be wrong."""
    return list(
        session.exec(
            select(DevIssueDraft)
            .where(DevIssueDraft.user_id == user_id)
            .where(DevIssueDraft.status.in_((DRAFT, SAVED)))
            .where(DevIssueDraft.related_issues.is_(None))
            .order_by(DevIssueDraft.id)
        ).all()
    )


def _match_ref(m: dict, draft_repo: str) -> str:
    """One match's textual reference: `#123` inside its own repo, the cross-repo form
    `owner/repo#123` (which GitHub also auto-links) otherwise."""
    same = not m.get("repo") or m.get("repo") == draft_repo
    return f"#{m['number']}" if same else f"{m['repo']}#{m['number']}"


def _related_line(
    matches: list[dict], draft_repo: str, *, exclude: dict | None = None
) -> str:
    """The deterministic `**Related:** #123, PR owner/repo#45 (merged)` body line,
    built from VALIDATED matches only (code, not LLM). GitHub auto-links both the `#N`
    and the `owner/repo#N` forms once filed. `exclude` drops a comment draft's own
    target issue (a reference to #N inside a comment on #N would be noise). Empty
    string when nothing remains."""
    parts: list[str] = []
    for m in matches:
        if m["type"] == "pr":
            parts.append(f"PR {_match_ref(m, draft_repo)} ({m['state']})")
        elif not (
            exclude
            and m["number"] == exclude.get("number")
            and m.get("repo") == exclude.get("repo")
        ):
            parts.append(_match_ref(m, draft_repo))
    return "**Related:** " + ", ".join(parts) if parts else ""


def _validated_matches(
    proposed,
    issue_by_key: dict[tuple[str, int], dict],
    pr_by_key: dict[tuple[str, int], dict],
) -> list[dict]:
    """Dispose the matcher's proposal for one draft: every returned
    `(repo, number, type)` is checked against the fetched candidate set — out-of-set
    entries are dropped — and the stored repo/url/title/type/state come from the
    code-fetched candidate keyed by the validated pair, NEVER from LLM output (house
    rule: ids/URLs code acts on never come from the model). Only the confidence label
    and the one-line reason are the model's."""
    out: list[dict] = []
    for m in proposed:
        key = (m.repo, m.number)
        if m.type == "issue" and key in issue_by_key:
            cand, state = issue_by_key[key], "open"
        elif m.type == "pr" and key in pr_by_key:
            cand = pr_by_key[key]
            state = cand["state"]
        else:
            continue  # out-of-set (wrong repo, bogus number, or mistyped) — dropped
        out.append(
            {
                "repo": cand["repo"],
                "number": cand["number"],
                "type": m.type,
                "state": state,
                "url": cand["html_url"],
                "title": cand["title"],
                "confidence": m.confidence
                if m.confidence in ("high", "medium")
                else "medium",
                "reason": (m.reason or "")[:300],
            }
        )
    return out


async def _match_and_convert(session: Session, user_id: int) -> dict:
    """The post-dispose dedup pass: fetch candidates from EVERY catalog repo (code),
    judge each unmatched draft against all of them (LLM), store validated links + the
    `Related:` body line (code), and convert confirmed-duplicate drafts into comment
    drafts.

    Catalog-wide on purpose (12b.1): the synthesiser sometimes tags the wrong repo
    (out-of-catalog picks fall back to the default), and per-repo matching then judged
    those drafts against a repo whose issues could never match. The candidate fetch is
    all-or-nothing per scan — one repo failing aborts the phase (drafts stay NULL and
    retry next scan) so a draft's true match is never silently missed; a matcher
    failure skips only its CHUNK of drafts."""
    tally = {"linked": 0, "converted": 0, "matching_skipped": False}
    drafts = _unmatched_drafts(session, user_id)
    if not drafts:
        return tally

    issue_cands: list[dict] = []
    pr_cands: list[dict] = []
    catalog = [
        r["full_name"]
        for r in get_repos(get_or_create_config(session, user_id))
        if "/" in r["full_name"]
    ]
    for repo_full in catalog:
        owner, repo_name = repo_full.split("/", 1)
        pat = get_pat_for_owner(session, user_id, owner)
        if not pat:
            # No token = this repo is unreadable until one is added (not a transient
            # failure) — exclude it rather than blocking matching forever, but say so.
            _log.warning(
                "dev match: no token for owner %r — %s excluded from candidates "
                "(user %s)",
                owner,
                repo_full,
                user_id,
            )
            tally["matching_skipped"] = True
            continue
        # The two fetches fail independently: a repo with issues DISABLED (410 on
        # /issues; 404 for renamed/out-of-grant) can still carry matchable PRs.
        # Permanent statuses exclude just that list (retrying can't fix them); anything
        # else is transient and aborts the whole phase so no draft settles
        # matched-empty while its true match's repo was unreachable.
        fetched_issues: list[dict] = []
        fetched_prs: list[dict] = []
        for label, fetch, sink in (
            ("issues", github.list_open_issues, fetched_issues),
            ("PRs", github.list_recent_prs, fetched_prs),
        ):
            try:
                sink.extend(await fetch(pat, owner, repo_name))
            except github.GithubError as exc:
                tally["matching_skipped"] = True
                if exc.status in (404, 410):
                    _log.warning(
                        "dev match: %s of %s unreadable (HTTP %s: %s) — that list "
                        "contributes no candidates (user %s)",
                        label,
                        repo_full,
                        exc.status,
                        exc.message,
                        user_id,
                    )
                    continue
                _log.warning(
                    "dev match: %s fetch failed for %s (HTTP %s: %s) — match phase "
                    "aborted, drafts left unmatched for the next scan (user %s)",
                    label,
                    repo_full,
                    exc.status,
                    exc.message,
                    user_id,
                )
                return tally
        # Tag every candidate with its repo — numbers are only unique per repo, and
        # the (repo, number) pair is the validation key.
        issue_cands.extend({**c, "repo": repo_full} for c in fetched_issues)
        pr_cands.extend({**c, "repo": repo_full} for c in fetched_prs)

    if not issue_cands and not pr_cands:
        # Nothing to match against — settle the NULL-guard without an LLM call.
        for draft in drafts:
            draft.related_issues = "[]"
            draft.updated_at = _now()
            session.add(draft)
        session.commit()
        return tally

    issue_by_key = {(c["repo"], c["number"]): c for c in issue_cands}
    pr_by_key = {(c["repo"], c["number"]): c for c in pr_cands}

    # Chunk the drafts across matcher calls (candidates repeated per call, matches
    # merged code-side): the OUTPUT budget is per call, and a whole-backlog call can
    # outgrow it — the first prod run put 78 drafts in one call and truncated at
    # DEV_MATCH_MAX_TOKENS, matching nothing. A failed chunk skips only itself.
    chunk_size = max(1, config.DEV_MATCH_DRAFT_CHUNK)
    for start in range(0, len(drafts), chunk_size):
        chunk = drafts[start : start + chunk_size]
        result = await synth.match_issues(
            [{"title": d.title, "body": d.body, "repo": d.repo} for d in chunk],
            issue_cands,
            pr_cands,
        )
        if result is None:
            tally["matching_skipped"] = True
            continue

        matches_by_index = {dm.draft_index: dm.matches for dm in result.drafts}
        for i, draft in enumerate(chunk):
            await _dispose_draft_matches(
                session,
                draft,
                _validated_matches(
                    matches_by_index.get(i, []), issue_by_key, pr_by_key
                ),
                tally,
                user_id,
                pr_by_key,
            )
    return tally


async def _dispose_draft_matches(
    session: Session,
    draft: DevIssueDraft,
    validated: list[dict],
    tally: dict,
    user_id: int,
    pr_by_key: dict[tuple[str, int], dict],
) -> None:
    """Store one draft's validated matches (link + `Related:` line) and attempt the
    comment conversion when its top issue match is high-confidence."""
    draft.related_issues = json.dumps(validated)  # "[]" = matched, none found
    if validated:
        tally["linked"] += 1
        line = _related_line(validated, draft.repo)
        if line:
            draft.body = (draft.body or "").rstrip() + "\n\n" + line
    draft.updated_at = _now()
    session.add(draft)
    session.commit()

    # Convert only on a high-confidence ISSUE match (PRs are never comment
    # targets — a PR-only match stays a linked issue draft). The match may live in a
    # DIFFERENT repo than the draft's tag — the token routes by the MATCH's owner.
    top_issue = next((v for v in validated if v["type"] == "issue"), None)
    if top_issue and top_issue["confidence"] == "high":
        pat = get_pat_for_owner(session, user_id, top_issue["repo"].split("/", 1)[0])
        if pat and await _convert_to_comment(
            session, draft, pat, top_issue, validated, pr_by_key, user_id
        ):
            tally["converted"] += 1


async def _convert_to_comment(
    session: Session,
    draft: DevIssueDraft,
    pat: str,
    top_issue: dict,
    validated: list[dict],
    pr_by_key: dict[tuple[str, int], dict],
    user_id: int,
) -> bool:
    """Fetch the matched issue's thread (plus any high-matched PR's commit subjects),
    ask the drafter what the draft adds, and mutate accordingly:

    - `has_new_info` → the draft becomes `kind=comment`: target set from the VALIDATED
      match, body replaced by the comment markdown (explicit owner sign-off — the
      original body is superseded), project preselect cleared, and **`repo` re-tagged
      to the target issue's repo** — the comment lives where the issue lives, which
      also heals a synthesiser mis-tag. Title kept for display and the do-not-redraft
      list.
    - nothing new → the draft stays `kind=issue`, its top match flagged
      `nothing_new: true`; the HUMAN dismisses — never auto-dismiss.
    Returns True only on an actual conversion. Best-effort: any failure leaves the
    linked issue draft as-is."""
    t_owner, t_repo = top_issue["repo"].split("/", 1)
    try:
        issue = await github.get_issue(pat, t_owner, t_repo, top_issue["number"])
        comments = await github.list_issue_comments(
            pat, t_owner, t_repo, top_issue["number"]
        )
    except github.GithubError:
        _log.warning(
            "dev match: thread fetch for %s#%s failed — draft %s left linked",
            top_issue["repo"],
            top_issue["number"],
            draft.id,
        )
        return False

    # A high-matched PR contributes title/description/commit subjects as drafter
    # context (the ONLY point commit subjects are ever fetched — never at candidate
    # stage). Its token routes by ITS repo's owner; a failed subjects fetch (or a
    # missing token) degrades to metadata-only context.
    related_prs: list[dict] = []
    for v in validated:
        key = (v.get("repo"), v["number"])
        if v["type"] == "pr" and v["confidence"] == "high" and key in pr_by_key:
            cand = pr_by_key[key]
            p_owner, p_repo = v["repo"].split("/", 1)
            p_pat = get_pat_for_owner(session, user_id, p_owner)
            subjects: list[str] = []
            if p_pat:
                try:
                    subjects = await github.list_pr_commit_subjects(
                        p_pat, p_owner, p_repo, v["number"]
                    )
                except github.GithubError:
                    subjects = []
            related_prs.append({**cand, "commit_subjects": subjects})

    result = await synth.draft_comment(
        {"title": draft.title, "body": draft.body},
        {**issue, "comments": comments},
        related_prs,
    )
    if result is None:
        return False

    if result.has_new_info and (result.comment_markdown or "").strip():
        draft.kind = KIND_COMMENT
        draft.repo = top_issue["repo"]  # re-tag: the comment lives with the issue
        draft.target_issue_number = top_issue["number"]
        draft.target_issue_url = top_issue["url"]
        body = result.comment_markdown.strip()
        # The secondary links still land inside the filed comment; the target issue
        # itself is excluded (a reference to #N inside a comment on #N is noise).
        line = _related_line(validated, draft.repo, exclude=top_issue)
        draft.body = body + ("\n\n" + line if line else "")
        draft.project_node_id = None
        draft.project_title = None
        draft.updated_at = _now()
        session.add(draft)
        session.commit()
        return True

    # Covered by the existing issue with nothing to add: flag it, keep it an issue
    # draft, and leave the decision to the human ("drafts are cheap, filing is sacred").
    matches = json.loads(draft.related_issues or "[]")
    for m in matches:
        if (
            m.get("type") == "issue"
            and m.get("number") == top_issue["number"]
            and m.get("repo") == top_issue["repo"]
        ):
            m["nothing_new"] = True
            break
    draft.related_issues = json.dumps(matches)
    draft.updated_at = _now()
    session.add(draft)
    session.commit()
    return False


# ── Drafts view + edits ───────────────────────────────────────────────────────

# The tab a caller asks for → the underlying status value. `review` is the pending lane.
TAB_STATUS = {
    "review": DRAFT,
    "saved": SAVED,
    "filed": FILED,
    "dismissed": DISMISSED,
}

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50


def _naive_utc(dt: datetime) -> datetime:
    """Normalise to a tz-naive UTC datetime. Rows come back from the DB naive (the
    column is timezone-less on both SQLite and Postgres) while freshly built values are
    tz-aware, so keyset comparisons must be done on one consistent representation."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def encode_cursor(draft: DevIssueDraft) -> str:
    """An opaque keyset token for the last row of a page — `(updated_at, id)`, NOT an
    offset, so rows landing mid-scroll never shift or duplicate a page."""
    raw = f"{_naive_utc(draft.updated_at).isoformat()}|{draft.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + pad).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), int(id_str)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ApiError(400, "bad_cursor", "That page cursor is not valid.") from exc


def list_drafts(
    session: Session,
    user_id: int,
    *,
    status: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str | None = None,
) -> tuple[list[DevIssueDraft], str | None]:
    """One page of the user's drafts, newest activity first (`updated_at` desc, `id`
    desc as a stable tiebreak).

    `status` filters to a single lane (None = every lane, used by the tests and any
    whole-list caller). Paging is **keyset**: `cursor` carries the previous page's last
    `(updated_at, id)` and the query asks for strictly-older rows, so a draft created
    between two page fetches neither shifts nor duplicates a row. Returns
    `(items, next_cursor)`; `next_cursor` is None on the last page."""
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    query = select(DevIssueDraft).where(DevIssueDraft.user_id == user_id)
    if status is not None:
        query = query.where(DevIssueDraft.status == status)
    if cursor:
        c_ts, c_id = decode_cursor(cursor)
        query = query.where(
            or_(
                DevIssueDraft.updated_at < c_ts,
                and_(
                    DevIssueDraft.updated_at == c_ts,
                    DevIssueDraft.id < c_id,
                ),
            )
        )
    query = query.order_by(
        DevIssueDraft.updated_at.desc(), DevIssueDraft.id.desc()
    ).limit(limit + 1)  # one extra row: presence of it = there is a next page
    rows = list(session.exec(query).all())
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


def draft_counts(session: Session, user_id: int) -> dict[str, int]:
    """Per-tab draft counts (drives the tab badges). One grouped query, user-scoped."""
    rows = session.exec(
        select(DevIssueDraft.status, func.count())
        .where(DevIssueDraft.user_id == user_id)
        .group_by(DevIssueDraft.status)
    ).all()
    by_status = {status: count for status, count in rows}
    return {tab: by_status.get(value, 0) for tab, value in TAB_STATUS.items()}


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
    text already lives on GitHub — and a dismissed one is declined, so edits apply only
    while the draft is still actionable: `draft` (in review) or `saved` (shelved, goal
    12a — a shelf is not a freeze)."""
    draft = _owned_draft(session, user_id, draft_id)
    if draft.status not in (DRAFT, SAVED):
        raise ApiError(409, "draft_not_editable", "Only a pending draft can be edited.")
    if title is not None:
        draft.title = title.strip()
    if body is not None:
        draft.body = body
    if repo is not None and repo.strip() != draft.repo:
        # A comment draft's target is fixed — re-targeting one means dismissing it
        # (the UI hides the repo dropdown; this backstops a direct API call).
        if draft.kind == KIND_COMMENT:
            raise ApiError(
                409,
                "comment_target_fixed",
                "A comment draft's target is fixed — dismiss it instead.",
            )
        draft.repo = repo.strip()
        # Matches survive a repo change (12b.1): they were judged against the WHOLE
        # catalog, so re-targeting the draft doesn't invalidate them — correcting a
        # synthesiser mis-tag must not throw away the cross-repo links just found.
    if project_node_id is not None:
        draft.project_node_id = project_node_id or None
        draft.project_title = (project_title or "").strip() or None
    draft.updated_at = _now()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


def dismiss_draft(session: Session, user_id: int, draft_id: int) -> DevIssueDraft:
    """Decline a draft — a local status flip, zero GitHub calls. Reachable from the
    review lane and the saved shelf alike."""
    draft = _owned_draft(session, user_id, draft_id)
    if draft.status == FILED:
        raise ApiError(409, "already_filed", "A filed draft cannot be dismissed.")
    return _flip_status(session, draft, DISMISSED)


def save_draft(session: Session, user_id: int, draft_id: int) -> DevIssueDraft:
    """Set a draft aside for later (goal 12a) — the "not now" that isn't "no". A local
    status flip, zero GitHub calls, idempotent (re-saving a saved draft is a no-op).
    The card stays fully actionable from the saved shelf."""
    draft = _owned_draft(session, user_id, draft_id)
    if draft.status == FILED:
        raise ApiError(409, "already_filed", "A filed draft cannot be saved for later.")
    if draft.status == DISMISSED:
        raise ApiError(
            409, "draft_dismissed", "Move the draft back to review before saving it."
        )
    return _flip_status(session, draft, SAVED)


def unsave_draft(session: Session, user_id: int, draft_id: int) -> DevIssueDraft:
    """Move a card back to the review lane — the escape hatch out of the saved shelf
    (and out of the dismissed lane, so a mis-click is recoverable). A local status flip,
    zero GitHub calls, idempotent. A filed draft has no way back: the issue exists."""
    draft = _owned_draft(session, user_id, draft_id)
    if draft.status == FILED:
        raise ApiError(409, "already_filed", "A filed draft cannot return to review.")
    return _flip_status(session, draft, DRAFT)


def _flip_status(session: Session, draft: DevIssueDraft, status: str) -> DevIssueDraft:
    """Persist a local status change. Idempotent: a flip to the status a draft already
    holds leaves `updated_at` (and so the card's position in its lane) alone."""
    if draft.status == status:
        return draft
    draft.status = status
    draft.updated_at = _now()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


# ── GitHub filing (the human-approved write; partial-state idempotent) ────────

# Per filing step: the fallback error code, what the step was trying to do (phrased to
# drop into a sentence), and the fine-grained-PAT permission it needs. The permission
# differs per step — creating an issue and commenting want Issues, the project attach
# wants Projects — which is exactly the distinction a single "github write failed"
# message loses.
_FILE_STEPS = {
    "create": ("github_create_failed", "create the issue", "Issues: Read and write"),
    "comment": ("github_comment_failed", "post the comment", "Issues: Read and write"),
    "attach": (
        "github_project_attach_failed",
        "attach the issue to the project",
        "Projects: Read and write",
    ),
}


def _filing_error(
    step: str, exc: github.GithubError, *, draft_id: int, owner: str, repo: str
) -> ApiError:
    """Turn a `GithubError` into the specific thing the user can go fix, and log it.

    Two constraints shape this. The HTTP status stays 502 for every case: the failure
    IS upstream, and a 401 would trip the frontend's session handler and sign the user
    out over a stale GitHub token. So all the diagnosis has to ride in the code and the
    message. And GitHub only ever names the symptom — "Bad credentials", "Resource not
    accessible by personal access token" — never which of the several things the user
    controls (token freshness, the token's repo set, its permissions, the repo's own
    settings) is the one at fault. Each branch below names that instead, and keeps
    GitHub's own words appended so nothing is hidden."""
    generic, what, permission = _FILE_STEPS[step]
    status, reason = exc.status, exc.message
    lowered = reason.lower()
    # A fine-grained PAT that cannot see a repo gets a 404, not a 403 — GitHub hides
    # existence rather than admitting the repo is off-limits, so both statuses collapse
    # onto one remedy. GraphQL (the attach step) reports the same class of failure as a
    # scope complaint inside a 200, which reaches us as 422.
    denied = status in (403, 404) or (
        status == 422
        and any(w in lowered for w in ("scope", "permission", "not authorized"))
    )

    if status == 0:
        code = "github_unreachable"
        fix = f"Could not reach GitHub to {what}. Check the host's network, then retry."
    elif status == 401:
        code = "github_token_invalid"
        fix = (
            f"GitHub rejected the stored token for '{owner}' while trying to {what}. "
            "Fine-grained tokens expire — add a fresh one in the Dev config."
        )
    elif status in (403, 429) and "rate limit" in lowered:
        code = "github_rate_limited"
        fix = (
            f"GitHub rate-limited the attempt to {what}. Wait a few minutes, then "
            "retry — nothing was written."
        )
    elif denied:
        code = "github_no_permission"
        fix = (
            f"The token for '{owner}' is not allowed to {what} on {repo}. Check that "
            f"'{repo}' is in the token's selected repositories and that the token "
            f"grants '{permission}'."
        )
    elif status == 410:
        code = "github_issues_disabled"
        fix = f"Issues are turned off on {repo}, so the app cannot {what}."
    elif status == 422:
        code = "github_rejected"
        fix = f"GitHub rejected the request to {what} as invalid."
    elif status >= 500:
        code = "github_server_error"
        fix = f"GitHub itself failed while trying to {what}. Retry in a few minutes."
    else:
        code = generic
        fix = f"GitHub would not {what}."

    _log.warning(
        "draft %s: could not %s on %s — %s (GitHub %s: %s)",
        draft_id,
        what,
        repo,
        code,
        status or "unreachable",
        reason,
    )
    # "GitHub said" would be a lie when GitHub never answered — a transport failure's
    # detail is ours, and its text already leads with the same "could not reach" phrase.
    detail = (
        reason.removeprefix("Could not reach GitHub: ")
        if status == 0
        else f"GitHub said: {reason}"
    )
    return ApiError(502, code, f"{fix} ({detail})")


async def file_draft(session: Session, user_id: int, draft_id: int) -> DevIssueDraft:
    """File the draft on GitHub (human-approved) — the only GitHub mutation path.

    `kind=issue`: create the issue + attach it to the chosen project, idempotent with
    partial-state recording — the issue number/url is stored the moment creation
    succeeds, so if the project-attach fails the retry re-runs ONLY the GraphQL step
    (an issue is never double-created).

    `kind=comment` (goal 12b): ONE comment on the pre-existing target issue — no
    create_issue, no project attach, no partial-state dance. Success flips to `filed`
    with the comment URL; failure leaves the draft untouched for a retry click.
    Comments are the only mutation ever applied to a pre-existing GitHub object.

    Filing needs a valid PAT; a GitHub failure surfaces as an `ApiError` built by
    `_filing_error`, which names the remedy for THIS step rather than echoing GitHub's
    symptom, and logs the reason host-side (rollback-not-blind-retry)."""
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

    if draft.kind == KIND_COMMENT:
        if not draft.target_issue_number:
            raise ApiError(
                400, "no_comment_target", "This comment draft has no target issue."
            )
        if not draft.issue_url:  # already-posted guard (a re-click never double-posts)
            try:
                created = await github.create_issue_comment(
                    pat, owner, repo_name, draft.target_issue_number, draft.body
                )
            except github.GithubError as exc:
                raise _filing_error(
                    "comment", exc, draft_id=draft.id, owner=owner, repo=draft.repo
                ) from exc
            draft.issue_url = created["url"]  # the COMMENT's html_url
            draft.issue_number = draft.target_issue_number
            draft.status = FILED
            draft.updated_at = _now()
            session.add(draft)
            session.commit()
            session.refresh(draft)
        return draft

    # Step 1 — create the issue (skipped if a prior attempt already created it).
    if not draft.issue_url:
        try:
            created = await github.create_issue(
                pat, owner, repo_name, draft.title, draft.body
            )
        except github.GithubError as exc:
            raise _filing_error(
                "create", exc, draft_id=draft.id, owner=owner, repo=draft.repo
            ) from exc
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
            raise _filing_error(
                "attach", exc, draft_id=draft.id, owner=owner, repo=draft.repo
            ) from exc
        draft.project_attached = True
        draft.updated_at = _now()
        session.add(draft)
        session.commit()
        session.refresh(draft)

    return draft
