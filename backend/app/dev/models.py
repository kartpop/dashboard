"""Dev-view persistence (goal 12).

Four per-user tables backing the meeting-notes → issue-draft pipeline:

- `dev_config` — one row per user: the selected source nodes (notes-hierarchy node
  ids), the repo catalog (full name + a short user-written description + a default
  flag), the per-repo default ProjectsV2 project, and the daily-scan bookmark. This is
  the dev counterpart to `news_profile` — the module owns its own config tables rather
  than bloating `user_settings`, matching the news precedent.
- `dev_pat` — the GitHub tokens, **one row per resource owner** (`(user_id, owner)`
  unique). A fine-grained PAT is bound to a single resource owner at mint time, so
  filing issues into both a personal account and an org needs one token each; the token
  is chosen at file time by the target repo's owner. Each token is Fernet-encrypted at
  rest exactly like the Google refresh token in `user` and never returned to the client
  (only a masked hint + the owner it covers). The owner(s) a token covers are derived
  from the repos it can see — never hand-typed. (Supersedes the single legacy
  `dev_config.pat_encrypted` column, kept only for the one-way data migration.)
- `dev_doc_cursor` — the forward-only, process-once cursor **per (user, doc)**: the
  newest entry timestamp already processed, plus the boundary keys (entries at exactly
  that minute already consumed). No marker is ever written into the Doc — the cursor is
  invisible, undeletable, and equally token-frugal.
- `dev_issue_draft` — one proposed issue: title/body/repo (LLM-proposed, human-edited),
  status draft|saved|filed|dismissed, the multi-source provenance (which entries it was
  synthesised from), the chosen project, and — once filed — the GitHub issue url/number.

Row-scoping (goal 8): every table carries `user_id`; every query filters by it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# ── dev_issue_draft.status ────────────────────────────────────────────────────
# The value set is a **convention on a free-text column** (`max_length=20`), not a DB
# enum — widening it (goal 12a added `saved`) needs no migration.
#
# Proposed by the LLM, awaiting the owner's review. The default resting state.
DRAFT = "draft"
# Set aside for later (goal 12a): a "not now" that isn't "no". A shelf, not a terminal
# state — a saved draft is still fully editable and still offers Approve & file /
# Dismiss / Move back to review. Local status flip only — zero GitHub calls.
SAVED = "saved"
# Approved + created on GitHub (issue_url/issue_number set). Terminal for the happy
# path; a project-attach that failed leaves `project_attached` False for retry.
FILED = "filed"
# The owner declined the draft. Local status flip only — zero GitHub calls.
DISMISSED = "dismissed"

# ── dev_issue_draft.kind (goal 12b) ───────────────────────────────────────────
# Same free-text-column convention as `status`. An `issue` draft files as a new GitHub
# issue (the two-step create + project-attach path); a `comment` draft — a confirmed
# duplicate carrying new information — files as ONE comment on its `target_issue_number`
# (no create_issue, no project attach). Comment drafts flow through the 12a lanes
# exactly like issue drafts; only the filing branch differs.
KIND_ISSUE = "issue"
KIND_COMMENT = "comment"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DevConfig(SQLModel, table=True):
    __tablename__ = "dev_config"

    # One row per user; the user id doubles as the primary key (like news_profile).
    user_id: int = Field(primary_key=True, foreign_key="user.id")
    # LEGACY (goal-12 v1): the single Fernet-encrypted GitHub PAT. Superseded by the
    # per-owner `dev_pat` table — the multi-PAT migration copies this into `dev_pat`
    # (keyed by the owner derived from the configured repos) and NULLs it. Nothing reads
    # it anymore; it lingers only so the migration is reversible-ish and no secret is
    # silently dropped for a config whose owner can't be derived.
    pat_encrypted: Optional[str] = Field(default=None)
    # JSON list of notes-hierarchy `node_id`s the user marked as meeting-notes sources.
    # A node may be a Doc leaf (that Doc) or a folder (every Doc under it, recursively).
    # Resolved to concrete drive ids at scan time from the hierarchy index — never from
    # LLM output. Empty ("[]") → no sources configured (the scan is a no-op).
    sources_json: str = Field(default="[]")
    # JSON list of {full_name, description, is_default} — the repo catalog the user
    # ticked from what the PAT can see. `description` is the ONLY hand-typed field; it
    # feeds the LLM's repo pick. `is_default` marks the fallback repo.
    repos_json: str = Field(default="[]")
    # JSON map {repo_full_name: {node_id, title}} — the default ProjectsV2 project per
    # repo, fetched via the API. The per-card project dropdown can override at file time.
    projects_json: str = Field(default="{}")
    # Daily-scan bookmark: when the scan last completed for this user.
    last_scan_at: Optional[datetime] = Field(default=None)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class DevPat(SQLModel, table=True):
    __tablename__ = "dev_pat"
    __table_args__ = (
        # A user stores at most one token per GitHub resource owner; a re-add for the
        # same owner replaces (upserts) rather than duplicates.
        UniqueConstraint("user_id", "owner", name="uq_dev_pat_user_owner"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)  # row-scoping (goal 8)
    # The GitHub resource owner (login or org) this token files under — the routing key.
    # Filing to `owner/repo` selects the token whose `owner` matches. Derived from the
    # repos the token can see (a fine-grained PAT is scoped to one owner), never typed.
    owner: str = Field(index=True)
    # The fine-grained GitHub PAT, **Fernet-encrypted** (never plaintext, never logged,
    # never echoed back — the config API returns only a masked hint + the owner/login).
    # Same encryption posture as the Google refresh token in `user`. A classic token that
    # spans several owners is stored once per owner (each row an independent ciphertext).
    pat_encrypted: str = Field()
    # The GitHub login that created the token (from `GET /user`) — display only.
    login: Optional[str] = Field(default=None)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class DevDocCursor(SQLModel, table=True):
    __tablename__ = "dev_doc_cursor"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)  # row-scoping (goal 8)
    # The app-created Doc's drive id this cursor tracks. (user_id, doc_id) is unique.
    doc_id: str = Field(index=True)
    # The newest entry timestamp already processed (IST wall-clock, minute-granular,
    # stored tz-naive — every entry timestamp is IST wall-clock so a naive compare is
    # consistent and avoids a tz round-trip through SQLite). A scan keeps entries with
    # timestamp strictly newer than this, PLUS entries equal to it whose key is not in
    # `boundary_entry_keys`.
    last_processed_entry_ts: Optional[datetime] = Field(default=None)
    # JSON list of stable keys (hash of entry timestamp + H3 one-liner) of the entries
    # already processed at exactly `last_processed_entry_ts`. Timestamps are
    # minute-granular and several entries can share one (batch pastes do this), so a
    # strictly-newer rule alone would silently drop a same-minute entry captured after a
    # scan; the boundary keys close that gap without reprocessing.
    boundary_entry_keys: str = Field(default="[]")
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class DevIssueDraft(SQLModel, table=True):
    __tablename__ = "dev_issue_draft"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)  # row-scoping (goal 8)
    # The issue title + body_markdown — LLM-proposed, then human-edited on the card.
    # Stored verbatim as a draft; the GitHub write uses the card's *current* values.
    title: str = Field()
    body: str = Field(default="")
    # The target repo full name (owner/repo). LLM-proposed; validated against the
    # user's configured catalog on dispose (out-of-catalog → the default repo).
    repo: str = Field(default="")
    status: str = Field(default=DRAFT, max_length=20, index=True)
    # JSON array of provenance rows {doc_id, doc_path, entry_ts} — the entries this
    # draft was synthesised from. A merged issue cites several; the card renders them as
    # the muted sources line, so a de-duplicated draft visibly shows its provenance.
    sources: str = Field(default="[]")
    # The chosen ProjectsV2 project to attach on file (node id + title). Defaults to the
    # repo's configured default; the card dropdown can override before filing.
    project_node_id: Optional[str] = Field(default=None)
    project_title: Optional[str] = Field(default=None)
    # What filing does (goal 12b): `issue` = create a new issue; `comment` = post one
    # comment on `target_issue_number`. Free-text by the same convention as `status`.
    kind: str = Field(default=KIND_ISSUE, max_length=20)
    # A comment draft's target — the EXISTING issue it will comment on. Set by code from
    # a validated high-confidence match (url/number from the fetched candidate list,
    # never from LLM output). NULL on issue drafts.
    target_issue_number: Optional[int] = Field(default=None)
    target_issue_url: Optional[str] = Field(default=None)
    # JSON array of validated matches against live GitHub —
    # [{number, type: issue|pr, state, url, title, confidence, reason, nothing_new?}].
    # NULL = not yet matched (the NULL-guard: the matcher targets non-settled drafts
    # whose related_issues IS NULL, once per draft); "[]" = matched, nothing found.
    # Every url/title/type/state comes from the code-fetched candidate list keyed by
    # validated number — never from the model. Cleared (back to NULL) on a repo change.
    related_issues: Optional[str] = Field(default=None)
    # Set the moment issue creation succeeds (partial-state idempotency): once present,
    # a re-file never re-creates the issue — only the project-attach step may retry.
    # A filed COMMENT draft reuses these columns: issue_url holds the comment's
    # html_url, issue_number the target issue's number (no extra columns).
    issue_url: Optional[str] = Field(default=None)
    issue_number: Optional[int] = Field(default=None)
    # The created issue's GraphQL node id (needed to attach it to a project).
    issue_node_id: Optional[str] = Field(default=None)
    # Whether the ProjectsV2 attach succeeded. False after a create-succeeded /
    # attach-failed partial → the card shows "filed — project attach pending" and retry
    # re-runs ONLY the GraphQL step.
    project_attached: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)
