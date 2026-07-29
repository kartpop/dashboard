"""The news curator — the news runtime LLM (goal 11).

One batched call per user per day. Input is the deduped candidate list as
`[{id, title, synopsis, source, published_at}]` — the `synopsis` being the capped
feed-provided summary, the ONLY text beyond the title the LLM ever sees — plus the
user's profile doc. **No Google data ever enters this prompt; no article body is ever
fetched or serialized.** Output is a set of ids + a one-line why + a coarse domain.

LLM-proposes / code-disposes (same ethos as the router): this module only builds the
prompt and calls Anthropic. Every returned id is validated against the candidate set
by the caller (`service`), and the serendipity slots are code-random, not LLM picks —
the escape hatch from the profile can never itself be captured by the profile.

The prompt builders (`build_candidate_payload`, `build_prompt`) are pure so they
unit-test without the API — the guardrail asserts the exact serialized field set and
that the synopsis is length-capped.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import anthropic

from app.news import config
from app.news.schema import CurationResult

if TYPE_CHECKING:
    from app.news.models import NewsItem

_log = logging.getLogger("news.curator")

# The candidate payload's ONLY keys — the news-LLM contract's field set. The guardrail
# test pins this: no full-article-body / fetched-content field is ever serialized.
CANDIDATE_FIELDS = ("id", "title", "synopsis", "source", "published_at")

_SYSTEM = """You are a personal news curator. You are given a list of candidate news \
items — each with a title and, when the source provided one, a short synopsis — and a \
markdown profile describing what the reader cares about. You SELECT which items are \
worth the reader's time and say why in one line. You ONLY select and label; you take \
no other action.

You see ONLY the title and the feed's own short synopsis. You never see the full \
article. The synopsis (when present) is the publisher's own standfirst — trust it over \
a catchy headline: a title can be clickbait, the synopsis usually reflects the content.

Selection guidance:
- Pick roughly {picks} items that best match the profile — capability jumps and \
substantive developments over incremental noise; concrete results over hype.
- Prefer variety across topics; do not fill every slot with one theme.
- For each pick give a `why`: one short, specific sentence on why THIS reader should \
care, grounded in their profile. Not a summary of the article.
- Label each pick's `domain`: "frontier-models" (frontier AI model / capability \
developments), "science", "technology", or "other".

Rules:
- Select ONLY from the candidate ids provided. Never invent an id.
- Return the picks best-first.

Output ONLY the structured selection."""


def _published_str(item: "NewsItem") -> str | None:
    return item.published_at.isoformat() if item.published_at else None


def build_candidate_payload(items: list["NewsItem"]) -> list[dict]:
    """Serialize candidates to the EXACT contract field set — nothing else.

    Only `{id, title, synopsis, source, published_at}` per item. `synopsis` is the
    already-capped feed summary (capped at ingest, `ingest.capture_synopsis`); there is
    no article-body field here and there never can be — this is the whole news-LLM
    contract, asserted by the prompt-builder guardrail test."""
    return [
        {
            "id": str(item.id),
            "title": item.title,
            "synopsis": item.synopsis,
            "source": item.source,
            "published_at": _published_str(item),
        }
        for item in items
    ]


def build_prompt(candidates: list[dict], profile: str) -> tuple[str, str]:
    """Build (system, user) for the curation call. Pure — no I/O.

    The user message carries the profile + the candidate JSON. No Google data, no
    fetched body: `candidates` is exactly `build_candidate_payload`'s output."""
    system = _SYSTEM.format(picks=config.CURATION_PICKS)
    profile_text = profile.strip() or (
        "(no profile yet — infer broad interest in science, technology, and "
        "frontier AI model developments)"
    )
    user = (
        f"READER PROFILE (markdown):\n{profile_text}\n\n"
        f"CANDIDATES (JSON, choose ~{config.CURATION_PICKS} by id):\n"
        f"{json.dumps(candidates, ensure_ascii=False)}"
    )
    return system, user


async def curate(items: list["NewsItem"], profile: str) -> CurationResult:
    """Ask the LLM to select ids. Returns an empty result on any failure (never
    raises) — the caller then falls back to a code-only recency selection so the feed
    is never empty because the model hiccupped."""
    if not items:
        return CurationResult(picks=[])
    try:
        client = anthropic.AsyncAnthropic()
        system, user = build_prompt(build_candidate_payload(items), profile)
        resp = await client.messages.parse(
            model=config.NEWS_MODEL,
            max_tokens=config.NEWS_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=CurationResult,
        )
        return resp.parsed_output or CurationResult(picks=[])
    except Exception:
        _log.exception("news curation call failed; falling back to recency selection")
        return CurationResult(picks=[])
