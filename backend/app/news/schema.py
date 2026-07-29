"""Structured-output schema for the news curator (goal 11).

The curator LLM *proposes* which candidate ids to surface + a one-line why + a coarse
domain label; deterministic code *disposes* (validates every id against the candidate
set, caps the count, code-random serendipity). Same LLM-proposes/code-disposes ethos
as the router. An invalid/absent result degrades to a code-only recency fallback,
never a crash.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Domain = Literal["frontier-models", "science", "technology", "other"]


class CurationPick(BaseModel):
    id: str = Field(
        description="The candidate id to surface — MUST be one of the ids provided."
    )
    why: str = Field(
        description="One short line on why this matters to the user, grounded in their "
        "profile. A single sentence, not a summary of the article."
    )
    domain: Domain = Field(description="Coarse topic label for the item.")


class CurationResult(BaseModel):
    picks: list[CurationPick] = Field(
        default_factory=list,
        description="The selected items, best-first. Only ids from the candidate list.",
    )
