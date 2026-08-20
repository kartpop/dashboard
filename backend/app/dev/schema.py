"""Structured-output schema for the synthesising LLM (goal 12).

The LLM *proposes* a de-duplicated set of issue drafts across the whole day's new
entries; deterministic code *disposes* — it validates every proposed repo against the
user's configured catalog (out-of-catalog → the default repo), stores bodies/titles
verbatim as drafts, and never lets the model touch GitHub. Same LLM-proposes /
code-disposes ethos as the router and news. An invalid/absent result degrades to an
empty proposal (no drafts), never a crash.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    """One entry a draft was synthesised from — the provenance the card renders."""

    doc_path: str = Field(
        description="The source Doc path exactly as given in the input, e.g. "
        "'internal/kaapi'."
    )
    entry_ts: str = Field(
        description="The entry timestamp exactly as given in the input for that entry."
    )


class ProposedIssue(BaseModel):
    title: str = Field(description="A concise, imperative GitHub issue title.")
    body_markdown: str = Field(
        description="The issue body in GitHub-flavored markdown, written from the union "
        "of everything said across the cited entries. Include a short context/background "
        "and concrete acceptance criteria or next steps where the notes support them."
    )
    repo: str = Field(
        description="The target repo full name — MUST be one of the repo full names in "
        "the provided catalog. If unsure, pick the closest by the catalog descriptions."
    )
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="Every entry this issue was synthesised from. When the same "
        "underlying work was mentioned in several entries (different Docs and/or "
        "different timestamps), cite them ALL here and emit a SINGLE issue — do not "
        "emit near-duplicate issues.",
    )


class SynthesisResult(BaseModel):
    issues: list[ProposedIssue] = Field(
        default_factory=list,
        description="The de-duplicated set of proposed issues. An entry with no "
        "actionable work contributes to no issue; a topic mentioned repeatedly across "
        "the day yields exactly one issue citing all its mentions.",
    )


# ── Goal 12b: the matcher (drafts vs live GitHub candidates) ──────────────────
#
# Same posture as SynthesisResult: the matcher only PROPOSES (number, type, confidence)
# tuples; code validates every one against the fetched candidate set and takes the
# stored url/title/state from that list, never from the model.


class ProposedMatch(BaseModel):
    """One candidate the model believes covers (part of) a draft."""

    repo: str = Field(
        description="The candidate's repo full name (owner/name), exactly as given "
        "in the input — candidates span all the user's repos and numbers are only "
        "unique within one repo."
    )
    number: int = Field(
        description="The candidate's number, exactly as given in the input."
    )
    type: str = Field(
        description="'issue' or 'pr' — which candidate list the number came from."
    )
    confidence: str = Field(
        description="'high' only when the candidate clearly covers the same underlying "
        "work as the draft; 'medium' for a probable-but-unsure relation."
    )
    reason: str = Field(
        description="One short sentence: why this candidate matches the draft."
    )


class DraftMatches(BaseModel):
    draft_index: int = Field(
        description="The draft's index exactly as given in the input."
    )
    matches: list[ProposedMatch] = Field(
        default_factory=list,
        description="Every existing issue/PR that probably covers this draft's work, "
        "best match first. Empty when nothing plausibly matches — do NOT force one.",
    )


class MatchResult(BaseModel):
    drafts: list[DraftMatches] = Field(
        default_factory=list,
        description="One entry per input draft (a draft with no matches may be "
        "omitted or carry an empty matches list).",
    )


class CommentDraftResult(BaseModel):
    """The comment drafter's verdict on one draft vs its matched issue's thread."""

    has_new_info: bool = Field(
        description="True only if the draft carries information the existing issue "
        "thread lacks (a new reproduction, a fresh occurrence, an extra constraint). "
        "False when the thread already covers everything the draft says."
    )
    comment_markdown: str | None = Field(
        default=None,
        description="When has_new_info: the comment to post, GitHub-flavored markdown "
        "— ONLY the genuinely new information, written to read naturally in the "
        "existing thread. Null when has_new_info is false.",
    )
