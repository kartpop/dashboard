/**
 * The Dev view's tab + paging state machine (goal 12a) — pure, so the paging contract
 * (no overlap, no gap, drain-to-the-end, cards moving between lanes) is unit-testable
 * without a DOM. `useDevPanel` owns the fetching; every state transition lives here.
 *
 * One flat draft list became four lanes because the settled tail grew without bound.
 * Each lane holds its own page cursor: the review lane is drained to empty by scroll,
 * the three settled lanes stop after their first page until the owner asks for older.
 */

export interface DraftSource {
  doc_path: string;
  entry_ts: string;
}

export type DraftStatus = "draft" | "saved" | "filed" | "dismissed";

export type DraftKind = "issue" | "comment";

/**
 * One validated match against live GitHub (goal 12b). Everything here except
 * `confidence`/`reason` came from the code-fetched candidate list (never the LLM), so
 * the card can safely render `url` as a real anchor.
 */
export interface RelatedMatch {
  number: number;
  type: "issue" | "pr";
  state: string;
  url: string;
  title: string;
  confidence: "high" | "medium";
  reason: string;
  /** The drafter judged the existing issue already covers everything the draft says. */
  nothing_new?: boolean;
}

export interface DevDraft {
  id: number;
  title: string;
  body: string;
  repo: string;
  status: DraftStatus;
  /** goal 12b: `comment` files as one comment on `target_issue_number`. */
  kind: DraftKind;
  target_issue_number: number | null;
  target_issue_url: string | null;
  /** null = not yet matched against GitHub; [] = matched, nothing similar found. */
  related_issues: RelatedMatch[] | null;
  sources: DraftSource[];
  project_node_id: string | null;
  project_title: string | null;
  issue_url: string | null;
  issue_number: number | null;
  project_attached: boolean;
  created_at: string;
}

/** One page of a lane, straight off `GET /dev/drafts`. */
export interface DraftPage {
  items: DevDraft[];
  next_cursor: string | null;
}

export type TabKey = "review" | "saved" | "filed" | "dismissed";

export const TABS: { key: TabKey; label: string }[] = [
  { key: "review", label: "In review" },
  { key: "saved", label: "Saved for later" },
  { key: "filed", label: "Filed" },
  { key: "dismissed", label: "Dismissed" },
];

/**
 * `review` is the only lane that auto-fetches to the end (it is meant to be worked to
 * empty); the settled lanes are archives you occasionally dig into, so they stay at one
 * page until "Load older" is clicked.
 */
export const AUTO_LOAD_TAB: TabKey = "review";

export type Counts = Record<TabKey, number>;

export interface TabState {
  items: DevDraft[];
  /** Keyset token for the next page; null = this lane is fully loaded. */
  nextCursor: string | null;
  loading: boolean;
  /** Whether the first page has been fetched (tabs load lazily, on activation). */
  loaded: boolean;
}

export type TabsState = Record<TabKey, TabState>;

export const ZERO_COUNTS: Counts = {
  review: 0,
  saved: 0,
  filed: 0,
  dismissed: 0,
};

export function emptyTabs(): TabsState {
  return {
    review: emptyTab(),
    saved: emptyTab(),
    filed: emptyTab(),
    dismissed: emptyTab(),
  };
}

function emptyTab(): TabState {
  return { items: [], nextCursor: null, loading: false, loaded: false };
}

/** Which lane a card belongs in, given its status. */
export function tabForStatus(status: DraftStatus): TabKey {
  return status === "draft" ? "review" : status;
}

function patchTab(
  state: TabsState,
  tab: TabKey,
  patch: Partial<TabState>,
): TabsState {
  return { ...state, [tab]: { ...state[tab], ...patch } };
}

export function setLoading(
  state: TabsState,
  tab: TabKey,
  loading: boolean,
): TabsState {
  return patchTab(state, tab, { loading });
}

/**
 * Append a fetched page to a lane. `reset` starts the lane over (first page / after a
 * scan). Ids already present are skipped so a double-fired fetch can never duplicate a
 * card — the keyset cursor makes that unlikely, an optimistic move makes it possible.
 */
export function appendPage(
  state: TabsState,
  tab: TabKey,
  page: DraftPage,
  reset = false,
): TabsState {
  const base = reset ? [] : state[tab].items;
  const seen = new Set(base.map((d) => d.id));
  const items = [...base, ...page.items.filter((d) => !seen.has(d.id))];
  return patchTab(state, tab, {
    items,
    nextCursor: page.next_cursor,
    loading: false,
    loaded: true,
  });
}

/** Drop a lane back to its unloaded state, so the next activation refetches page one. */
export function resetTab(state: TabsState, tab: TabKey): TabsState {
  return { ...state, [tab]: emptyTab() };
}

/** Swap in an authoritative version of a card without moving it between lanes. */
export function replaceDraft(
  state: TabsState,
  tab: TabKey,
  draft: DevDraft,
): TabsState {
  return patchTab(state, tab, {
    items: state[tab].items.map((d) => (d.id === draft.id ? draft : d)),
  });
}

/** Apply a partial edit to a card wherever it currently sits. */
export function patchDraftEverywhere(
  state: TabsState,
  id: number,
  patch: Partial<DevDraft>,
): TabsState {
  const next = { ...state };
  for (const { key } of TABS) {
    if (state[key].items.some((d) => d.id === id)) {
      next[key] = {
        ...state[key],
        items: state[key].items.map((d) =>
          d.id === id ? { ...d, ...patch } : d,
        ),
      };
    }
  }
  return next;
}

/**
 * Move a card to the lane its new status puts it in: drop it from `from`, and prepend
 * it to the destination **only if that lane is already loaded** (an unloaded lane picks
 * the card up whole when it is first visited — prepending there would show a card above
 * rows that were never fetched).
 */
export function moveDraft(
  state: TabsState,
  from: TabKey,
  draft: DevDraft,
): TabsState {
  const to = tabForStatus(draft.status);
  let next = patchTab(state, from, {
    items: state[from].items.filter((d) => d.id !== draft.id),
  });
  if (to === from) return replaceDraft(state, from, draft);
  if (next[to].loaded) {
    next = patchTab(next, to, {
      items: [draft, ...next[to].items.filter((d) => d.id !== draft.id)],
    });
  }
  return next;
}

/** Shift the tab badges for a card that moved lanes (never below zero). */
export function bumpCounts(counts: Counts, from: TabKey, to: TabKey): Counts {
  if (from === to) return counts;
  return {
    ...counts,
    [from]: Math.max(0, counts[from] - 1),
    [to]: counts[to] + 1,
  };
}
