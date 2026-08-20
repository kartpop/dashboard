import { useCallback, useEffect, useRef, useState } from "react";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "../api";
import {
  type Counts,
  type DevDraft,
  type DraftPage,
  type TabKey,
  type TabsState,
  ZERO_COUNTS,
  appendPage,
  bumpCounts,
  emptyTabs,
  moveDraft,
  patchDraftEverywhere,
  replaceDraft,
  setLoading as setTabLoading,
  tabForStatus,
} from "./draftTabs";
import type { Member } from "./mentions";
import type { ScanTally } from "./scanTally";

export type { DevDraft, DraftSource, TabKey } from "./draftTabs";
export type { Member } from "./mentions";

/** `GET /dev` — view metadata only (goal 12a); the drafts come from `/dev/drafts`. */
interface DevMeta {
  last_scan_at: string | null;
  config_complete: boolean;
  counts: Counts;
}

const PAGE_LIMIT = 20;

/**
 * The Dev view's draft hook (goal 12; tabbed + paged in 12a) — owns four lanes of
 * issue drafts and the scan/file/save/unsave/dismiss/edit actions. Each lane keeps its
 * own keyset cursor and loads lazily on first activation, so the view's payload stays
 * bounded however much filed/dismissed history piles up. The pure state transitions
 * live in `draftTabs.ts`; this hook is the fetching + optimism around them.
 *
 * Inline edits and the three local status flips are optimistic (the card moves now, the
 * write fires without a reload), matching the dashboard's optimistic-write convention.
 * Filing is NOT optimistic: a GitHub write is consequential, so we await it and reload
 * the card from the server (issue link / partial-attach state come back authoritative).
 */
export function useDevPanel() {
  const [tabs, setTabs] = useState<TabsState>(emptyTabs);
  const [activeTab, setActive] = useState<TabKey>("review");
  const [counts, setCounts] = useState<Counts>(ZERO_COUNTS);
  const [lastScanAt, setLastScanAt] = useState<string | null>(null);
  const [configComplete, setConfigComplete] = useState(false);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  // What the last scan in THIS session did. Session-only by design: it answers "what did
  // that click just do?", so it is meaningless after a reload (unlike `lastScanAt`).
  const [lastTally, setLastTally] = useState<ScanTally | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The cursor a fetch is already in flight for, per lane — guards the review lane's
  // scroll sentinel from firing the same page twice.
  const inFlight = useRef<Partial<Record<TabKey, string | null>>>({});

  /** Re-read the view metadata (counts reconcile against the DB on every tab switch). */
  const loadMeta = useCallback(async () => {
    try {
      const meta = await apiGet<DevMeta>("/dev");
      setLastScanAt(meta.last_scan_at);
      setConfigComplete(meta.config_complete);
      setCounts(meta.counts);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  /**
   * Fetch one page of a lane and fold it in. The cursor is passed in rather than read
   * from state (the caller holds the live lane), so this stays identity-stable; `null`
   * + `reset` is "page one, replace what's there" (first activation, or after a scan).
   * `inFlight` makes a re-fired fetch for the same cursor a no-op — the review lane's
   * scroll sentinel can trip twice for one page.
   */
  const loadPage = useCallback(
    async (tab: TabKey, cursor: string | null, reset = false) => {
      if (inFlight.current[tab] === cursor) return;
      inFlight.current[tab] = cursor;
      setTabs((prev) => setTabLoading(prev, tab, true));
      try {
        const q = new URLSearchParams({
          status: tab,
          limit: String(PAGE_LIMIT),
        });
        if (cursor) q.set("cursor", cursor);
        const page = await apiGet<DraftPage>(`/dev/drafts?${q}`);
        setTabs((prev) => appendPage(prev, tab, page, reset));
        setError(null);
      } catch (e) {
        setError((e as Error).message);
        setTabs((prev) => setTabLoading(prev, tab, false));
      } finally {
        inFlight.current[tab] = undefined;
      }
    },
    [],
  );

  useEffect(() => {
    void (async () => {
      await Promise.all([loadMeta(), loadPage("review", null, true)]);
      setLoading(false);
    })();
  }, [loadMeta, loadPage]);

  /** Switch lanes: load the first page if this is the lane's first visit, and
   * reconcile the badges against the server. */
  const setActiveTab = useCallback(
    (tab: TabKey) => {
      setActive(tab);
      if (!tabs[tab].loaded) void loadPage(tab, null, true);
      void loadMeta();
    },
    [loadMeta, loadPage, tabs],
  );

  /** Append the next page of a lane (scroll sentinel for review, "Load older" for the
   * settled lanes). A no-op once the lane's cursor is null. */
  const loadMore = useCallback(
    (tab: TabKey) => {
      const lane = tabs[tab];
      if (lane.loading || lane.nextCursor === null) return;
      void loadPage(tab, lane.nextCursor);
    },
    [loadPage, tabs],
  );

  const reload = useCallback(async () => {
    await Promise.all([loadMeta(), loadPage(activeTab, null, true)]);
  }, [activeTab, loadMeta, loadPage]);

  const scanNow = useCallback(async () => {
    setScanning(true);
    setLastTally(null);
    try {
      // Keep the tally: "2 docs · 2 new entries · 0 drafts" is the only thing that tells
      // a suppressed-as-already-filed entry apart from a source Doc that never got read.
      const { tally } = await apiPost<{ tally: ScanTally }>(
        "/dev/scan-now",
        {},
      );
      setLastTally(tally);
      // New drafts land at the top of the review lane — re-read it from page one.
      await Promise.all([loadMeta(), loadPage("review", null, true)]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setScanning(false);
    }
  }, [loadMeta, loadPage]);

  const patchDraft = useCallback((id: number, patch: Partial<DevDraft>) => {
    // Optimistic: update the card now, fire the write without awaiting/reloading.
    setTabs((prev) => patchDraftEverywhere(prev, id, patch));
    void apiPatch(`/dev/${id}`, patch).catch((e) =>
      setError((e as Error).message),
    );
  }, []);

  /**
   * A local status flip (save / unsave / dismiss): the card leaves the lane it is in
   * and joins its destination, the badges shift, and the POST fires without awaiting —
   * none of these touch GitHub. A failure re-reads the truth rather than guessing.
   */
  const flip = useCallback(
    (
      id: number,
      action: "save" | "unsave" | "dismiss",
      status: DevDraft["status"],
    ) => {
      const from = activeTab;
      const draft = tabs[from].items.find((d) => d.id === id);
      if (!draft) return;
      const moved = { ...draft, status };
      setTabs((prev) => moveDraft(prev, from, moved));
      setCounts((prev) => bumpCounts(prev, from, tabForStatus(status)));
      void apiPost(`/dev/${id}/${action}`, {}).catch((e) => {
        setError((e as Error).message);
        void reload();
      });
    },
    [activeTab, reload, tabs],
  );

  const saveDraft = useCallback(
    (id: number) => flip(id, "save", "saved"),
    [flip],
  );
  const unsaveDraft = useCallback(
    (id: number) => flip(id, "unsave", "draft"),
    [flip],
  );
  const dismissDraft = useCallback(
    (id: number) => flip(id, "dismiss", "dismissed"),
    [flip],
  );

  /**
   * Approve & file — awaited, never optimistic. The card is replaced in place with the
   * server's version so the issue link (or the "attach pending" retry affordance) is
   * visible right where it was filed; the badges move it to the Filed lane.
   */
  const fileDraft = useCallback(
    async (id: number) => {
      const from = activeTab;
      const before = tabs[from].items.find((d) => d.id === id);
      try {
        const updated = await apiPost<DevDraft>(`/dev/${id}/file`, {});
        setTabs((prev) => replaceDraft(prev, from, updated));
        if (before && before.status !== "filed") {
          setCounts((prev) => bumpCounts(prev, from, "filed"));
        }
        setError(null);
      } catch (e) {
        setError((e as Error).message);
        // Reload so a partial (issue created, attach pending) surfaces correctly.
        void reload();
      }
    },
    [activeTab, reload, tabs],
  );

  return {
    tabs,
    activeTab,
    setActiveTab,
    counts,
    lastScanAt,
    lastTally,
    configComplete,
    loading,
    scanning,
    error,
    reload,
    loadMore,
    scanNow,
    patchDraft,
    fileDraft,
    saveDraft,
    unsaveDraft,
    dismissDraft,
  };
}

// ── Config ────────────────────────────────────────────────────────────────────

export interface TreeNode {
  node_id: string;
  name: string;
  kind: "folder" | "doc";
  children: TreeNode[];
}

export interface RepoCfg {
  full_name: string;
  description: string;
  is_default: boolean;
}

export interface AvailableRepo {
  full_name: string;
  description: string;
  private: boolean;
}

export interface Project {
  node_id: string;
  title: string;
  number: number;
}

export interface GithubToken {
  owner: string;
  hint: string;
  login: string | null;
}

export interface DevConfig {
  tokens: GithubToken[];
  sources: string[];
  notes_tree: TreeNode[];
  repos: RepoCfg[];
  projects: Record<string, { node_id: string; title: string }>;
  last_scan_at: string | null;
  config_complete: boolean;
}

/** The in-view Dev config hook: PAT, source Docs, repo catalog, per-repo projects. */
export function useDevConfig() {
  const [config, setConfig] = useState<DevConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setConfig(await apiGet<DevConfig>("/dev/config"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const addToken = useCallback(
    async (pat: string): Promise<string[]> => {
      const data = await apiPost<{ owners: string[]; login: string | null }>(
        "/dev/config/tokens",
        { pat },
      );
      await load();
      return data.owners;
    },
    [load],
  );

  const removeToken = useCallback(
    async (owner: string) => {
      await apiDelete(`/dev/config/tokens/${encodeURIComponent(owner)}`);
      await load();
    },
    [load],
  );

  const saveSources = useCallback(async (nodeIds: string[]) => {
    setConfig((c) => (c ? { ...c, sources: nodeIds } : c));
    await apiPut("/dev/config/sources", { node_ids: nodeIds });
  }, []);

  const saveRepos = useCallback(async (repos: RepoCfg[]) => {
    setConfig((c) => (c ? { ...c, repos } : c));
    await apiPut("/dev/config/repos", { repos });
  }, []);

  const saveProjects = useCallback(
    async (projects: Record<string, { node_id: string; title: string }>) => {
      setConfig((c) => (c ? { ...c, projects } : c));
      await apiPut("/dev/config/projects", { projects });
    },
    [],
  );

  const refreshRepos = useCallback(async (): Promise<AvailableRepo[]> => {
    const data = await apiPost<{ repos: AvailableRepo[] }>(
      "/dev/config/refresh",
      {},
    );
    return data.repos;
  }, []);

  const listProjects = useCallback(async (repo: string): Promise<Project[]> => {
    const data = await apiGet<{ projects: Project[] }>(
      `/dev/config/projects?repo=${encodeURIComponent(repo)}`,
    );
    return data.projects;
  }, []);

  // The @-mention typeahead's member list (goal 12b) — fetched lazily the first time
  // a card's editor needs it, then cached per repo for the session. A failure caches
  // an empty list: the typeahead offers nothing, typing `@login` by hand still works.
  const membersCache = useRef<Map<string, Promise<Member[]>>>(new Map());
  const listMembers = useCallback((repo: string): Promise<Member[]> => {
    const cached = membersCache.current.get(repo);
    if (cached) return cached;
    const fetched = apiGet<{ members: Member[] }>(
      `/dev/config/members?repo=${encodeURIComponent(repo)}`,
    )
      .then((data) => data.members)
      .catch(() => [] as Member[]);
    membersCache.current.set(repo, fetched);
    return fetched;
  }, []);

  return {
    config,
    error,
    setError,
    load,
    addToken,
    removeToken,
    saveSources,
    saveRepos,
    saveProjects,
    refreshRepos,
    listProjects,
    listMembers,
  };
}
