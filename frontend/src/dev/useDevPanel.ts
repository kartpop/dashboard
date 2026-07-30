import { useCallback, useEffect, useState } from "react";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "../api";

export interface DraftSource {
  doc_path: string;
  entry_ts: string;
}

export interface DevDraft {
  id: number;
  title: string;
  body: string;
  repo: string;
  status: "draft" | "filed" | "dismissed";
  sources: DraftSource[];
  project_node_id: string | null;
  project_title: string | null;
  issue_url: string | null;
  issue_number: number | null;
  project_attached: boolean;
  created_at: string;
}

interface DraftsResponse {
  drafts: DevDraft[];
  last_scan_at: string | null;
  config_complete: boolean;
}

/**
 * The Dev view's draft hook (goal 12) — owns the issue-draft list + the scan/file/
 * dismiss/edit actions. Inline edits are optimistic (the card updates now, the PATCH
 * fires without a reload), matching the dashboard's optimistic-write convention. Filing
 * is NOT optimistic: a GitHub write is consequential, so we await it and reload the card
 * from the server (issue link / partial-attach state come back authoritative).
 */
export function useDevPanel() {
  const [drafts, setDrafts] = useState<DevDraft[]>([]);
  const [lastScanAt, setLastScanAt] = useState<string | null>(null);
  const [configComplete, setConfigComplete] = useState(false);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await apiGet<DraftsResponse>("/dev");
      setDrafts(data.drafts);
      setLastScanAt(data.last_scan_at);
      setConfigComplete(data.config_complete);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const scanNow = useCallback(async () => {
    setScanning(true);
    try {
      await apiPost("/dev/scan-now", {});
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setScanning(false);
    }
  }, [load]);

  const patchDraft = useCallback((id: number, patch: Partial<DevDraft>) => {
    // Optimistic: update the card now, fire the write without awaiting/reloading.
    setDrafts((prev) =>
      prev.map((d) => (d.id === id ? { ...d, ...patch } : d)),
    );
    void apiPatch(`/dev/${id}`, patch).catch((e) =>
      setError((e as Error).message),
    );
  }, []);

  const fileDraft = useCallback(
    async (id: number) => {
      try {
        const updated = await apiPost<DevDraft>(`/dev/${id}/file`, {});
        setDrafts((prev) => prev.map((d) => (d.id === id ? updated : d)));
        setError(null);
      } catch (e) {
        setError((e as Error).message);
        // Reload so a partial (issue created, attach pending) surfaces correctly.
        void load();
      }
    },
    [load],
  );

  const dismissDraft = useCallback(async (id: number) => {
    try {
      const updated = await apiPost<DevDraft>(`/dev/${id}/dismiss`, {});
      setDrafts((prev) => prev.map((d) => (d.id === id ? updated : d)));
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  return {
    drafts,
    lastScanAt,
    configComplete,
    loading,
    scanning,
    error,
    reload: load,
    scanNow,
    patchDraft,
    fileDraft,
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
  };
}
