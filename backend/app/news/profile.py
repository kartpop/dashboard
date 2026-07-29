"""The news profile doc + the weekly feedback-driven rewrite (goal 11).

The profile is one human-readable markdown blob per user (hand-editable). The curator
reads it; a weekly job rewrites it from the accumulated 👍/👎 + comments, retaining the
one previous version so a bad rewrite is one revert away. Comments carry far more signal
than the thumbs and are first-class input.

Like the curator, the rewrite prompt sees ONLY public feed metadata (item titles) and
the profile — never Google data. The prompt builder (`build_rewrite_prompt`) is pure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

from app.news import config

_log = logging.getLogger("news.profile")

# What the curator falls back to when a user has never written/earned a profile.
DEFAULT_PROFILE = """# News interests

I care about substantive developments in:

- **Frontier AI models** — capability jumps, new model releases, research that moves
  the frontier (not incremental benchmark noise or product PR).
- **Science** — fundamental results, especially physics, biology, and math.
- **Technology** — genuinely new engineering, tools, and systems.

I prefer depth over hype and concrete results over speculation. Surprise me
occasionally with something outside these interests.
"""


def effective_profile(profile_md: str) -> str:
    """The profile the curator actually reads — the stored one, or the default when
    the user has none yet."""
    return profile_md.strip() or DEFAULT_PROFILE


@dataclass
class FeedbackRecord:
    """One item's feedback for the rewrite prompt: the title (public metadata), the
    vote, and the free-text comment (the high-signal part)."""

    title: str
    vote: int  # +1 / -1 / 0
    comment: str | None = None


_SYSTEM = """You maintain a personal news-interest profile — a short markdown document \
describing what a reader wants to see in their daily curated feed. You are given the \
CURRENT profile and a week of FEEDBACK: for each item the reader saw, their vote \
(up/down) and any comment. Rewrite the profile to better reflect this feedback.

- Comments are the strongest signal — weight them heavily (e.g. "too incremental — I \
only care about capability jumps" should sharpen the profile toward capability jumps).
- Thumbs are weaker, aggregate signal: consistent down-votes on a theme mean less of it.
- Keep it human-readable markdown, concise, and PRESERVE the reader's stable interests \
— evolve the profile, do not rewrite it from scratch or drop long-standing topics on \
one week of noise.
- Keep any "surprise me / serendipity" preference intact.

Output ONLY the rewritten markdown profile document."""


def build_rewrite_prompt(
    current_profile: str, feedback: list[FeedbackRecord]
) -> tuple[str, str]:
    """Build (system, user) for the weekly rewrite. Pure — no I/O. Only titles + votes
    + comments enter the prompt (public feed metadata), never Google data."""
    lines = []
    for f in feedback:
        vote = {1: "👍", -1: "👎"}.get(f.vote, "·")
        comment = (
            f' — comment: "{f.comment.strip()}"' if (f.comment or "").strip() else ""
        )
        lines.append(f"- [{vote}] {f.title}{comment}")
    feedback_block = "\n".join(lines) if lines else "(no feedback this week)"
    user = (
        "CURRENT PROFILE (markdown):\n"
        f"{effective_profile(current_profile)}\n\n"
        f"FEEDBACK (this week):\n{feedback_block}"
    )
    return _SYSTEM, user


async def rewrite(current_profile: str, feedback: list[FeedbackRecord]) -> str | None:
    """Return the rewritten profile markdown, or None on any failure / no feedback
    (caller keeps the current profile — a failed rewrite never clobbers it)."""
    if not feedback:
        return None
    try:
        client = anthropic.AsyncAnthropic()
        system, user = build_rewrite_prompt(current_profile, feedback)
        resp = await client.messages.create(
            model=config.NEWS_MODEL,
            max_tokens=config.NEWS_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
        return text or None
    except Exception:
        _log.exception("news profile rewrite failed; keeping current profile")
        return None
