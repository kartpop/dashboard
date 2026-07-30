"""Dev-view access gating (goal 12).

Dev is a per-user feature flag the superuser toggles in the admin UI (stored on the
`allowed_email.features` JSON column — the goal-11 mechanism verbatim). The superuser
always has Dev on. The flag gates **three** surfaces: the rail entry, every `/dev/*`
endpoint, and — crucially for cost — the scheduled scan's per-user loop, so an
unflagged user is never read from Docs and never sent to the (opus) model. This mirrors
`news.gating.is_news_enabled` exactly.
"""

from __future__ import annotations

from sqlmodel import Session

from app.auth import service as auth_svc
from app.auth.models import User

FEATURE = "dev"


def is_dev_enabled(session: Session, user: User) -> bool:
    """True if Dev is on for `user`: any superuser, or the `dev` flag is set on their
    allowlist row."""
    return auth_svc.is_feature_enabled(session, user, FEATURE)
