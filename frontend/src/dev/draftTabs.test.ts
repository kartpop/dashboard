import { describe, expect, it } from "vitest";
import {
  AUTO_LOAD_TAB,
  type DevDraft,
  type DraftPage,
  type TabKey,
  type TabsState,
  appendPage,
  bumpCounts,
  emptyTabs,
  moveDraft,
  patchDraftEverywhere,
  tabForStatus,
} from "./draftTabs";

function draft(id: number, status: DevDraft["status"] = "draft"): DevDraft {
  return {
    id,
    title: `draft ${id}`,
    body: "",
    repo: "org/repo",
    status,
    sources: [],
    project_node_id: null,
    project_title: null,
    issue_url: null,
    issue_number: null,
    project_attached: false,
    created_at: "2026-08-01T00:00:00",
  };
}

/**
 * A mocked `GET /dev/drafts` — keyset-paged over a fixed row set, counting the fetches
 * so a lane's "how many pages did you pull?" behaviour is observable.
 */
function pagedEndpoint(rows: DevDraft[], limit: number) {
  let calls = 0;
  return {
    get calls() {
      return calls;
    },
    fetch(cursor: string | null): DraftPage {
      calls += 1;
      const start = cursor ? Number(cursor) : 0;
      const end = start + limit;
      return {
        items: rows.slice(start, end),
        next_cursor: end < rows.length ? String(end) : null,
      };
    },
  };
}

/** The review lane's drain loop: keep following the cursor until it runs out. */
function drain(
  state: TabsState,
  tab: TabKey,
  endpoint: ReturnType<typeof pagedEndpoint>,
): TabsState {
  let next = appendPage(state, tab, endpoint.fetch(null), true);
  while (next[tab].nextCursor !== null) {
    next = appendPage(next, tab, endpoint.fetch(next[tab].nextCursor));
  }
  return next;
}

describe("paging a lane", () => {
  it("drains the review lane to the last page — no overlap, no gap", () => {
    const rows = Array.from({ length: 47 }, (_, i) => draft(1000 - i));
    const endpoint = pagedEndpoint(rows, 20);

    // First page paints immediately, before the rest is asked for.
    let state = appendPage(emptyTabs(), "review", endpoint.fetch(null), true);
    expect(state.review.items).toHaveLength(20);
    expect(state.review.nextCursor).not.toBeNull();
    expect(state.review.loaded).toBe(true);

    state = drain(emptyTabs(), "review", endpoint);
    const ids = state.review.items.map((d) => d.id);
    expect(ids).toEqual(rows.map((d) => d.id)); // every row, in order
    expect(new Set(ids).size).toBe(47); // no duplicates
    expect(state.review.nextCursor).toBeNull(); // reached the end
    expect(state.review.loading).toBe(false);
  });

  it("keeps a settled lane at one page until Load older is asked for", () => {
    const rows = Array.from({ length: 55 }, (_, i) => draft(500 - i, "filed"));
    const endpoint = pagedEndpoint(rows, 20);
    expect(AUTO_LOAD_TAB).toBe("review"); // only review self-drains

    // Activation = exactly one fetch, one page in the DOM.
    let state = appendPage(emptyTabs(), "filed", endpoint.fetch(null), true);
    expect(endpoint.calls).toBe(1);
    expect(state.filed.items).toHaveLength(20);

    // "Load older" appends the next page and nothing more.
    state = appendPage(state, "filed", endpoint.fetch(state.filed.nextCursor));
    expect(endpoint.calls).toBe(2);
    expect(state.filed.items).toHaveLength(40);
    expect(state.filed.nextCursor).not.toBeNull();
  });

  it("never duplicates a card a page re-delivers", () => {
    let state = appendPage(
      emptyTabs(),
      "review",
      { items: [draft(1), draft(2)], next_cursor: "c" },
      true,
    );
    state = appendPage(state, "review", {
      items: [draft(2), draft(3)],
      next_cursor: null,
    });
    expect(state.review.items.map((d) => d.id)).toEqual([1, 2, 3]);
  });

  it("resets to page one when told to", () => {
    let state = appendPage(
      emptyTabs(),
      "review",
      { items: [draft(1), draft(2)], next_cursor: "c" },
      true,
    );
    state = appendPage(
      state,
      "review",
      { items: [draft(9)], next_cursor: null },
      true,
    );
    expect(state.review.items.map((d) => d.id)).toEqual([9]);
  });
});

describe("a card changing lanes", () => {
  const loadedBoth = () => {
    let s = appendPage(
      emptyTabs(),
      "review",
      { items: [draft(1), draft(2)], next_cursor: null },
      true,
    );
    s = appendPage(
      s,
      "saved",
      { items: [draft(7, "saved")], next_cursor: null },
      true,
    );
    return s;
  };

  it("leaves the source lane and joins a loaded destination at the top", () => {
    const state = moveDraft(loadedBoth(), "review", {
      ...draft(2),
      status: "saved",
    });
    expect(state.review.items.map((d) => d.id)).toEqual([1]);
    expect(state.saved.items.map((d) => d.id)).toEqual([2, 7]);
  });

  it("does not seed an unvisited lane (it loads whole on first visit)", () => {
    const start = appendPage(
      emptyTabs(),
      "review",
      { items: [draft(1)], next_cursor: null },
      true,
    );
    const state = moveDraft(start, "review", {
      ...draft(1),
      status: "dismissed",
    });
    expect(state.review.items).toEqual([]);
    expect(state.dismissed.items).toEqual([]);
    expect(state.dismissed.loaded).toBe(false);
  });

  it("replaces in place when the card stays in its lane (a filed retry)", () => {
    const start = appendPage(
      emptyTabs(),
      "filed",
      { items: [draft(4, "filed"), draft(5, "filed")], next_cursor: null },
      true,
    );
    const attached = { ...draft(4, "filed"), project_attached: true };
    const state = moveDraft(start, "filed", attached);
    expect(state.filed.items.map((d) => d.id)).toEqual([4, 5]); // order held
    expect(state.filed.items[0].project_attached).toBe(true);
  });

  it("maps each status to its lane", () => {
    expect(tabForStatus("draft")).toBe("review");
    expect(tabForStatus("saved")).toBe("saved");
    expect(tabForStatus("filed")).toBe("filed");
    expect(tabForStatus("dismissed")).toBe("dismissed");
  });
});

describe("tab badges", () => {
  const counts = { review: 3, saved: 1, filed: 8, dismissed: 0 };

  it("shifts one from the source lane to the destination", () => {
    expect(bumpCounts(counts, "review", "saved")).toEqual({
      review: 2,
      saved: 2,
      filed: 8,
      dismissed: 0,
    });
  });

  it("is a no-op within one lane and never goes negative", () => {
    expect(bumpCounts(counts, "filed", "filed")).toEqual(counts);
    expect(bumpCounts(counts, "dismissed", "review").dismissed).toBe(0);
  });
});

describe("inline edits", () => {
  it("patch a card wherever it currently sits", () => {
    const start = appendPage(
      emptyTabs(),
      "saved",
      { items: [draft(1, "saved"), draft(2, "saved")], next_cursor: null },
      true,
    );
    const state = patchDraftEverywhere(start, 2, {
      title: "edited on the shelf",
    });
    expect(state.saved.items[1].title).toBe("edited on the shelf");
    expect(state.saved.items[0].title).toBe("draft 1");
  });
});
