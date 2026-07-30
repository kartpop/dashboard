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
