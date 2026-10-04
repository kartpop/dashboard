# Goal 15 — Mobile layout: Home and Dev on a phone browser

**One line:** Make the dashboard usable in a phone browser (≤640px wide). Home gets a mobile shell:
a bottom bar instead of the nav rail, a header with the day and an agenda row, and **one section
at a time behind tabs** (My Tasks · Follow-ups · Threads · Scratch). Task and thread actions move
into bottom sheets. The Dev view gets the same pass. Desktop and the 641–1080px stacked layout do
not change. **Frontend only.** There are no API, schema, or write-surface changes.

**Prototype:** `goal-15-prototype.html`. Open it in a browser; the phone mock is interactive. It is
the visual spec for this goal. Where the prose here and the prototype disagree, the prose wins.

## Why (the friction this closes)

The owner opens the prod dashboard on a phone, and today it's unusable:

- The nav rail takes ~90 of 390px, so every panel is squeezed into the rest.
- Task titles cut off after 4–5 characters. The per-row date wraps to three lines ("7 / Oct / 202").
- The calendar strip renders as an empty white card.
- Thread rows: the step text gets one character, and the pills and `+1` run past the edge.
- Scratchpad: the placeholder, RECENT, the notes, and the Capture button draw on top of each other.
- The ≤1080px stacked layout gives each panel `min-height: 60vh`, so you scroll through four tall
  boxes to find anything.

The ≤1080px fallback was only ever "stack the desktop columns". Nobody designed it for a phone.

## How it's built (the approach)

**Breakpoint:** one constant, `MOBILE_MAX = 640` (px), in a new leaf module
`frontend/src/useIsMobile.ts`. The CSS uses the same number in `@media (max-width: 640px)`. Keep
the two in sync and put a comment on both.

**Two tools, each with its own job:**

1. **CSS media queries** handle sizing, spacing, wrapping, hiding, and font size. If a change is
   only "the same DOM, styled differently", it's CSS. This is the default.
2. **`useIsMobile()`** handles structural differences, where the mobile DOM is a different shape:
   bottom bar vs rail, tabs vs grid, bottom sheet vs anchored popover, agenda row vs hour axis. It
   is `useSyncExternalStore` over `window.matchMedia('(max-width: 640px)')`, so it re-renders when
   the phone rotates or a desktop window is resized across the line. Use it **only** at the points
   where the tree differs. Don't scatter `isMobile ? … : …` through leaf styling.

**Keep every tab mounted.** Switching tabs toggles the `hidden` attribute on four `.m-pane`
wrappers. It does not unmount the panes. This is the same reasoning as the rail's `.view-pane`
(goal 11). An unsent scratchpad draft, an expanded thread, a half-typed add-task, and the panels'
scroll positions all survive a tab switch. Tasks and threads state are already lifted to
`DashboardPage`, so the data side is unaffected either way.

**Shared components, new layout shells.** The panels keep their hooks and row data. Mobile adds a
handful of **new presentational components** and reuses everything else:

| New (mobile-only) | Lives in | Replaces on mobile |
|---|---|---|
| `BottomBar` | `AppShell.tsx` | the nav rail |
| `MeSheet` | `AppShell.tsx` | the rail's bottom group (settings, avatar, sign-out) |
| `MobileHome` (header + tabs + 4 panes) | `DashboardPage.tsx` | `PinnedTasksRow`'s grid |
| `AgendaRow` | `panels/calendar/` | `CalendarStrip`'s axis (same `useCalendarStrip` hook) |
| `Sheet` (generic bottom sheet) | `frontend/src/Sheet.tsx` (shared leaf, like `api.ts`) | anchored popovers / menus |
| `QuickCaptureSheet` | `panels/scratch/` | — (new entry point to the existing capture flow) |

`Sheet` is a leaf utility with no panel knowledge, so panels may import it. That's the same
allowance `frontend.md` already gives `api.ts` and `formatDate.ts`. Panels still don't import each
other.

**Touch-specific rules** (these apply to everything in this goal):

- Tap targets are at least **44×44px**: checkboxes, ⋯ buttons, tabs, and bottom-bar items.
- **Inputs and textareas use font-size ≥16px on mobile.** iOS Safari zooms the page on focus when
  they're smaller, and doesn't zoom back out.
- There's no hover on a phone, so anything only reachable by hover (title tooltips, the calendar
  hover card, hover-revealed buttons) needs a tap path or is dropped on mobile. Use
  `@media (hover: none)` where it's a CSS concern.
- Safe areas: add `viewport-fit=cover` to the viewport meta in `index.html`. The bottom bar pads
  by `env(safe-area-inset-bottom)`, and fixed toasts sit above the bar.
- Heights use `dvh`, not `vh`, so the browser's own chrome doesn't hide the bottom of the page.

## What ships

### 1. Shell: bottom bar + Me sheet (`AppShell.tsx`)

- **≤640px:** the rail is not rendered. A fixed **bottom bar** shows **Home · News · + · Dev ·
  Me**. News and Dev appear **only when their per-user flag is on**, the same rule as the rail.
  The **+** is always there.
- **+** opens `QuickCaptureSheet` from **any** view (§6).
- **Me** opens `MeSheet`: the avatar and email, **Settings** (opens the existing settings modal),
  and **Sign out**.
- Views stay mounted exactly as today (`.view-pane`). Only the navigation chrome changes.
- **Settings modal on mobile:** full-screen. Its side-nav becomes a horizontal, scrollable tab row
  at the top. Content is a single column. It needs no new behaviour, only to fit.

### 2. Home header (`MobileHome`)

The header is sticky at the top of the Home scroll and stays in view while a list scrolls. It has
three rows:

1. **Brand mark · ‹ day › · refresh.**
   - The day nav uses the same viewed-date state as the calendar strip (prev/next day, and tapping
     the date jumps back to Today).
   - Refresh reloads the **active tab's** data.
2. **Agenda row** (`AgendaRow`), fed by `useCalendarStrip`, which is reused unchanged:
   - one card per timed event (time + title, ellipsized), in a horizontally scrolling row with
     scroll-snap;
   - past events dimmed;
   - a red **now** marker between past and upcoming events, on today only;
   - the next upcoming event highlighted with a "Next ·" prefix;
   - on load, the row scrolls so the now marker is near the left edge;
   - tapping a card opens a `Sheet` with the event details (time range, attendees, location) and a
     **Join Meet** button when there's a Meet link. This replaces the desktop hover card and
     click-to-join;
   - when the viewed day has no timed events, a single muted "No events" card.
3. **Tabs:** **My Tasks *n* · Follow-ups *n* · Threads *n* · Scratch**. Counts are open items,
   from the same state the panels use. The active tab is ephemeral and starts at My Tasks.

**Find and fix the root cause** of the empty calendar card in the current ≤1080px layout. The
641–1080px range still uses `CalendarStrip`, so it has to render there too.

### 3. Tasks tabs (My Tasks, Follow-ups)

These reuse the pinned-list rendering, with mobile row and list rules:

- **No drag on mobile** (locked). Don't pass the pointer/keyboard sensors, or mark the sortables
  `disabled`, under `isMobile`. The drag handle is not rendered. Reordering, cross-list drag, and
  group editing are desktop-only.
- **Groups render read-only.** Existing groups show as today. The `+ group` affordance and
  group-delete are hidden on mobile.
- **Row:** a 44px checkbox column; the **title takes the full remaining width, wrapping to at most
  2 lines** (not ellipsized to one); the thread chip, when there is one, on a meta line under the
  title; and a single **⋯** button at the right. The per-row date icon is not rendered, since the
  bucket header carries the date.
- **Tapping the title or ⋯** opens the **task sheet** (`Sheet`):
  - the title in full, with the list name and thread under it;
  - reschedule chips: **Today · Tomorrow · Next week · Pick date…** ("Pick date" opens the
    existing picker, or a native `<input type="date">` if the custom picker doesn't fit). These
    all call the existing reschedule path;
  - **Move to list…** (the existing move menu as a sub-list in the sheet);
  - **Edit** (title + notes, the existing edit write);
  - **Open thread** (only when linked: switches to the Threads tab and focuses the thread via the
    existing `requestThread(id, "focus")`);
  - **Delete** (the existing deferred delete with its undo toast).

  These are the desktop row's actions with nothing new, so the write surface is unchanged.
- **Add-task row** sits at the top of the list as a full-width input, 16px text, with its date
  picker.
- Overdue/Today/Tomorrow bucket headers keep their tint, full width.
- No `min-height: 60vh`, no inner scroll box. The page scrolls.
- **Toasts** (complete-undo, delete-undo, thread coupling "What's next?") keep their behaviour and
  are positioned above the bottom bar, full width minus the 16px gutters.

### 4. Threads tab

- **Section header:** the title "Threads" and a **+ thread** button. The list/graph view toggle is
  **hidden** on mobile, which always uses the compact list. The panel's refresh moves to the Home
  header (§2).
- **Filter chips:** one row with horizontal scroll (no wrapping). The counts are unchanged.
- **Compact row**, two lines:
  1. caret · **title** (ellipsized) · age · ⋯
  2. ring · the **soonest open step's text in full** (wraps), then on its own line the whose-move
     pill ("My task · Tomorrow") and "+N step(s)" when there are more open steps.
  
  A dangling thread shows its "Needs next step" cue in line 2 instead.
- **Expanded row:** a **vertical** list, not the desktop horizontal track. It shows:
  - the open steps, each with its ring, text, and pill;
  - the last 3 done steps (struck, muted) with the existing "+N earlier" fold;
  - then **+ next step** and **+ log update** buttons that open the existing inline forms,
    full width.
- **Popovers become sheets:** the step popover (Mark done, Delete/unlink, open task), the thread
  ⋯ menu (rename, archive, delete), and the "What's next?" chooser all render inside `Sheet` on
  mobile. Their content and actions are the same. Only the container changes.
- **Coupling:** the toast's **Show thread** / **Set next step** actions switch to the Threads tab
  before focusing the thread.

### 5. Scratch tab

- A plain vertical flow: **editor → Capture button → Review queue (when it has items) → Recent**.
  Nothing is absolutely positioned or overlapping.
- Sized like the desktop column (owner call, 2026-10-04): the tab fills the screen between the
  sticky header and the bottom bar, **Recent rests at its header + 2 rows and scrolls the rest**,
  and the editor takes all the space above it (it scrolls internally; the page doesn't grow). It
  uses a monospace font at 16px.
  Bullet ergonomics (Enter continues, Tab indent) work as today; Tab on a phone keyboard is
  usually absent, which is fine.
- **Capture** is a full-width button under the editor. The existing deferred-capture undo toast is
  unchanged.
- The editor/RECENT drag split is not rendered on mobile.
- Recent rows: one line, ellipsized, with the state badge. The copy button stays and is a 44px
  target.

### 6. Quick capture (`QuickCaptureSheet`)

- The bottom-bar **+** opens a sheet with a textarea (autofocused, 16px) and a **Capture** button.
- It goes through **the same capture path** as the Scratch tab: `POST /scratch`, inline routing,
  and the deferred undo toast. It is not a second implementation. Lift `useScratchPanel` (or the
  slice of it that captures) high enough that the sheet and the Scratch tab share one state, so
  the Scratch tab's Recent shows the new entry at once. If lifting it to `AppShell` is too
  invasive, a refetch when the Scratch tab becomes visible is acceptable. Note which one was
  chosen in `frontend.md`.
- Closing the sheet with text in it keeps the text for the next open (the same draft-survival
  idea as the mounted panes).

### 7. Dev view

Dev already has tabs, so this section is CSS plus sheet conversions:

- The header stacks: the title on one row; last-run meta, config, and scan on the next row,
  wrapping.
- The lane tabs (`.dev-tabs`) scroll horizontally on one row.
- Draft cards are full width. Their edit forms are single-column. The textareas and inputs are
  16px. The action buttons wrap and are ≥44px tall.
- Any anchored popover in Dev (config, repo/project pickers, mention suggestions in
  `MentionTextarea`) either fits on screen or renders in `Sheet`. Pick per case, with the rule
  "nothing off-screen, nothing under the bottom bar".
- Infinite-scroll/load-more keeps working inside the page scroll.

### 8. News (fit only)

News is not redesigned in this goal. Its acceptance bar is: **no horizontal page overflow** and
**nothing hidden under the bottom bar** at 390px. A real News mobile pass is a later goal.

## Locked decisions (2026-10-04)

- **Tabs**, not stacked sections, for Home on mobile.
- **No drag on mobile.** No reordering, cross-list drag, or group editing. Groups render
  read-only.
- **No swipe gestures** (swipe-to-complete / swipe-to-tomorrow) in this goal.
- **Scope: Home + Dev**, plus the shell and the settings modal. News is fit-only.
- **Breakpoint 640px.** 641–1080px keeps today's stacked layout with the rail. >1080px is
  untouched.
- **Mobile web only.** No PWA manifest, service worker, or install prompt.
- **No new actions.** Every sheet exposes actions that already exist on desktop. The write
  surface, API, and schema are unchanged.

## Out of scope (do not build)

- Swipe gestures, long-press menus, pull-to-refresh.
- Touch drag (dnd-kit `TouchSensor`) and any mobile reorder UI.
- The threads graph view on mobile.
- A News redesign.
- A tablet-specific layout (641–1080px) beyond fixing the empty calendar card.
- PWA / offline / home-screen install.
- Persisting the active tab across reloads.

## Acceptance criteria

**Verify with Playwright at mobile viewports**, through the `verifier` subagent (`/verify`). Use
**390×844** (primary) and **360×780** (small Android), light and dark.

**No horizontal overflow:** on Home (each of the 4 tabs), Dev (each lane), News, and the settings
modal, `document.documentElement.scrollWidth <= window.innerWidth`.

**Shell:**
- The rail is absent at ≤640px, and the bottom bar is present with only the flag-enabled views.
- Me → Settings opens the modal full-screen. Sign out works.
- **+** opens quick capture from Home, Dev, and News.

**Home header:**
- The agenda row shows today's timed events, with past events dimmed and a now marker.
- Tapping an event opens its details sheet, with Join Meet when there's a Meet link.
- Day ‹ › changes the viewed day.
- Tab counts match the lists.

**Tasks:**
- Long titles wrap to 2 lines. No date is drawn in the row. There's no drag handle.
- Checkbox complete → undo toast above the bar → Undo restores the task.
- The ⋯ sheet's Tomorrow chip reschedules the task, and Move to list… moves it. Run both against
  `zz-verifier-test` per `verifier-writes`, with cleanup.
- Open thread switches to the Threads tab with that thread focused.

**Threads:**
- Two-line compact rows, with the full step text visible (no 1-character truncation).
- The filter chips scroll on one row.
- Expanding a row shows the vertical step list. A step's sheet offers Mark done.

**Scratch:**
- Nothing overlaps (check the bounding boxes of the editor, Capture, and Recent don't intersect).
- Recent shows 2 rows and scrolls; the editor fills the space above it; typing many lines doesn't
  make the page scroll.
- Capture via the Scratch tab and via quick capture both land in Recent.
- A draft survives switching tabs and back.

**Dev:** the tabs scroll, the cards are full width, and an edit form opens and saves.

**iOS zoom guard:** every `input`, `textarea`, and `select` reachable on mobile has a computed
`font-size >= 16px`.

**Desktop regression:**
- Screenshots of Home and Dev at **1440×900** and **1024×768** match today's layout: the rail,
  resizable grid, drag, and hover cards all work.
- Drag-reorder at 1440 still produces exactly one PATCH.

**Owner check on a real phone:** after deploy, open prod on the phone, then do a pass over the
four tabs, quick capture, and Dev.

**Regression:** `tsc`, the frontend build, and the frontend unit tests pass. Backend tests are
untouched.

## Harness upkeep

- `.claude/rules/frontend.md`: add a **Mobile layout (goal 15)** entry covering
  - the 640 breakpoint and the `useIsMobile` / CSS split;
  - mounted tab panes;
  - `Sheet` as a shared leaf;
  - no drag on mobile;
  - the 16px input rule and 44px targets;
  - how quick capture shares state.
- `.claude/rules/threads.md` and `tasks-panel.md`: one line each on what renders differently
  under `isMobile`.
- `.claude/rules/dev.md`: the mobile notes for the Dev view.
- The verifier recipe (`verifier-web` skill): add the mobile viewport sizes and the
  no-overflow + 16px-input checks as reusable steps.
- Add the goal-15 line to `docs/goals/README.md`.
- No owner steps file. The deploy is the usual one.
