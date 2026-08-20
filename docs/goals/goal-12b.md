# Goal 12b — Dev view: dedup against live GitHub — link matching issues & PRs, comment instead of duplicating

**One line:** Before a draft becomes a new GitHub issue, check it against the issues **and
pull requests** that **already exist** in its target repo: probable matches (issue or PR,
open or recently merged) render as **links on the draft card** and as a code-appended
`Related:` line in the draft body, and when an **issue** match is confirmed and the draft
carries information the existing issue lacks, the draft is **converted into a comment
draft** targeting that issue — **Approve & file** then posts a comment on the existing
issue instead of creating a duplicate. Built as a
**workflow, not an agent** (decision recorded below): two new bounded LLM steps (issue
**matcher**, comment **drafter**) between deterministic fetch and dispose code — the LLM
still never calls GitHub and never sees the token.

## The pipeline at a glance

One scan = the existing g12 steps plus a match-and-convert tail. LLM calls are numbered;
everything else is deterministic code:

1. **Read (code, existing):** cron / Create-now reads each source Doc, cursor-filters to
   new entries only.
2. **Synthesise (LLM call 1, existing, unchanged):** ONE batched call over all new entries
   → draft issues `{title, body, repo, sources}`.
3. **Dispose (code, existing):** validate repo picks against the catalog, persist drafts.
   *The repo is only final here — which is why issue fetching comes after synthesis, not
   before.*
4. **Fetch candidates (code, new):** for each repo that has unmatched **non-settled**
   drafts — this scan's newborns **plus anything lingering in review / saved-for-later**
   from earlier scans (dismissed and filed are skipped) — fetch two candidate lists: its
   **open issues** (number + title + labels, capped at the ~200 most recently updated —
   open state, deliberately **not** a recency window: a six-month-old open issue is
   exactly the duplicate that matters) and its **open + merged PRs** (number + title +
   state + a truncated description excerpt, all free from the one list call;
   closed-unmerged skipped, capped). No issue bodies, no commits fetched yet.
5. **Match (LLM call 2, new; one per repo):** that repo's unmatched drafts (title + body)
   vs its typed candidates → per draft, `matches: [{number, type, confidence, reason}]`.
   Numbers validated code-side against the fetched set; links stored on the draft and a
   deterministic `Related: #N …` line appended to the draft body (code, not LLM).
6. **Fetch the thread (code, new):** only for a high-confidence **issue** match — that ONE
   issue's body + comments — plus, if a PR also matched high, that ONE PR's **commit
   subject lines** (first line of each message; never diffs or file contents).
7. **Draft the comment (LLM call 3, new; one per converted draft):** draft + issue thread
   (+ matched-PR context) → `{has_new_info, comment_markdown}` → code converts the draft
   to `kind=comment`, or flags it nothing-new and leaves it an issue draft. **Only issues
   are ever comment targets** — a PR-only match stays a linked issue draft.

Steps 4–7 are best-effort: any failure leaves plain issue drafts and never blocks the scan
or the cursor.

**Why three calls, not one:** each judgment needs different context at a different width.
The matcher needs MANY candidates but only their titles (cheap, wide); the comment drafter
needs ONE issue but its whole thread (expensive, narrow). A single do-everything call would
need every candidate's full body in the prompt across every catalog repo — and the
synthesis call already had to move to streaming because its output was truncating on big
backlogs (`5c6b48e`); folding matching + comment prose into that same call re-opens the
wound. The funnel keeps each prompt small and each output schema single-purpose.

## Why (the friction this closes)

Goal 12's dedup is entirely app-local: the synthesiser merges repeated mentions within a
batch, and the do-not-redraft list feeds it *titles of the app's own drafts*
([service.py](../../backend/app/dev/service.py) `_do_not_redraft_titles`, cap 60). Nothing
checks what **actually exists on GitHub**:

- Issues filed **outside the app** — by hand, by a teammate, or before g12 shipped — are
  invisible. The synthesiser happily proposes them again, and the human either files a
  duplicate or dismisses and loses the fresh detail in the draft body.
- When the human *does* recognize a duplicate at review time, the card offers only
  file-anyway or dismiss. There is **no "add this to the existing issue"**. New reproduction
  detail, a fresh occurrence, an extra constraint — all of it dies with the dismissed card.

## Agent or workflow? (decision, recorded)

**Workflow.** The control flow is fully known before any LLM runs: *fetch candidate issues →
judge matches → fetch the matched thread → draft a comment → dispose*. Every tool call is
predictable from the previous step's output; the LLM contributes exactly two judgments
(match? / what's missing?), both expressible as structured output over inputs code fetched.
An agent earns its keep only when the model must *choose what to look at next* based on what
it just found — that isn't this. Three repo-specific reasons reinforce it:

- **The posture holds.** The g12 contract — the LLM never calls GitHub, never sees doc ids
  or the PAT, LLM-proposes/code-disposes — survives untouched. An agent holding GitHub
  tools would be the first breach of that line, for no capability we need.
- **Scale doesn't demand search.** Catalog repos are the owner's own projects; open-issue
  counts fit in a capped fetch (most-recently-updated first). Agentic query refinement
  solves a large-corpus problem we don't have.
- **Deterministic = testable.** Both steps spy-test and eval like the router and
  synthesiser; an agent loop is variable in cost, latency, and behaviour.

**Revisit-as-agent triggers** (any one makes an agentic matcher a real 12x candidate):
match quality degrades once a repo outgrows the fetch cap; matching starts to require
following cross-references ("duplicate of #123", linked PRs, project boards); or comment
drafting needs to consult long threads selectively rather than reading them whole.

## What ships

- **1. GitHub read path (`github.py` — the app's first GitHub reads).** Raw `httpx`
  like the rest of the module, per-owner PAT routing via `get_pat_for_owner`:
  - `list_open_issues(token, owner, repo)` — REST `GET /repos/{o}/{r}/issues?state=open&
    sort=updated`, paginated up to a cap (`DEV_ISSUE_FETCH_CAP`, default 200). **Filter out
    pull requests** (the endpoint returns PRs with a `pull_request` key — a known gotcha;
    PRs come from their own fetch below, which carries the fields we actually want).
    Keep `{number, title, labels, html_url, updated_at}` only.
  - `list_recent_prs(token, owner, repo)` — REST `GET /repos/{o}/{r}/pulls?state=all&
    sort=updated`, paginated up to `DEV_PR_FETCH_CAP` (default 100), keeping **open and
    merged** (`merged_at` set) PRs and skipping closed-unmerged (abandoned). Keep
    `{number, title, state (open|merged), description_excerpt (PR body truncated
    code-side, ~400 chars), html_url, updated_at}` — the list response already carries
    the body, so PR candidates cost **zero** per-PR calls.
  - `get_issue(token, owner, repo, number)` — title, body, state, html_url.
  - `list_issue_comments(token, owner, repo, number)` — `[{author, body, created_at}]`,
    capped (~50 newest).
  - `list_pr_commit_subjects(token, owner, repo, number)` — `GET /pulls/{n}/commits`,
    **subject lines only** (first line of each commit message, cap ~30). Called **only for
    a matched PR** at the drafter stage — never during candidate fetch — and the app never
    fetches diffs, patch bodies, review threads, or file contents.
  - `list_assignees(token, owner, repo)` — `GET /repos/{o}/{r}/assignees`, the users who
    can be assigned issues in that repo (≈ mentionable collaborators). `[{login, name?}]`,
    capped (~100). Serves the @-mention typeahead (item 8) — **never** fed to any LLM.
  - **Best-effort in the scan:** a failed issue fetch degrades that repo to "no match info"
    — drafts still persist, the cursor still advances, the scan tally says matching was
    skipped. A GitHub read failure must never block synthesis output.
- **2. Matcher step (new LLM call, in `synth.py` — LLM-only module contract holds).**
  `match_issues(drafts, candidates)` — env `DEV_MATCH_MODEL` (default `claude-sonnet-5`),
  structured output. Runs **after** `_dispose_synthesis` (the repo is only final
  post-dispose), batched **one call per repo** that has unmatched drafts. Input per draft:
  title + body; input per candidate: issues as number + title + labels, PRs as number +
  title + state + description excerpt — no URLs, no token, no doc ids. Output: per draft,
  `matches: [{number, type: issue|pr, confidence: high|medium, reason}]`.
  - **Dispose:** every returned `(number, type)` is validated against the fetched
    candidate set — out-of-set entries are dropped. The stored `related_issues` JSON
    (`[{number, type, state, url, title, confidence, reason}]`) takes **url, title, type,
    and state from the code-fetched candidate list keyed by validated number, never from
    LLM output** (house rule: ids/URLs that code acts on never come from the model).
  - **`Related:` line (code, not LLM):** after dispose, a deterministic
    `**Related:** #123, PR #45 (merged)` line is appended to the draft body from the
    validated matches — so the links land inside the filed issue or comment itself
    (GitHub auto-links `#N`), not just on the card. Appended once, editable like the rest
    of the body.
  - **Matcher scope — the whole unfiled backlog, not just this scan's output:** the
    matcher targets every **non-settled** draft (`draft` and `saved`) whose
    `related_issues` is still NULL, regardless of when it was synthesised; dismissed and
    filed drafts are never matched. This is deliberate: the deployed review lane holds
    **50+ lingering drafts**, most parked precisely because some version already exists on
    GitHub — the **first scan after 12b deploys processes that entire backlog**, linking or
    converting each one, so clearing it becomes a pass of dismiss-clicks instead of
    hand-deduping. (Side benefit: draining the backlog un-saturates the
    `DO_NOT_REDRAFT_LIMIT=60` title list, restoring g12's cross-scan dedup headroom.)
    Matching is once per draft (NULL-guard) — no re-match of already-linked drafts each
    scan.
  - **Sized for the backlog:** the matcher call **streams** like the synthesiser (the g12
    `5c6b48e` truncation lesson) with its own output budget env `DEV_MATCH_MAX_TOKENS`
    (default 16000). Input size has no API knob — it's governed by `DEV_ISSUE_FETCH_CAP`
    (candidates per repo) and the unmatched-draft count; if a first backlog run ever
    overflows the context window, raise nothing — chunk the drafts across calls per repo
    (candidates repeated, matches merged code-side).
- **3. Comment conversion (second LLM step, also `synth.py`).** For a draft whose top
  **issue** match is `high` confidence (PRs are never comment targets — a PR-only match
  stays a linked issue draft; see out of scope): code fetches the issue body + comment
  thread — plus, if a PR also matched high, that PR's title / description / commit subject
  lines — then `draft_comment(draft, issue_thread, related_prs)` — model reuses `DEV_MODEL`
  (opus; this text faces humans), output budget env `DEV_COMMENT_MAX_TOKENS` (default
  8000) — returns `{has_new_info: bool, comment_markdown: str | null}` (the comment may
  reference the PR, e.g. "PR #45 appears to cover part of this").
  - `has_new_info` → the draft mutates to **`kind = "comment"`**: `target_issue_number` /
    `target_issue_url` set from the validated match, `body` replaced by the comment
    markdown (fully editable in the card), project preselect cleared. Title is kept for
    display and for the do-not-redraft list.
  - Not → the draft stays `kind = "issue"` with its links plus a stored
    `nothing_new: true` flag on the top match — the card says "covered by #N — nothing new
    to add" and the **human dismisses; never auto-dismiss** ("drafts are cheap, filing is
    sacred" holds).
- **4. Schema (one Alembic migration).** `dev_issue_draft` gains `kind` (free-text, default
  `"issue"`), `target_issue_number` (int, nullable), `target_issue_url` (nullable),
  `related_issues` (JSON, nullable). Filed comment drafts reuse the existing `issue_url` /
  `issue_number` columns (comment html_url / target issue number) — no extra columns.
  Statuses and the 12a tab machinery are untouched: comment drafts flow through
  review/saved/filed/dismissed exactly like issue drafts.
- **5. Filing branch (`service.file_draft` — the third sanctioned GitHub write).**
  `kind == "comment"` → one `POST /repos/{o}/{r}/issues/{n}/comments`
  (`github.create_issue_comment`), **no** `create_issue`, **no** project attach, no
  partial-state dance (single write: success flips to `filed` with the comment URL; failure
  leaves the draft untouched for a retry click). `kind == "issue"` → the existing two-step
  path, byte-identical. Filing remains the **only** GitHub mutation path, and comments are
  the **only** mutation ever applied to a pre-existing GitHub object — the app never edits
  an existing issue's body, labels, or state.
- **6. UI (DevView / DraftCard).**
  - Comment cards get a header badge — **"Comment on {repo}#{n} ↗"** linking to the issue —
    an editable comment body, and **hidden repo/project dropdowns** (the target is fixed;
    re-targeting a comment means dismissing it).
  - Issue cards with matches render a **"Similar: #123 title ↗ · PR #45 (merged) ↗"** line
    under the sources line — PR matches carry a type badge and their open/merged state,
    each entry linking out; the `nothing_new` variant states it plainly.
  - **The Similar line is the clickable pre-file affordance.** The card body stays a plain
    editable textarea (no markdown preview in v1), so the `Related: #N` line *in the body*
    is not clickable on the card — `#N` auto-links only once filed on GitHub. The Similar
    line renders the **same validated matches as real anchors** (`target="_blank"`), so
    every related issue/PR is one click away *before* deciding to file. Comment cards keep
    the Similar line too (secondary matches, e.g. the matched PR) alongside their target
    badge.
  - **Changing the repo on an issue draft clears `related_issues`** (they're stale); no
    live re-match (out of scope).
  - The scan tally (`scanTally.ts`) grows `linked` / `converted` counts — e.g. "5 drafts
    (2 linked to existing issues, 1 converted to a comment)"; a skipped match phase is
    reported, not hidden.
- **7. @-mentions from the card.** Tag teammates in a draft's body before filing:
  - **Members endpoint:** `GET /dev/config/members?repo=` (behind `require_dev_enabled`,
    per-owner PAT routing — the `GET /dev/config/projects?repo=` pattern) returns the
    repo's assignable logins via `list_assignees`. Fetched lazily when the editor first
    needs it, cached in-memory per repo for the session; a PAT that can't list assignees
    degrades to an **empty list** (typeahead offers nothing — typing `@login` by hand
    still works, it's just text).
  - **Typeahead in the body editor:** typing `@` in the card's body textarea opens a small
    filtered dropdown of logins; picking one inserts plain `@login` text at the caret.
    That's the whole mechanism — **mentions are plain text**; GitHub linkifies and
    notifies when the issue/comment is filed. No new write surface, no change to
    `file_draft`.
  - **Humans mention, LLMs don't:** member logins are **never** included in any LLM
    payload (synthesiser, matcher, drafter — pinned-fields tests extend), so no LLM step
    can ping a real person; a mention exists only if the owner typed or picked it.
  - **No auto-assign:** mentioning ≠ assigning. The issue `assignees` API field stays
    unused (out of scope).
- **8. Contracts.** `dev.md` gains the GitHub read surface, both new LLM steps' input
  pinning (payload fields listed, like `ENTRY_FIELDS`), the `kind` semantics, and the third
  write. The goals README spanning constraint adds the matcher + drafter to the runtime-LLM
  set. The synthesiser prompt stays **byte-identical to 12a** — prevention-at-synthesis is
  deliberately deferred (see out of scope).

## Post-ship amendments (12b.1, 2026-08-20 — from the first prod run)

Two field findings changed the shipped behaviour; `dev.md` and
`architecture/dev-pipeline.md` carry the authoritative descriptions:

1. **Matcher calls are chunked** (`DEV_MATCH_DRAFT_CHUNK`, default 20 drafts/call,
   candidates repeated, matches merged code-side): the 78-draft backlog in one call
   truncated at `DEV_MATCH_MAX_TOKENS` and matched nothing. A failed chunk skips only
   itself.
2. **Matching is catalog-wide, not per-repo**: the synthesiser sometimes tags the wrong
   repo (out-of-catalog picks fall back to the default), and per-repo matching judged
   those drafts against a repo whose issues could never match. Candidates now come from
   every catalog repo (tagged with `repo`; validation keys on `(repo, number, type)`),
   the `Related:` line uses `owner/repo#N` for cross-repo matches, conversion re-tags
   the draft to the target issue's repo, and a repo change no longer clears
   `related_issues`. (This supersedes the acceptance line "changing repo clears
   matches".)

## Out of scope (do not build)

- **Anything agentic.** No LLM tool use, no `gh` CLI, no LLM-issued GitHub calls — the
  decision above stands until a listed trigger fires.
- **Prevention-at-synthesis** — feeding live issue titles into the synthesiser prompt. The
  post-dispose matcher is enforced and keeps the batch prompt lean; revisit only if
  duplicate *drafts* (pre-match) get noisy.
- **Closed issues.** Matching is against open state only; "this was closed and is
  recurring" (reopen/regression semantics) is its own future decision.
- **Editing existing issues** — no PATCH of body/labels/state/assignees, ever.
- **Commenting on PRs.** Issues are the only comment targets; a PR match is link + drafter
  context only. (A PR review thread is a different surface with different norms — revisit
  only if daily use asks for it.)
- **PR contents beyond metadata.** Description + commit subject lines are the ceiling —
  never diffs, patch bodies, full commit messages, review threads, file contents, or CI
  status.
- **Assigning, labels, milestones** — mentioning is text-only; the `assignees` field and
  the rest of the issue-metadata surface stay unused (unchanged from g12's out-of-scope).
- **Team mentions (`@org/team`)** — listing org teams needs org-level permissions the PAT
  may not have; usernames only in v1.
- **Live re-match** on repo change or a "recheck" button; matches are computed at scan time.
- **Cross-repo matching / transfer suggestions**, comment-thread sync-back after filing,
  label/assignee suggestions, bulk actions (all still out, per 12/12a).

## Acceptance criteria

- **Reads:** `list_open_issues` filters out PRs and caps at `DEV_ISSUE_FETCH_CAP`
  most-recently-updated; `list_recent_prs` keeps open + merged, skips closed-unmerged, and
  caps at `DEV_PR_FETCH_CAP`; both route the token per owner. Commit subjects are fetched
  **only** for a matched PR — candidate fetching makes zero `/pulls/{n}/commits` calls
  (call-count spy). A scan where a GitHub read raises still persists all drafts (matches
  NULL), still advances the cursor, and the tally reports matching as skipped (unit test
  with a raising GitHub stub).
- **Matcher dispose:** LLM-returned `(number, type)` pairs outside the candidate set are
  dropped; `related_issues` urls/titles/types provably come from the fetched candidate
  list, not model output (test with a fake matcher returning a bogus number + bogus url);
  the `Related:` body line is built code-side from validated matches only. The matcher
  payload contains exactly the pinned fields (PR descriptions pre-truncated) — no doc ids,
  no tokens, no candidate URLs (pinned-fields test, `ENTRY_FIELDS` pattern).
- **Conversion:** a high-confidence **issue** match with `has_new_info` yields
  `kind=comment` with target set, body replaced, project cleared; a draft whose only high
  match is a **PR** stays `kind=issue` — linked, never converted (test); `nothing_new`
  leaves `kind=issue` flagged;
  nothing is ever auto-dismissed. Non-settled pre-existing drafts (review + saved) with
  NULL `related_issues` get matched on the next scan; filed/dismissed never do; an
  already-linked draft is not re-matched. The matcher call streams and honours
  `DEV_MATCH_MAX_TOKENS` (a large seeded backlog completes without truncation — stub
  test, `5c6b48e` pattern).
- **Filing:** filing a comment draft makes exactly one call to the comments endpoint and
  **zero** calls to `create_issue` / `addProjectV2ItemById` (GitHub-spy test); success
  stores the comment URL and flips to `filed`; failure leaves status `draft`. 403 without
  the `dev` flag; cross-user draft id → 404. Issue-kind filing is byte-identical to 12a.
- **UI:** comment cards show the target badge + link and hide repo/project controls; issue
  cards render the similar-issues links as real anchors (new tab) — every stored match is
  clickable on the card before filing; changing repo clears matches; the tally line shows
  linked/converted counts (pure-function tests, `draftTabs.ts` style).
- **Mentions:** `GET /dev/config/members?repo=` is dev-flag-gated, routes the token per
  owner, and returns logins from the assignees endpoint; a permission failure yields an
  empty list, not an error. The typeahead inserts plain `@login` text (frontend test);
  filing a body containing mentions goes through the existing write path unchanged. No
  LLM payload contains member logins (pinned-fields tests extended).
- **Posture:** synthesiser prompt and input payload byte-identical to 12a; cursor logic
  untouched; migration applies cleanly up and down; `tsc`, frontend build, and backend
  tests green.

## Harness upkeep (closing checklist)

- `dev.md`: GitHub **read** surface (six methods, caps, the issue-vs-PR candidate split,
  commit-subjects-only-post-match, member-logins-never-to-LLMs), matcher + drafter
  contracts (models, pinned payloads, dispose rules), `kind` semantics, the third write,
  and the "comments are the only mutation of pre-existing objects" line.
- Goals `README.md`: mark 12b done in the ladder; confirm the runtime-LLM-set amendment
  reads true against the shipped code.
- `verifier-web`: Dev-view checks for the similar-issues line, the comment-card badge +
  hidden dropdowns, and the extended tally line.
- Owner steps: none expected — the fine-grained PAT's `issues:write` already covers issue
  reads and comment writes. If a real PAT proves otherwise, write
  `goal-12b-owner-steps.md`.
