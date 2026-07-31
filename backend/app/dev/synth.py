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
from app.dev.schema import SynthesisResult

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
