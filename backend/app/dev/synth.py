"""The issue synthesiser — the fourth runtime LLM (goal 12).

One batched call per scan per user (**opus** by default — this step is cross-conversation
synthesis, not classification: it must recognise the same latent issue mentioned across
separate meetings/timestamps and merge it into ONE draft). Input is the full set of new
entries across the whole configured subtree since the cursor — every new entry from every
source Doc in one prompt, each tagged with its source doc path + entry timestamp — plus
the user's repo catalog (full name + a short description each) and the titles of still-open
drafts / recently-filed issues as *do-not-redraft* context. Output is a de-duplicated list
of proposed issues, each `{title, body_markdown, repo, sources[]}`.

The prompt builders are **pure** so they unit-test without the API — the guardrail pins
the exact serialized field set: entry text + doc path + entry timestamp + repo catalog +
open-draft titles, and **never** a doc/folder id or a token. The LLM has no GitHub access
and files nothing (LLM-proposes / code-disposes — code validates every proposed repo and
only a human approve triggers a GitHub write).
"""

from __future__ import annotations

import json
import logging

import anthropic

from app.dev import config
from app.dev.schema import CommentDraftResult, MatchResult, SynthesisResult

_log = logging.getLogger("dev.synth")

# The entry payload's ONLY keys — the dev-LLM contract's field set. The guardrail test
# pins this: no doc_id / folder_id / drive id / PAT ever appears here.
ENTRY_FIELDS = ("doc_path", "entry_ts", "one_liner", "keywords", "body")

_SYSTEM = """You are an engineering assistant that turns a day's meeting notes into a \
de-duplicated set of GitHub issue drafts. You are given every new notes ENTRY captured \
across the day (each tagged with the Doc it came from and its timestamp) and a CATALOG \
of the repos issues may target (each with a short description). You ONLY propose issue \
drafts; you take no other action and you have no access to GitHub.

Your one hard job is SYNTHESIS, not transcription:
- The same underlying work is often mentioned in several entries — the same Doc at \
different times, or different Docs (a bug raised in standup and written up again after a \
1:1). Collapse ALL such mentions into a SINGLE issue and cite every entry it came from in \
`sources`. Do NOT emit near-duplicate issues for one piece of work.
- Write each issue's `body_markdown` from the UNION of what was said across the cited \
entries — context first, then concrete acceptance criteria / next steps where the notes \
support them.
- Pick each issue's `repo` from the catalog by matching the work to the repo \
descriptions. If genuinely unsure, pick the closest — code will fall back to the default \
repo if the name is not in the catalog.
- An entry with no actionable engineering work (a pure status update, a social note) \
contributes to NO issue. Quality over coverage: propose only issues worth filing.

You are also given a DO-NOT-REDRAFT list — titles of issues already drafted or filed. Do \
NOT propose an issue that restates one of these; only surface genuinely new work.

Output ONLY the structured list of proposed issues."""


def build_entry_payload(entries: list[dict]) -> list[dict]:
    """Serialize entries to the EXACT contract field set — nothing else.

    Each entry is `{doc_path, entry_ts, one_liner, keywords, body}`. There is no doc id,
    folder id, or token here and there never can be — this is the whole dev-LLM contract,
    asserted by the prompt-builder guardrail test. `entries` items may carry extra keys
    (the service threads doc_id through for its own bookkeeping); only ENTRY_FIELDS are
    serialized."""
    return [{k: entry.get(k) for k in ENTRY_FIELDS} for entry in entries]


def build_prompt(
    entries: list[dict],
    repo_catalog: list[dict],
    do_not_redraft: list[str],
) -> tuple[str, str]:
    """Build (system, user) for the synthesis call. Pure — no I/O.

    `entries` is exactly `build_entry_payload`'s output; `repo_catalog` is
    `[{full_name, description}]`; `do_not_redraft` is a list of titles. No Google data
    beyond the entry text, no ids, no tokens."""
    catalog = [
        {
            "full_name": r.get("full_name"),
            "description": (r.get("description") or "").strip(),
        }
        for r in repo_catalog
    ]
    user = (
        "REPO CATALOG (choose each issue's repo by full_name):\n"
        f"{json.dumps(catalog, ensure_ascii=False)}\n\n"
        "DO-NOT-REDRAFT (titles already drafted or filed — do not restate these):\n"
        f"{json.dumps(do_not_redraft, ensure_ascii=False)}\n\n"
        "NEW ENTRIES (synthesise these into de-duplicated issues):\n"
        f"{json.dumps(entries, ensure_ascii=False)}"
    )
    return _SYSTEM, user


async def synthesise(
    entries: list[dict],
    repo_catalog: list[dict],
    do_not_redraft: list[str],
) -> SynthesisResult | None:
    """Ask the LLM to synthesise the new entries into de-duplicated issue drafts.

    Never raises. Returns a `SynthesisResult` on success — **including an empty one when
    the model legitimately finds no actionable work** — and `None` on *failure* (the call
    errored, or the response was truncated at max_tokens). The distinction matters: the
    caller advances the per-doc cursor only when this returns a result, so a failure leaves
    the entries un-consumed and they are re-scanned next run. A swallowed failure that
    returned an empty result would advance the cursor and silently lose the entries."""
    if not entries:
        return SynthesisResult(issues=[])
    try:
        client = anthropic.AsyncAnthropic()
        system, user = build_prompt(
            build_entry_payload(entries), repo_catalog, do_not_redraft
        )
        # Stream rather than messages.parse: a first-run backlog (many days of notes in
        # one call) emits a large drafts payload, and a big max_tokens on a non-streaming
        # call risks an SDK HTTP timeout. Streaming lifts that ceiling; output_format keeps
        # the same structured-output guarantee (schema enforced, message.parsed_output set).
        async with client.messages.stream(
            model=config.DEV_MODEL,
            max_tokens=config.DEV_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=SynthesisResult,
        ) as stream:
            message = await stream.get_final_message()
        if message.stop_reason == "max_tokens":
            # Output truncated before the JSON closed → failure, not "no drafts". Return
            # None so the caller leaves the cursor unadvanced and the backlog re-scans;
            # bump DEV_MAX_TOKENS (currently %d) and rerun to recover it.
            _log.error(
                "dev synthesis truncated at max_tokens=%d — cursor left unadvanced; "
                "raise DEV_MAX_TOKENS and rescan",
                config.DEV_MAX_TOKENS,
            )
            return None
        return message.parsed_output or SynthesisResult(issues=[])
    except Exception:
        # A JSON/EOF parse error here means the response was truncated mid-payload
        # (raise DEV_MAX_TOKENS, currently %d) — otherwise it's a model/refusal hiccup.
        # Either way this is a failure: return None so the cursor stays put and re-scans.
        _log.exception(
            "dev synthesis call failed; cursor left unadvanced "
            "(if a JSON/EOF parse error, the response was truncated — "
            "raise DEV_MAX_TOKENS, currently %d)",
            config.DEV_MAX_TOKENS,
        )
        return None


# ── Goal 12b: the matcher (LLM call 2) ────────────────────────────────────────
#
# Chunked calls over the unmatched drafts (title + body + tagged repo) vs the typed
# candidates from EVERY catalog repo, each tagged with its repo (12b.1 — the draft's
# repo tag is the synthesiser's guess and may be wrong, so matching is catalog-wide).
# Wide but cheap — MANY candidates, titles/excerpts only, no bodies. The pinned field
# sets below are the whole matcher contract: no doc ids, no tokens, no candidate URLs
# (code re-derives url/title/state from the fetched list by the validated
# (repo, number) pair — ids/URLs that code acts on never come from the model).

DRAFT_MATCH_FIELDS = ("draft_index", "title", "body", "repo")
ISSUE_CANDIDATE_FIELDS = ("repo", "number", "title", "labels")
PR_CANDIDATE_FIELDS = ("repo", "number", "title", "state", "description_excerpt")

_MATCH_SYSTEM = """You are a de-duplication judge for GitHub issue drafts. You are \
given DRAFTS (proposed new issues) and two CANDIDATE lists spanning ALL of the user's \
configured repositories: their OPEN ISSUES and their recent OPEN/MERGED PULL REQUESTS. \
Every candidate is tagged with its `repo`. You have no other GitHub access and take no \
action.

For each draft, decide which candidates (if any) already cover the same underlying \
work:
- An open issue describing the same bug/feature is a match even if worded differently.
- A PR (open or merged) whose title/description says it implements or fixes the \
draft's work is a match.
- A draft's own `repo` field is only the drafting system's GUESS at where it belongs \
and is sometimes wrong — a candidate from a DIFFERENT repo that covers the same work \
is still a match. Same-repo candidates deserve a closer look first, but never limit \
yourself to them.
- Confidence 'high' means you would stake the call on it: same underlying work, not \
merely the same area. 'medium' means probably related. Omit anything weaker — most \
drafts match NOTHING, and an empty matches list is the expected common answer.

Return each match's repo, number and type exactly as given in the input. Output ONLY \
the structured result."""


def build_match_payload(
    drafts: list[dict], issue_candidates: list[dict], pr_candidates: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Serialize matcher inputs to EXACTLY the pinned field sets — nothing else.

    Drafts carry a positional `draft_index` (assigned here) — never the DB id. Rows may
    carry extra keys (html_url, updated_at from the fetch); only the pinned fields are
    serialized, so no URL or timestamp reaches the prompt."""
    d_out = [
        {
            "draft_index": i,
            "title": d.get("title"),
            "body": d.get("body"),
            "repo": d.get("repo"),
        }
        for i, d in enumerate(drafts)
    ]
    i_out = [{k: c.get(k) for k in ISSUE_CANDIDATE_FIELDS} for c in issue_candidates]
    p_out = [{k: c.get(k) for k in PR_CANDIDATE_FIELDS} for c in pr_candidates]
    return d_out, i_out, p_out


def build_match_prompt(
    drafts: list[dict], issue_candidates: list[dict], pr_candidates: list[dict]
) -> tuple[str, str]:
    """Build (system, user) for one match call. Pure — no I/O. Inputs are exactly
    `build_match_payload`'s output."""
    user = (
        "OPEN ISSUES (candidates across the user's repos; match by repo + number, "
        "type 'issue'):\n"
        f"{json.dumps(issue_candidates, ensure_ascii=False)}\n\n"
        "OPEN/MERGED PULL REQUESTS (candidates across the user's repos; match by "
        "repo + number, type 'pr'):\n"
        f"{json.dumps(pr_candidates, ensure_ascii=False)}\n\n"
        "DRAFTS (judge each against ALL the candidates above — the draft's own repo "
        "tag may be wrong):\n"
        f"{json.dumps(drafts, ensure_ascii=False)}"
    )
    return _MATCH_SYSTEM, user


async def match_issues(
    drafts: list[dict], issue_candidates: list[dict], pr_candidates: list[dict]
) -> MatchResult | None:
    """Judge one repo's unmatched drafts against its fetched candidates.

    Never raises. Returns a `MatchResult` on success (empty matches are a real answer)
    and `None` on failure (errored or truncated at max_tokens) — the caller then leaves
    those drafts' `related_issues` NULL so the next scan retries. Streams like the
    synthesiser: the first post-deploy scan matches a 50+ draft backlog in one pass and
    a non-streaming call would truncate (the `5c6b48e` lesson)."""
    if not drafts:
        return MatchResult(drafts=[])
    try:
        client = anthropic.AsyncAnthropic()
        system, user = build_match_prompt(
            *build_match_payload(drafts, issue_candidates, pr_candidates)
        )
        async with client.messages.stream(
            model=config.DEV_MATCH_MODEL,
            max_tokens=config.DEV_MATCH_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=MatchResult,
        ) as stream:
            message = await stream.get_final_message()
        if message.stop_reason == "max_tokens":
            _log.error(
                "dev matcher truncated at max_tokens=%d — drafts left unmatched for "
                "the next scan; raise DEV_MATCH_MAX_TOKENS",
                config.DEV_MATCH_MAX_TOKENS,
            )
            return None
        return message.parsed_output or MatchResult(drafts=[])
    except Exception:
        _log.exception(
            "dev matcher call failed — drafts left unmatched for the next scan"
        )
        return None


# ── Goal 12b: the comment drafter (LLM call 3) ────────────────────────────────
#
# One call per draft whose top ISSUE match is high-confidence: the draft vs that ONE
# issue's whole thread (narrow but deep) — plus, when a PR also matched high, that PR's
# title/description/commit subjects as context. Reuses DEV_MODEL: this text faces
# humans on GitHub.

_COMMENT_SYSTEM = """You are an engineering assistant. A DRAFT issue turned out to \
duplicate an EXISTING GitHub issue, whose body and comment thread you are given (plus, \
sometimes, RELATED PULL REQUESTS with their commit subject lines). Decide whether the \
draft carries information the existing thread does NOT already have — a new \
reproduction, a fresh occurrence, an extra constraint, a sharper acceptance criterion.

- If it does: set has_new_info true and write comment_markdown — ONLY the genuinely \
new information, phrased to read naturally as a comment in that thread (GitHub-flavored \
markdown; you may reference a related PR by number, e.g. "PR #45 appears to cover part \
of this"). Do not restate what the thread already says; do not summarise the draft.
- If everything the draft says is already covered: set has_new_info false and \
comment_markdown null.

You only draft text; a human reviews and files it. Output ONLY the structured result."""


def build_comment_prompt(
    draft: dict, issue_thread: dict, related_prs: list[dict]
) -> tuple[str, str]:
    """Build (system, user) for one comment-draft call. Pure — no I/O.

    `draft` is `{title, body}`; `issue_thread` is `{number, title, body, comments:
    [{author, body, created_at}]}`; `related_prs` is `[{number, title, state,
    description_excerpt, commit_subjects}]`. Comment authors are GitHub logins already
    public on the thread — no member directory is ever included."""
    payload = {
        "draft": {"title": draft.get("title"), "body": draft.get("body")},
        "existing_issue": {
            "number": issue_thread.get("number"),
            "title": issue_thread.get("title"),
            "body": issue_thread.get("body"),
            "comments": [
                {
                    "author": c.get("author"),
                    "body": c.get("body"),
                    "created_at": c.get("created_at"),
                }
                for c in (issue_thread.get("comments") or [])
            ],
        },
        "related_prs": [
            {
                "number": p.get("number"),
                "title": p.get("title"),
                "state": p.get("state"),
                "description_excerpt": p.get("description_excerpt"),
                "commit_subjects": p.get("commit_subjects") or [],
            }
            for p in related_prs
        ],
    }
    user = json.dumps(payload, ensure_ascii=False)
    return _COMMENT_SYSTEM, user


async def draft_comment(
    draft: dict, issue_thread: dict, related_prs: list[dict]
) -> CommentDraftResult | None:
    """Draft the add-to-existing-issue comment (or report nothing-new).

    Never raises; `None` on failure — the caller then leaves the draft an issue draft
    with its links (conversion is best-effort, never blocking)."""
    try:
        client = anthropic.AsyncAnthropic()
        system, user = build_comment_prompt(draft, issue_thread, related_prs)
        async with client.messages.stream(
            model=config.DEV_MODEL,
            max_tokens=config.DEV_COMMENT_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=CommentDraftResult,
        ) as stream:
            message = await stream.get_final_message()
        if message.stop_reason == "max_tokens":
            _log.error(
                "dev comment drafter truncated at max_tokens=%d — draft left as an "
                "issue draft; raise DEV_COMMENT_MAX_TOKENS",
                config.DEV_COMMENT_MAX_TOKENS,
            )
            return None
        return message.parsed_output
    except Exception:
        _log.exception("dev comment-draft call failed — draft left as an issue draft")
        return None
