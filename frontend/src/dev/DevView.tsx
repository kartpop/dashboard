import { useEffect, useMemo, useState } from "react";
import { formatRelative } from "../formatDate";
import { DevConfigDrawer } from "./DevConfig";
import {
  type DevDraft,
  type Project,
  useDevConfig,
  useDevPanel,
} from "./useDevPanel";

/**
 * The Dev view (goal 12): a list of synthesised GitHub-issue draft cards. Each card is
 * editable (title/body/repo/project) and has exactly one path to a GitHub write —
 * Approve & file. Dismiss is a free local flip. A merged issue shows its provenance
 * (every entry it was synthesised from) on the muted sources line. The config
 * (PAT → repos → projects → source Docs) lives in a drawer inside the view, keeping this
 * goal independent of the settings restructure (g13).
 */
export function DevView() {
  const dev = useDevPanel();
  const cfg = useDevConfig();
  const [showConfig, setShowConfig] = useState(false);

  useEffect(() => {
    void cfg.load();
  }, [cfg.load]);

  const { pending, settled } = useMemo(() => {
    const pending = dev.drafts.filter((d) => d.status === "draft");
    const settled = dev.drafts.filter((d) => d.status !== "draft");
    return { pending, settled };
  }, [dev.drafts]);

  const repos = cfg.config?.repos ?? [];

  return (
    <main className="dev-view">
      <header className="dev-header">
        <h1>Dev</h1>
        <div className="dev-header-meta">
          {dev.lastScanAt && (
            <span className="dev-last-run">
              Scanned {formatRelative(dev.lastScanAt)}
            </span>
          )}
          <button
            className="dev-config-btn"
            onClick={() => setShowConfig((s) => !s)}
          >
            Config
          </button>
          <button
            className="dev-scan-btn"
            onClick={dev.scanNow}
            disabled={dev.scanning || !dev.configComplete}
            title={
              dev.configComplete
                ? "Scan your notes for new issues now"
                : "Finish config first (token, source Doc, repo)"
            }
          >
            {dev.scanning ? "Scanning…" : "Create now"}
          </button>
        </div>
      </header>

      {showConfig && (
        <DevConfigDrawer
          cfg={cfg}
          onClose={() => {
            setShowConfig(false);
            // Config lives in a separate hook; re-pull the panel so `configComplete`
            // (which gates Create now) reflects what was just saved — no page reload.
            void dev.reload();
          }}
        />
      )}

      {dev.error && <p className="dev-error">{dev.error}</p>}

      {dev.loading ? (
        <p className="dev-empty">Loading…</p>
      ) : !dev.configComplete && dev.drafts.length === 0 ? (
        <p className="dev-empty">
          Set up the <strong>Config</strong> (a GitHub token, at least one
          source Doc, and a target repo) — then <strong>Create now</strong> to
          draft issues from your notes.
        </p>
      ) : dev.drafts.length === 0 ? (
        <p className="dev-empty">
          No drafts yet. Hit <strong>Create now</strong> after a meeting to
          synthesise your notes into issues.
        </p>
      ) : (
        <div className="dev-list">
          {pending.map((d) => (
            <DraftCard
              key={d.id}
              draft={d}
              repos={repos}
              listProjects={cfg.listProjects}
              onPatch={dev.patchDraft}
              onFile={dev.fileDraft}
              onDismiss={dev.dismissDraft}
            />
          ))}
          {settled.length > 0 && (
            <div className="dev-settled">
              {settled.map((d) => (
                <DraftCard
                  key={d.id}
                  draft={d}
                  repos={repos}
                  listProjects={cfg.listProjects}
                  onPatch={dev.patchDraft}
                  onFile={dev.fileDraft}
                  onDismiss={dev.dismissDraft}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </main>
  );
}

function formatEntryTs(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

function DraftCard({
  draft,
  repos,
  listProjects,
  onPatch,
  onFile,
  onDismiss,
}: {
  draft: DevDraft;
  repos: { full_name: string; description: string; is_default: boolean }[];
  listProjects: (repo: string) => Promise<Project[]>;
  onPatch: (id: number, patch: Partial<DevDraft>) => void;
  onFile: (id: number) => void | Promise<void>;
  onDismiss: (id: number) => void | Promise<void>;
}) {
  const [title, setTitle] = useState(draft.title);
  const [body, setBody] = useState(draft.body);
  const [projects, setProjects] = useState<Project[]>([]);
  const [filing, setFiling] = useState(false);
  const editable = draft.status === "draft";

  useEffect(() => setTitle(draft.title), [draft.title]);
  useEffect(() => setBody(draft.body), [draft.body]);

  // Load the ProjectsV2 projects for the current repo (repopulates on repo change).
  useEffect(() => {
    let live = true;
    if (!draft.repo || !editable) {
      setProjects([]);
      return;
    }
    void listProjects(draft.repo)
      .then((ps) => live && setProjects(ps))
      .catch(() => live && setProjects([]));
    return () => {
      live = false;
    };
  }, [draft.repo, editable, listProjects]);

  const commitTitle = () => {
    if (title.trim() !== draft.title)
      onPatch(draft.id, { title: title.trim() });
  };
  const commitBody = () => {
    if (body !== draft.body) onPatch(draft.id, { body });
  };

  const onRepoChange = (repo: string) => {
    // Changing the repo clears the project (the old one belongs to the old repo).
    onPatch(draft.id, { repo, project_node_id: null, project_title: null });
  };

  const onProjectChange = (nodeId: string) => {
    const proj = projects.find((p) => p.node_id === nodeId);
    onPatch(draft.id, {
      project_node_id: nodeId || null,
      project_title: proj?.title ?? null,
    });
  };

  const doFile = async () => {
    setFiling(true);
    try {
      await onFile(draft.id);
    } finally {
      setFiling(false);
    }
  };

  const statusClass =
    draft.status === "filed"
      ? " dev-card--filed"
      : draft.status === "dismissed"
        ? " dev-card--dismissed"
        : "";

  return (
    <article className={`dev-card${statusClass}`}>
      {editable ? (
        <input
          className="dev-card-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onBlur={commitTitle}
          placeholder="Issue title"
        />
      ) : (
        <div className="dev-card-title dev-card-title--static">
          {draft.title}
        </div>
      )}

      {editable ? (
        <textarea
          className="dev-card-body"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onBlur={commitBody}
          rows={Math.min(16, Math.max(4, body.split("\n").length + 1))}
          placeholder="Issue body (markdown)"
        />
      ) : (
        <pre className="dev-card-body dev-card-body--static">{draft.body}</pre>
      )}

      <div className="dev-card-controls">
        <label className="dev-field">
          <span>Repo</span>
          <select
            value={draft.repo}
            onChange={(e) => onRepoChange(e.target.value)}
            disabled={!editable}
          >
            {!repos.some((r) => r.full_name === draft.repo) && (
              <option value={draft.repo}>{draft.repo || "(none)"}</option>
            )}
            {repos.map((r) => (
              <option key={r.full_name} value={r.full_name}>
                {r.full_name}
                {r.is_default ? " ★" : ""}
              </option>
            ))}
          </select>
        </label>

        <label className="dev-field">
          <span>Project</span>
          <select
            value={draft.project_node_id ?? ""}
            onChange={(e) => onProjectChange(e.target.value)}
            disabled={!editable}
          >
            <option value="">(no project)</option>
            {draft.project_node_id &&
              !projects.some((p) => p.node_id === draft.project_node_id) && (
                <option value={draft.project_node_id}>
                  {draft.project_title ?? "current project"}
                </option>
              )}
            {projects.map((p) => (
              <option key={p.node_id} value={p.node_id}>
                {p.title}
              </option>
            ))}
          </select>
        </label>
      </div>

      {draft.sources.length > 0 && (
        <div className="dev-card-sources">
          {draft.sources.map((s, i) => (
            <span key={i} className="dev-source-chip">
              {s.doc_path} · {formatEntryTs(s.entry_ts)}
            </span>
          ))}
        </div>
      )}

      <div className="dev-card-foot">
        {draft.status === "draft" && (
          <>
            <button
              className="dev-file-btn"
              onClick={doFile}
              disabled={filing || !draft.repo}
            >
              {filing ? "Filing…" : "Approve & file"}
            </button>
            <button
              className="dev-dismiss-btn"
              onClick={() => onDismiss(draft.id)}
              disabled={filing}
            >
              Dismiss
            </button>
          </>
        )}
        {draft.status === "filed" && (
          <>
            {draft.issue_url && (
              <a
                className="dev-issue-link"
                href={draft.issue_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {draft.repo}#{draft.issue_number}
              </a>
            )}
            {draft.project_node_id && !draft.project_attached && (
              <button
                className="dev-retry-btn"
                onClick={doFile}
                disabled={filing}
                title="The issue was created; retry attaching it to the project"
              >
                {filing ? "Retrying…" : "Attach project (retry)"}
              </button>
            )}
          </>
        )}
        {draft.status === "dismissed" && (
          <span className="dev-dismissed-tag">Dismissed</span>
        )}
      </div>
    </article>
  );
}
