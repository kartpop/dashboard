"""Auth HTTP surface (goal 8): Google Sign-In login/callback, logout, and `/me`.

Google Sign-In doubles as the OAuth grant. `/auth/login` sends the user to consent
(identity + the three scopes, `access_type=offline`); `/auth/callback` verifies the
ID token, enforces the allowlist, upserts the user with an encrypted refresh token,
and sets the session cookie. A non-allowlisted account is bounced to a "not invited"
page with no user row and no token stored.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.auth import service as auth_svc
from app.auth.deps import get_current_user
from app.auth.models import User
from app.db import get_session
from app.dev import gating as dev_gating
from app.google import auth as google_auth
from app.news import gating as news_gating

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _frontend_url(path: str = "/") -> str:
    origin = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173").rstrip("/")
    return f"{origin}{path}"


@router.get("/login")
async def login(request: Request):
    """Redirect to Google's consent screen; stash the CSRF `state` in the session."""
    url, state, code_verifier = google_auth.authorization_url()
    request.session["oauth_state"] = state
    if code_verifier is not None:
        request.session["oauth_code_verifier"] = code_verifier
    return RedirectResponse(url)


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
):
    saved_state = request.session.pop("oauth_state", None)
    code_verifier = request.session.pop("oauth_code_verifier", None)
    if error or not code:
        return RedirectResponse(_frontend_url("/?auth_error=denied"))
    if saved_state and state and saved_state != state:
        return RedirectResponse(_frontend_url("/?auth_error=state_mismatch"))

    try:
        creds = google_auth.exchange_code(
            code, state=state, code_verifier=code_verifier
        )
        claims = google_auth.verify_id_token(creds.id_token)
    except Exception as exc:  # noqa: BLE001 — any exchange/verify failure → bounce
        logger.warning("OAuth callback failed: %s", exc)
        return RedirectResponse(_frontend_url("/?auth_error=exchange_failed"))

    email = (claims.get("email") or "").strip().lower()
    if not email or not auth_svc.is_email_allowed(session, email):
        request.session.clear()
        return RedirectResponse(_frontend_url("/?auth_error=not_invited"))

    granted = list(creds.scopes or [])
    try:
        google_auth.assert_scopes_within_allowlist(granted)
    except google_auth.ScopeError:
        request.session.clear()
        return RedirectResponse(_frontend_url("/?auth_error=scope"))

    user = auth_svc.get_or_create_user(session, claims, creds.refresh_token, granted)
    request.session["user_id"] = user.id
    return RedirectResponse(_frontend_url("/"))


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "is_superuser": user.is_superuser,
        # Goal 11: whether the News feature is on for this user (the per-user flag on
        # their allowlist row; superusers always). The frontend shows the News rail
        # entry only when true; /news 403s otherwise.
        "news_enabled": news_gating.is_news_enabled(session, user),
        # Goal 12: whether the Dev feature is on for this user (drives the Dev rail
        # entry). The flag gates /dev/* endpoints and the scheduled scan identically.
        "dev_enabled": dev_gating.is_dev_enabled(session, user),
    }
