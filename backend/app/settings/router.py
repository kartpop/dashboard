"""Settings HTTP surface (goal 8).

Per-user calendar toggles + the read-only notes folder/Doc ids, plus the
superuser-only allowed-email admin. Everything is scoped to `current_user`; the
allowed-email endpoints additionally require `is_superuser` (403 otherwise) and are
hidden entirely from the non-superuser UI.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from google.oauth2.credentials import Credentials
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.auth import service as auth_svc
from app.auth.deps import (
    get_current_credentials,
    get_current_user,
    require_superuser,
)
from app.auth.models import User
from app.db import get_session
from app.errors import ApiError
from app.google import calendar as calendar_client
from app.settings import service as settings_svc

router = APIRouter(prefix="/settings", tags=["settings"])


def _folder_url(fid: str | None) -> str | None:
    return f"https://drive.google.com/drive/folders/{fid}" if fid else None


def _doc_url(did: str | None) -> str | None:
    return f"https://docs.google.com/document/d/{did}/edit" if did else None


@router.get("")
async def get_settings(
    user: User = Depends(get_current_user),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    """Return the user's settings: which calendars they can see (with the toggle
    state), and their notes folder/Doc ids (bootstrapped here on first visit)."""
    enabled = set(settings_svc.get_enabled_calendar_ids(session, user.id))
    try:
        calendars = await calendar_client.get_calendar_list(creds)
    except Exception as exc:
        raise ApiError(
            502, "google_calendar_unavailable", "Could not list your calendars."
        ) from exc
    calendars_out = [
        {**c, "enabled": c["primary"] or c["id"] in enabled} for c in calendars
    ]
    # Include any toggled-on id the calendarList didn't return (add-by-id calendars).
    known = {c["id"] for c in calendars}
    for cid in enabled - known:
        calendars_out.append(
            {
                "id": cid,
                "summary": cid,
                "primary": False,
                "background_color": None,
                "enabled": True,
            }
        )

    # "settings visit" bootstraps the notes folder/Doc if they don't exist yet.
    doc_id, folder_id = await settings_svc.ensure_notes_target(session, creds, user.id)
    return {
        "calendars": calendars_out,
        "enabled_calendar_ids": sorted(enabled),
        "notes_folder_id": folder_id,
        "notes_doc_id": doc_id,
        "notes_folder_url": _folder_url(folder_id),
        "notes_doc_url": _doc_url(doc_id),
    }


class CalendarsUpdate(BaseModel):
    # The full set of extra (non-primary) calendar ids to merge into the day strip.
    # Free-text ids not in calendarList are accepted (add-by-id, goal 8).
    calendar_ids: list[str]


@router.put("/calendars")
async def set_calendars(
    body: CalendarsUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    settings_svc.set_enabled_calendars(session, user.id, body.calendar_ids)
    return {
        "enabled_calendar_ids": settings_svc.get_enabled_calendar_ids(session, user.id)
    }


# ── Notes hierarchy (goal 9) ──────────────────────────────────────────────────


class NoteNodeIn(BaseModel):
    """One incoming tree node. `drive_id` is deliberately absent — it is never
    trusted from the client; materialization re-derives it by `node_id` (ID
    hygiene, ADR layer 3)."""

    node_id: str
    name: str
    kind: Literal["folder", "doc"]
    children: list["NoteNodeIn"] = Field(default_factory=list)


class NotesIndexUpdate(BaseModel):
    nodes: list[NoteNodeIn] = Field(default_factory=list)


def _node_out(node: dict) -> dict:
    """Serialize a materialized node for the client (drive_id + a read-only link)."""
    drive_id = node.get("drive_id")
    kind = node.get("kind")
    return {
        "node_id": node.get("node_id"),
        "name": node.get("name"),
        "kind": kind,
        "drive_id": drive_id,
        "drive_url": (_doc_url(drive_id) if kind == "doc" else _folder_url(drive_id)),
        "children": [_node_out(c) for c in (node.get("children") or [])],
    }


@router.get("/notes-index")
async def get_notes_index(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Return the user's notes hierarchy (nodes with materialized drive ids/links)."""
    forest = settings_svc.get_notes_index(session, user.id)
    return {"nodes": [_node_out(n) for n in forest]}


@router.put("/notes-index")
async def put_notes_index(
    body: NotesIndexUpdate,
    user: User = Depends(get_current_user),
    creds: Credentials = Depends(get_current_credentials),
    session: Session = Depends(get_session),
):
    """Validate + eagerly materialize the edited tree, then return the persisted
    tree (with drive ids). Validation violations → 422; a Drive failure partway
    persists what succeeded and surfaces the error (retry is idempotent by node_id)."""
    incoming = [n.model_dump() for n in body.nodes]
    forest = await settings_svc.set_notes_index(session, creds, user.id, incoming)
    return {"nodes": [_node_out(n) for n in forest]}


# ── Allowed emails (superuser only) ───────────────────────────────────────────


def _allowed_out(row) -> dict:
    return {
        "id": row.id,
        "email": row.email,
        "added_by": row.added_by,
        "created_at": row.created_at.isoformat(),
        # Per-user feature flags (goal 11): every known key present, defaulting False.
        "features": {
            key: auth_svc.parse_features(row.features).get(key, False)
            for key in auth_svc.FEATURE_KEYS
        },
    }


class AllowedEmailCreate(BaseModel):
    email: str


class FeatureUpdate(BaseModel):
    feature: str
    enabled: bool


@router.get("/allowed-emails")
async def list_allowed(
    superuser: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    return {
        # The togglable feature registry, so the UI renders a checkbox per feature
        # without hard-coding names (goal 11; goal 12 generalises per-user flags).
        "features": [{"key": k, "label": label} for k, label in auth_svc.FEATURES],
        "allowed": [_allowed_out(r) for r in auth_svc.list_allowed(session)],
    }


@router.post("/allowed-emails", status_code=201)
async def add_allowed(
    body: AllowedEmailCreate,
    superuser: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    email = body.email.strip().lower()
    if "@" not in email:
        raise ApiError(400, "invalid_email", "Enter a valid email address.")
    row = auth_svc.add_allowed(session, email, added_by=superuser.email)
    return _allowed_out(row)


@router.delete("/allowed-emails/{email}")
async def remove_allowed(
    email: str,
    superuser: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    ok = auth_svc.remove_allowed(session, email)
    if not ok:
        raise ApiError(
            400,
            "cannot_remove",
            "That email is not removable (unknown, or the superuser's own).",
        )
    return {"ok": True}


@router.put("/allowed-emails/{email}/features")
async def set_feature(
    email: str,
    body: FeatureUpdate,
    superuser: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    """Toggle one per-user feature flag (e.g. News) on an allowlisted email."""
    try:
        row = auth_svc.set_feature(session, email, body.feature, body.enabled)
    except KeyError as exc:
        raise ApiError(
            400, "unknown_feature", f"Unknown feature: {body.feature}"
        ) from exc
    except LookupError as exc:
        raise ApiError(404, "not_found", "That email is not on the allowlist.") from exc
    return _allowed_out(row)
