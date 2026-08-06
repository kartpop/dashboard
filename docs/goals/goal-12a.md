# Goal 12a — Dev view: a "Save for later" lane + tabbed, paginated draft lists

**One line:** Give the Dev rail view a third card action (**Save for later**, alongside
**Approve & file** and **Dismiss**) and split the single flat draft list into **four tabs**
— **In review** (the only busy one: newest drafts, *all* of them, revealed by scroll) and
three settled lanes (**Saved for later**, **Filed**, **Dismissed**) that each load a
**bounded first page** and fetch older rows on demand — so the main view stays uncluttered
as filed/dismissed history piles up. **UI + a thin list/paginate API only** — the scan,
synthesis, GitHub filing, cursor, and gating machinery are untouched.

## Why (the friction this closes)

Goal 12 renders every draft the synthesiser ever produced in one column: pending drafts
first, then a dimmed `dev-settled` tail of everything filed or dismissed
([DevView.tsx:107-121](../../frontend/src/dev/DevView.tsx#L107-L121)). Two problems show up
in daily use:

- **The settled tail grows without bound.** Every filed issue and every dismissed card
  stays in the DOM forever, dimmed but present, pushing the handful of drafts I actually
  need to act on further down. `GET /dev` returns *all* drafts in one unpaginated payload
  ([dev.py:88-101](../../backend/app/routers/dev.py#L88-L101),
  `list_drafts`) — it only gets heavier.
- **There is no "not now" that isn't "no".** Today a card is a binary: file it or dismiss
  it. A draft that's real but not-this-week has nowhere to go — dismissing loses it,
  leaving it pending clutters the review lane. I want to **set it aside** without deciding.

The fix is entirely in the Dev surface: a `saved` status + a `POST /dev/{id}/save` flip,
and a re-shaped list API that serves one tab at a time with cursor pagination. Nothing
about how drafts are *produced* changes.

## What ships

- **1. A `saved` draft status (backend, no new table).** `dev_issue_draft.status` gains a
  fourth value `saved` alongside `draft|filed|dismissed`
  ([models.py:37-44](../../backend/app/dev/models.py#L37-L44) — add `SAVED = "saved"`). The
  column is already free-text `max_length=20`, so **no schema migration** is needed; the
  value set widens by convention (mirror it in the `dev.md` status note). A saved draft is
  **still fully actionable** — it carries the same editable title/body/repo/project and the
  same **Approve & file** / **Dismiss** affordances; `saved` is a shelf, not a terminal
  state. Filing or dismissing a saved card moves it to `filed`/`dismissed` exactly as from
  review.
- **2. Two status transitions (service + router).**
  - `service.save_draft(session, user_id, draft_id)` — flip `draft → saved` (idempotent;
    zero GitHub calls, same shape as `dismiss_draft`).
  - `service.unsave_draft` / **Move back to review** — flip `saved → draft`, so a shelved
    card can rejoin the active lane. (Symmetry the owner will want the moment they shelve
    something by mistake; cheap to add now.)
  - New endpoints `POST /dev/{id}/save` and `POST /dev/{id}/unsave`, each behind
    `require_dev_enabled` and `user.id`-scoped like the existing
    `/{id}/dismiss` ([dev.py:164-172](../../backend/app/routers/dev.py#L164-L172)). Both
    return the updated `_draft_out`. **Filing stays the only GitHub write** — save/unsave
    are local status flips (LLM-proposes / code-disposes contract unchanged).
- **3. A tabbed, cursor-paginated list API.** Replace the single unpaginated
  `GET /dev` draft dump with a list endpoint that serves **one tab at a time**:
  - `GET /dev/drafts?status={review|saved|filed|dismissed}&limit=N&cursor=<opaque>` →
    `{ items: DevDraft[], next_cursor: string | null }`. `review` maps to `status = draft`.
    Ordering is **newest activity first** (`updated_at` desc, `id` desc as a stable
    tiebreak); `next_cursor` is an opaque keyset token (encodes the last row's
    `(updated_at, id)`), **not** an offset — no drift as new drafts land mid-scroll.
  - `service.list_drafts` grows `status` / `limit` / `cursor` params (keyset `WHERE
    (updated_at, id) < cursor`), staying `user_id`-scoped. `limit` is clamped
    server-side (default 20, max ~50).
  - `GET /dev` slims to **view metadata only**: `{ last_scan_at, config_complete, counts:
    { review, saved, filed, dismissed } }` — the counts drive the tab badges. The drafts
    themselves come from `/dev/drafts`.
- **4. The tabbed view (frontend).** `DevView` gains a tab bar under the header —
  **In review · Saved for later · Filed · Dismissed** — with a count badge per tab from
  `/dev` metadata. Only the active tab's list is mounted.
  - **In review = load-all-by-scroll.** The review tab auto-fetches the **next page as the
    user nears the bottom** (IntersectionObserver sentinel) and keeps going until
    `next_cursor` is null — effectively "all pending drafts", but streamed in chunks so a
    large backlog never blocks first paint. This is the lane meant to be worked to empty.
  - **Saved / Filed / Dismissed = bounded + explicit "Load older".** Each settled tab loads
    only the **first page** on activation and shows a **Load older** button while
    `next_cursor` is non-null (no auto-infinite-scroll — these are archives you occasionally
    dig into, not worklists). Keeps the DOM and the payload small by default.
  - **Actions per tab:** review + saved cards show the full action row (**Approve & file**,
    **Save for later** *(review only)* / **Move to review** *(saved only)*, **Dismiss**),
    editable inline. Filed cards keep the issue link + "attach project (retry)" partial-state
    control ([DevView.tsx:316-339](../../frontend/src/dev/DevView.tsx#L316-L339)). Dismissed
    cards stay read-only with a **Move to review** escape hatch.
  - **Optimistic flips, no full reload.** Save/unsave/dismiss update the card locally and
    **remove it from the current tab's list** (it now belongs to another tab), decrement/
    increment the relevant count badges, and fire the POST without awaiting — matching the
    existing optimistic-write convention
    ([useDevPanel.ts:75-107](../../frontend/src/dev/useDevPanel.ts#L75-L107)). Filing stays
    non-optimistic (awaits the GitHub write, reloads the card authoritatively). Switching
    tabs re-reads counts so the badges reconcile.
- **5. Hook reshape.** `useDevPanel` moves from one `drafts` array to **per-tab paged
  state**: an active-tab selector, a `{ items, nextCursor, loading }` slice per tab, a
  `loadMore(tab)` that appends the next page, and `save`/`unsave` alongside `file`/`dismiss`.
  A card leaving a tab is dropped from that tab's `items` and, if the destination tab is
  already loaded, prepended there (else just the count bumps and it loads on next visit).

## Out of scope (do not build)

- **Any change to the scan / synthesis / GitHub-filing / cursor / gating machinery.** No new
  LLM behaviour, no new GitHub write, no change to `dev_doc_cursor`, `synth.py`,
  `github.py`, the scheduler, or the `dev` feature flag. This goal only adds a status value,
  two local status flips, list pagination, and the tabbed shell.
- **New tables or a status enum migration.** `status` stays a free-text column; `saved` is a
  convention, not a DB constraint.
- **Bulk actions** (bulk save / bulk dismiss / bulk file) — still one card at a time; filing
  is still one human approve per card (goal-12 "drafts are cheap, filing is sacred" holds).
- **Cross-tab search / filter / sort controls** (by repo, by date range, full-text). Tabs +
  newest-first is the whole information architecture here.
- **Auto-expiring or archiving saved drafts**, reminders, or any scheduled action on a
  `saved` card. It sits until the owner acts.
- **Settings-page relocation of the config drawer** (still g13 territory) and any mobile
  shell work.

## Acceptance criteria

- **Save action:** a review card shows **Save for later**; clicking it flips the draft to
  `saved` with **zero GitHub calls** (unit test with a GitHub spy that raises if touched),
  the card leaves the In-review list, and the Saved-for-later count increments. A saved card
  offers **Approve & file**, **Dismiss**, and **Move to review**; filing a saved card creates
  the issue exactly as filing a review card does (existing `file_draft` path, unchanged).
- **Transitions:** `POST /dev/{id}/save` (`draft→saved`) and `POST /dev/{id}/unsave`
  (`saved→draft`) are idempotent, `user.id`-scoped (a second user gets 404 on the first
  user's draft id), 403 without the `dev` flag, and return the updated draft (endpoint
  tests). Dismiss/file remain reachable from the saved lane.
- **Pagination:** `GET /dev/drafts?status=filed&limit=5` returns at most 5 items newest-first
  with a `next_cursor`; following the cursor returns the next page with **no overlap and no
  gap**, and a draft created between the two page fetches does **not** shift or duplicate rows
  (keyset, not offset — unit test seeding >limit rows). `next_cursor` is null on the last
  page. `status=review` returns only `draft`-status rows.
- **Metadata:** `GET /dev` returns `counts` for all four tabs matching the DB and no longer
  embeds the draft array; `last_scan_at` / `config_complete` are unchanged.
- **In-review load-all:** with a backlog larger than one page, the review tab renders the
  first page immediately and appends subsequent pages on scroll until `next_cursor` is null —
  ending with every `draft`-status row present (frontend test with a mocked paged endpoint).
- **Settled tabs bounded:** Saved/Filed/Dismissed each render only the first page on
  activation and reveal older rows only via **Load older** (no auto-fetch on scroll).
- **Optimistic + counts:** save/unsave/dismiss update the UI and the tab badges without a full
  `/dev/drafts` reload; filing still awaits and reloads the single card. Switching tabs
  reconciles the counts against `/dev`.
- **Posture:** no Alembic migration (status column reused); GitHub write surface and the
  synthesiser input byte-identical to goal 12; `dev.md` status note updated to list `saved`;
  `tsc`, frontend build, and backend tests green.

## Harness upkeep (closing checklist)

- `dev.md`: add `saved` to the `dev_issue_draft.status` set and note the two new local
  flips (save/unsave) as GitHub-write-free — the LLM-proposes/code-disposes and
  filing-is-the-only-write contracts are unchanged, just re-affirmed.
- `verifier-web`: extend the Dev-view checks with the tab bar (four tabs + badges), the
  Save-for-later flip, the In-review infinite scroll, and a settled-tab "Load older".
