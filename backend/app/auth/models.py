"""Auth + multi-tenancy tables (goal 8).

`User` holds the identity + the **encrypted** Google refresh token (Fernet — never
readable in a raw DB dump) and the scopes actually granted, so the per-token scope
assertion can run at credential-load time. `AllowedEmail` is the invite list the
superuser edits; `SUPERUSER_EMAIL` (env) bootstraps it and flags its user row.
`UserSettings` replaces the `NOTES_*` / `EXTRA_CALENDAR_IDS` env vars with per-user
config (calendar toggle list + the app-created notes folder/Doc ids).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "user"

    id: Optional[int] = Field(default=None, primary_key=True)
    # Google's stable subject id (the `sub` claim) — the join key, never the email
    # (emails can change; `sub` cannot).
    google_sub: str = Field(index=True, unique=True, max_length=64)
    email: str = Field(index=True, unique=True, max_length=320)
    name: Optional[str] = Field(default=None, max_length=200)
    picture: Optional[str] = Field(default=None)
    is_superuser: bool = Field(default=False)
    # Fernet-encrypted refresh token (str). Never stored in plaintext.
    refresh_token_encrypted: Optional[str] = Field(default=None)
    # Space-separated scopes actually granted on the last consent — the per-token
    # scope assertion reads these (a broader-than-allowlist grant refuses to serve).
    granted_scopes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class AllowedEmail(SQLModel, table=True):
    __tablename__ = "allowed_email"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True, max_length=320)
    added_by: Optional[str] = Field(default=None, max_length=320)  # superuser email
    # Per-user feature flags (goal 11), a JSON object of feature-name -> bool, e.g.
    # {"news": true}. The superuser edits these alongside the invite list; the
    # superuser themselves always has every feature on (no row needed). Replaces the
    # NEWS_ENABLED_EMAILS env var. Unknown/missing keys read as False.
    features: str = Field(default="{}")
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)


class UserSettings(SQLModel, table=True):
    __tablename__ = "user_settings"

    # One row per user; the user id doubles as the primary key.
    user_id: int = Field(primary_key=True, foreign_key="user.id")
    # App-created (bootstrap) notes folder + Doc ids in THIS user's Drive. Replace
    # the NOTES_FOLDER_ID / NOTES_DOC_ID env vars. Config-only, never LLM output.
    notes_folder_id: Optional[str] = Field(default=None)
    notes_doc_id: Optional[str] = Field(default=None)
    # JSON list of extra calendar ids merged into the day strip (primary is always
    # on and is NOT stored here). Replaces EXTRA_CALENDAR_IDS.
    enabled_calendar_ids: str = Field(default="[]")
    # JSON forest (goal 9): the user-defined notes folder/Doc tree rooted UNDER
    # `notes_folder_id`. Each node is {node_id, name, kind: "folder"|"doc",
    # drive_id, children}. Leaves are Docs, inner nodes are folders. The default
    # `notes_doc_id` stays OUTSIDE this tree and remains the routing fallback.
    # Empty ("[]") for existing users → behavior identical to today.
    notes_index: str = Field(default="[]")
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)
