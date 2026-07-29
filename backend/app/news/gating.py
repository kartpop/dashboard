"""News access gating (goal 11).

News is a per-user feature flag the superuser toggles in the admin UI (stored on the
`allowed_email` row alongside the invite list). The superuser always has News on. This
replaces the earlier `NEWS_ENABLED_EMAILS` env var — the flag now lives in the DB and
is edited from the UI, and goal 12 generalises this per-user feature-flag mechanism.
"""

from __future__ import annotations

from sqlmodel import Session

from app.auth import service as auth_svc
from app.auth.models import User

FEATURE = "news"


def is_news_enabled(session: Session, user: User) -> bool:
    """True if News is on for `user`: any superuser, or the `news` flag is set on
    their allowlist row."""
    return auth_svc.is_feature_enabled(session, user, FEATURE)
