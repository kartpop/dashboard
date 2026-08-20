---
paths: ["backend/app/dev/**", "backend/app/routers/dev.py", "frontend/src/dev/**"]
---

# Dev view safety (goal 12; tabs + pagination in 12a; live-GitHub dedup in 12b)

The Dev feature turns meeting-notes Docs into **de-duplicated GitHub issue drafts** the
owner reviews and files. It adds the **fourth** runtime LLM (the issue **synthesiser**),
the app's **first Docs read path**, and its **first non-Google write surface** (GitHub).
Goal 12b adds two more bounded LLM steps (the issue **matcher** and the comment
**drafter**), the app's **first GitHub reads**, and a **third sanctioned GitHub write**
(commenting on an existing issue instead of filing a duplicate). Read this before
editing anything under `backend/app/dev/`. It follows the same LLM-proposes /
code-disposes ethos as the router and news — and it is a **workflow, not an agent**: no
LLM step ever calls GitHub or chooses what to fetch next (decision + revisit triggers
recorded in `docs/goals/goal-12b.md`).

## The hard contract (what the synthesiser LLM may see)

- **Entry text + repo catalog + open-draft titles — never an id, never a token.** The
  synthesiser input is exactly `[{doc_path, entry_ts, one_liner, keywords, body}]` +
  `[{full_name, description}]` (the repo catalog) + a list of do-not-redraft titles
  (`synth.build_entry_payload` / `build_prompt`; the field set is pinned by
  `ENTRY_FIELDS` and a prompt-builder test). A doc/folder **drive id** or the **PAT**
  never appears in the prompt — the service threads `doc_id` alongside for its own
  provenance bookkeeping, but `build_entry_payload` serializes only `ENTRY_FIELDS`.
- **No GitHub access in any prompt, no filing by the model.** `synth.py` imports the
  Anthropic SDK, never `dev.github`. It returns a `SynthesisResult` (proposed issues)
  and nothing else. A model error/refusal returns an empty result and the scan stores
  no drafts (never a crash); because the cursor only advances after drafts persist, the
  same entries are simply re-scanned next run.
- **Opus by default, env-configurable.** `config.DEV_MODEL` — this step is
  cross-conversation synthesis (merge the same latent issue mentioned across separate
  meetings/timestamps into one draft), not per-entry classification. Drop it to a cheaper
  model via `DEV_MODEL` only if it proves good enough in practice.

## The 12b LLM steps (matcher + comment drafter — both in `synth.py`)

- **Matcher** (`match_issues`, `DEV_MATCH_MODEL` default sonnet, streams with its own
  `DEV_MATCH_MAX_TOKENS` budget — the `5c6b48e` truncation lesson): runs **after**
  `_dispose_synthesis`, **catalog-wide (12b.1)** — candidates come from EVERY
  configured repo, each tagged with its `repo`, because the draft's own repo tag is
  the synthesiser's guess and is sometimes wrong (prod mis-tags matched nothing under
  per-repo scoping). The candidate fetch is all-or-nothing per scan (one repo's
  transient failure aborts the phase so a true match is never silently missed; a
  missing PAT only excludes that repo). Drafts are **chunked
  `DEV_MATCH_DRAFT_CHUNK` (20) per call** (candidates repeated, matches merged
  code-side) — the first prod backlog put 78 drafts in one call and truncated at 16k
  output, matching nothing; a failed chunk skips only itself. Its payload is EXACTLY the pinned field sets — drafts as
  `DRAFT_MATCH_FIELDS` (`draft_index`, `title`, `body` — a positional index, never the
  DB id), issue candidates as `ISSUE_CANDIDATE_FIELDS` (`number`, `title`, `labels`),
  PR candidates as `PR_CANDIDATE_FIELDS` (`number`, `title`, `state`,
  `description_excerpt`, pre-truncated code-side) — **no candidate URLs, no doc ids, no
  tokens, no member logins** (pinned-fields test, `ENTRY_FIELDS` pattern).
- **Matcher scope: the whole unfiled backlog, once per draft.** Every non-settled draft
  (`draft`/`saved`) whose `related_issues` IS NULL, whatever scan synthesised it;
  filed/dismissed never; already-matched (even `"[]"`) never again (the NULL-guard).
  Changing a draft's repo KEEPS `related_issues` (12b.1: matches are judged against
  the whole catalog, so re-targeting doesn't invalidate them — and clearing would
  never re-match under the NULL-guard).
- **Matcher dispose:** every returned `(repo, number, type)` is validated against the
  fetched candidate set — out-of-set entries (wrong repo included) are dropped — and
  the stored `related_issues` repo/url/title/type/state come from the **code-fetched
  candidate list keyed by the validated (repo, number), never from LLM output**. The
  `**Related:** #N, PR #M (merged)` body line is appended by code from validated
  matches only; a match outside the draft's repo uses GitHub's cross-repo
  `owner/repo#N` form.
- **Comment drafter** (`draft_comment`, reuses `DEV_MODEL` — this text faces humans on
  GitHub; own `DEV_COMMENT_MAX_TOKENS` budget): runs only for a draft whose top
  **issue** match is `high` confidence. Input: the draft + that ONE issue's body and
  comment thread (+ a high-matched PR's title/description/commit subjects). Output
  `{has_new_info, comment_markdown}` → code converts the draft to `kind=comment` (body
  replaced by the comment markdown — explicit owner sign-off — target set from the
  validated match, project cleared, and **`repo` re-tagged to the target issue's repo**
  when the match is cross-repo: the comment lives with the issue, which also heals a
  synthesiser mis-tag; tokens route by the match's owner) or flags the match
  `nothing_new: true` and leaves an issue draft for the **human** to dismiss — never
  auto-dismiss.
- **PRs are never comment targets.** A PR-only match stays a linked issue draft;
  commenting targets issues exclusively.
- **Steps are best-effort.** Any GitHub-read/matcher/drafter failure leaves plain issue
  drafts (`related_issues` NULL retries next scan), never blocks the scan or the
  cursor, and is reported in the tally (`linked` / `converted` / `matching_skipped`).

## LLM-proposes / code-disposes

- **The synthesiser only proposes.** Code validates every proposed `repo` against the
  configured catalog (`service._dispose_synthesis`): an out-of-catalog repo falls back
  to the default repo (never filed to an unconfigured repo). Titles/bodies are stored
  **verbatim as drafts**; nothing is filed until a human approves a specific card.
- **Filing is deterministic + human-gated.** A GitHub write happens ONLY on
  `POST /dev/{id}/file` → `service.file_draft`. There is no bulk-approve and no auto-file
  from the cron path. `file_draft` is **partial-state idempotent**: the issue number/url
  is recorded the moment `github.create_issue` succeeds, so a failed project-attach
  retries **only** the GraphQL `add_issue_to_project` step — an issue is never
  double-created. Failures raise `ApiError` (rollback-not-blind-retry); the card shows
  the error / "attach pending".
- **`kind` semantics (12b) + the third write.** `dev_issue_draft.kind` is a free-text
  convention like `status`: `issue` (default) files through the two-step path above;
  `comment` — a confirmed duplicate carrying new info — files as **one**
  `github.create_issue_comment` on its `target_issue_number` (no `create_issue`, no
  project attach, no partial-state dance; success flips to `filed` storing the comment
  URL in the existing `issue_url`/`issue_number` columns, failure leaves the draft
  untouched for a retry; an `issue_url` already present is never re-posted). The write
  set is exactly {create issue, attach to project, comment on issue} — and **comments
  are the only mutation the app ever applies to a pre-existing GitHub object**: never a
  PATCH of an existing issue's body/labels/state/assignees, never a comment on a PR. A
  comment draft's target is fixed — re-targeting means dismissing (repo change → 409).
- **Every non-filing transition touches nothing.** `dev_issue_draft.status` is
  `draft|saved|filed|dismissed` — a **convention on a free-text column**, not a DB enum
  (goal 12a widened it to include `saved` with **no migration**). Three of the four
  transitions are local status flips with **zero GitHub calls** (each unit-tested with a
  spy that raises if any GitHub function is called): **dismiss** (`→ dismissed`),
  **save** (`draft → saved` — the "not now" that isn't "no"), and **unsave** /
  *Move to review* (`saved|dismissed → draft`, the escape hatch). Filing remains the one
  and only GitHub write; `saved` is a **shelf, not a terminal state**, so a shelved card
  stays editable and files through the unchanged `file_draft` path.
- **The draft list is paged, one lane at a time.** `GET /dev` carries view metadata +
  per-lane counts only; `GET /dev/drafts?status=…&limit=…&cursor=…` serves one tab, newest
  activity first, with an **opaque keyset cursor** over `(updated_at, id)` — never an
  offset, so a draft landing mid-scroll can neither shift nor duplicate a row. `limit` is
  clamped server-side. Every query stays `user_id`-scoped.

## The Docs read path (the app's first)

- **`docs.get_document` is `documents.get`-only, read-only.** It reads app-created Docs
  for the entry scan. **No scope change** — `drive.file` grants read on files the app
  created, and every Doc the app can reach is app-created. The AST test pins it as a read
  (`test_docs_get_document_is_read_only`: `documents().get` inside `_get_document`, and
  **no** `batchUpdate`/`create`/`update`/`delete` in it). The Docs **write** surface
  stays insert-only and byte-identical to goal 10 — no new mutation types.
- **The parser keys off the goal-10 entry shape.** `parser.py` splits a Doc into entries
  by **H3 one-liner → H4 timestamp → optional H5 keywords → body → delimiter** — the
  **timestamp is the H4 line** (goal-10 flipped goal-9's order). Timestamps parse back
  from `writes.service.format_note_heading`'s exact format (`6-July-2026, 8:41 PM IST`).
  Correctness is by parsed timestamp, never document position (Docs are newest-first;
  descending order is only an early-exit hint).

## The cursor (DB-only, no Doc marker)

- **`dev_doc_cursor` is forward-only, process-once, and lives entirely in the DB.** Per
  `(user, doc)`: the newest processed entry timestamp + the boundary keys (entries at
  exactly that minute already consumed — timestamps are minute-granular and batch pastes
  share one). A scan keeps entries strictly newer than the cursor **plus** same-minute
  entries whose key is not in `boundary_entry_keys`. **Never write a marker into the Doc**
  — a moving marker is delete+insert (breaks the insert-only invariant) and is
  human-deletable. The cursor **advances only after drafts persist** (a crash between the
  LLM and the store re-scans, losing nothing).

## The GitHub read surface (12b — the app's first GitHub reads)

Six read methods in `github.py`, all per-owner-PAT routed, all deterministic code:

- **Candidate fetches** (scan-time, per repo with unmatched drafts):
  `list_open_issues` (`state=open&sort=updated`, capped `DEV_ISSUE_FETCH_CAP` — open
  state deliberately not a recency window; **filters out the PR rows** the issues
  endpoint interleaves via their `pull_request` key) and `list_recent_prs`
  (`state=all&sort=updated`, capped `DEV_PR_FETCH_CAP`, keeps open + merged, skips
  closed-unmerged; the list response carries the body, excerpted code-side — zero
  per-PR calls).
- **Thread fetches** (drafter stage, only for a high-confidence match): `get_issue` +
  `list_issue_comments` (newest ~50) for the ONE matched issue;
  `list_pr_commit_subjects` (first lines only, ~30) for the ONE high-matched PR —
  **never during candidate fetch** (call-count spy-tested). The PR read ceiling is
  description + commit subject lines: never diffs, patch bodies, full messages, review
  threads, file contents, or CI status.
- **`list_assignees`** — the repo's assignable users, serving
  `GET /dev/config/members?repo=` for the @-mention typeahead. **Member logins are
  never fed to any LLM** (synthesiser, matcher, drafter — pinned-fields tests); a
  mention exists only if the owner typed or picked it, inserted as plain `@login` text
  (GitHub linkifies on filing — no new write surface, no auto-assign).
- All reads are **best-effort in the scan**: a failure degrades that repo to "no match
  info" — drafts persist, the cursor advances, the tally reports the skip.

## Config + secrets

- **Tokens are the key, one per resource owner; repos/projects are fetched, never
  hand-typed.** A fine-grained PAT (Issues R/W, org Projects R/W, Metadata read) is
  **bound to a single GitHub resource owner** at mint time, so filing into a personal
  account *and* an org needs one token each. Tokens live in the `dev_pat` table keyed by
  `(user_id, owner)` — **not** in `dev_config` (the legacy single `pat_encrypted` column
  is migrated into `dev_pat` and nulled). Each is stored **Fernet-encrypted** (same
  `app.google.auth._fernet` used for the Google refresh token), never logged, never
  echoed back — the config GET returns only `tokens: [{owner, hint, login}]` (masked).
  The owner(s) a token covers are **derived from the repos it can see**
  (`add_token` receives them from `github.list_repos`), never hand-typed. Filing routes
  by the target repo's owner (`service.get_pat_for_owner`); a repo whose owner has no
  stored token cannot be filed (`no_token_for_owner`). "Refresh repos" unions
  `github.list_repos` across **all** tokens; ProjectsV2 projects come from
  `github.list_projects_for_repo` using the owner's token. Doc/folder ids come from the
  hierarchy index, **never** from LLM output.
- **The repo description is the only hand-typed repo field** — it feeds the synthesiser's
  repo pick, so it stays human-authored. Labels/milestones/assignees are the same
  metadata-fetch layer extended (out of scope in v1 — repo + project only).

## Access gating (UI *and* cron)

Dev is a **per-user feature flag on the `allowed_email.features` JSON column** — the
goal-11 mechanism verbatim (`auth.service.FEATURES` gained `("dev", "Dev")`; **any
superuser is always on**). The flag gates **three** surfaces: (a) no flag → no Dev rail
entry (`/auth/me` reports `dev_enabled`); (b) every `/dev/*` endpoint 403s
(`require_dev_enabled`); (c) **the scheduled scan skips unflagged users** —
`gating.is_dev_enabled` is checked inside the cron's per-user loop **before** any creds
load, Doc read, or opus token, exactly like `news.scheduler`'s `is_news_enabled`. The
scan also skips users with incomplete config (no PAT / no source / no repo) — the flag is
the switch that keeps the opus call from firing for users who don't use the feature.

## Layering

- `parser.py` — pure Docs-payload → entries (timestamps, keys). No Google call, no LLM.
- `synth.py` — the runtime LLMs (structured output): the synthesiser, the 12b matcher
  and the comment drafter. No DB, no writes, never imports `dev.github`; prompt
  builders pure + unit-tested for the exact field sets (no ids/tokens/URLs/logins).
- `github.py` — the thin GitHub client (httpx): `validate_pat`, `list_repos`,
  `list_projects_for_repo`, the six 12b reads above, and the three writes —
  `create_issue` (REST), `add_issue_to_project` (GraphQL), `create_issue_comment`
  (REST). One concern per function, no orchestration, PAT passed explicitly, never
  logged.
- `service.py` — deterministic dispose: resolve sources, cursor-scoped read, batch,
  validate + store drafts, advance cursor, own config, and file on human approve. Every
  query `user_id`-scoped (goal 8).
- `scheduler.py` — in-process asyncio loop (same pattern as news/router), end-of-day
  daily, gated per-user on the `dev` flag + config-complete before any creds/Doc/LLM work.
- `routers/dev.py` — HTTP surface, every endpoint behind `require_dev_enabled`.
