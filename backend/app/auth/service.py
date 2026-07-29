"""Auth service: user upsert, the email allowlist, and superuser bootstrap (goal 8).

Deterministic DB logic only — the OAuth dance lives in `app.google.auth`, the HTTP
routes in `app.auth.router`. The allowlist is a DB table the superuser edits;
`SUPERUSER_EMAIL` (env) is always allowed and flags its user row `is_superuser`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.auth.models import AllowedEmail, User
from app.google import auth as google_auth

# Per-user, superuser-togglable features (goal 11). Add a (key, label) pair here and
# the admin UI renders a checkbox for it automatically; nothing else needs to change.
# The superuser always has every feature on regardless of this map.
FEATURES: tuple[tuple[str, str], ...] = (("news", "News"),)
FEATURE_KEYS: frozenset[str] = frozenset(key for key, _ in FEATURES)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def superuser_email() -> str | None:
    email = os.environ.get("SUPERUSER_EMAIL")
    return email.strip().lower() if email else None


def is_email_allowed(session: Session, email: str) -> bool:
    """True if `email` is the superuser or appears in the allowlist table."""
    e = email.strip().lower()
    if e == superuser_email():
        return True
    return (
        session.exec(select(AllowedEmail).where(AllowedEmail.email == e)).first()
        is not None
    )


def get_or_create_user(
    session: Session,
    claims: dict,
    refresh_token: str | None,
    granted_scopes: list[str],
) -> User:
    """Upsert the `user` row from verified ID-token claims + a fresh grant.

    Stores the refresh token **encrypted** (only when Google returned one — a repeat
    sign-in without `prompt=consent` may omit it, so we keep the existing token).
    Flags `is_superuser` when the email matches `SUPERUSER_EMAIL`.
    """
    sub = claims["sub"]
    email = (claims.get("email") or "").strip().lower()
    user = session.exec(select(User).where(User.google_sub == sub)).first()
    if user is None:
        user = User(google_sub=sub, email=email)

    user.email = email
    user.name = claims.get("name")
    user.picture = claims.get("picture")
    user.is_superuser = email == superuser_email()
    user.granted_scopes = " ".join(granted_scopes)
    if refresh_token:
        user.refresh_token_encrypted = google_auth.encrypt_token(refresh_token)
    user.updated_at = _now()

    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ── Allowed-email CRUD (superuser only — enforced at the router) ──────────────


def list_allowed(session: Session) -> list[AllowedEmail]:
    return list(session.exec(select(AllowedEmail).order_by(AllowedEmail.email)).all())


def add_allowed(session: Session, email: str, added_by: str) -> AllowedEmail:
    e = email.strip().lower()
    existing = session.exec(select(AllowedEmail).where(AllowedEmail.email == e)).first()
    if existing:
        return existing
    row = AllowedEmail(email=e, added_by=added_by)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def remove_allowed(session: Session, email: str) -> bool:
    """Remove an allowlist entry. The superuser's own email can never be removed
    (returns False without deleting). Removal blocks future sign-ins only."""
    e = email.strip().lower()
    if e == superuser_email():
        return False
    row = session.exec(select(AllowedEmail).where(AllowedEmail.email == e)).first()
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ── Per-user feature flags (superuser only — enforced at the router) ───────────


def parse_features(raw: str | None) -> dict[str, bool]:
    """Parse the stored JSON, tolerating garbage; keep only known feature keys."""
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: bool(v) for k, v in data.items() if k in FEATURE_KEYS}


def features_for(session: Session, email: str) -> dict[str, bool]:
    """The full feature map for an email: every known key present, defaulting False.

    The superuser has every feature on (they need no allowlist row)."""
    e = email.strip().lower()
    base = dict.fromkeys(FEATURE_KEYS, False)
    if e == superuser_email():
        return dict.fromkeys(FEATURE_KEYS, True)
    row = session.exec(select(AllowedEmail).where(AllowedEmail.email == e)).first()
    if row is not None:
        base.update(parse_features(row.features))
    return base


def is_feature_enabled(session: Session, user: User, feature: str) -> bool:
    """True if `user` may use `feature`. Any superuser has every feature; otherwise
    the feature must be flagged on in the user's allowlist row."""
    if user.is_superuser:
        return True
    return features_for(session, user.email).get(feature, False)


def set_feature(
    session: Session, email: str, feature: str, enabled: bool
) -> AllowedEmail:
    """Toggle one feature for an allowlisted email. Raises KeyError for an unknown
    feature and LookupError for an email with no allowlist row (the superuser is
    always-on and is not stored here)."""
    if feature not in FEATURE_KEYS:
        raise KeyError(feature)
    e = email.strip().lower()
    row = session.exec(select(AllowedEmail).where(AllowedEmail.email == e)).first()
    if row is None:
        raise LookupError(e)
    flags = parse_features(row.features)
    flags[feature] = bool(enabled)
    row.features = json.dumps(flags)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
