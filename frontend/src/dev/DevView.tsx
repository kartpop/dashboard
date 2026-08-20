import { useEffect, useRef, useState } from "react";
import { formatRelative } from "../formatDate";
import { DevConfigDrawer } from "./DevConfig";
import {
  AUTO_LOAD_TAB,
  type DevDraft,
  TABS,
  type TabKey,
  type TabState,
} from "./draftTabs";
import { MentionTextarea } from "./MentionTextarea";
import type { Member } from "./mentions";
import { formatScanTally } from "./scanTally";
import { matchLabel, visibleMatches } from "./similar";
import { type Project, useDevConfig, useDevPanel } from "./useDevPanel";

/** What an empty lane says, per tab. */
const EMPTY_HINT: Record<TabKey, string> = {
  review:
    "Nothing to review. Hit Create now after a meeting to synthesise your notes into issues.",
  saved:
    "Nothing set aside. Use Save for later on a draft that's real but not now.",
  filed: "No issues filed yet.",
  dismissed: "Nothing dismissed.",
};

/**
 * The Dev view (goal 12; tabbed in 12a): synthesised GitHub-issue draft cards, split
 * across four lanes — In review, Saved for later, Filed, Dismissed. Each card is
 * editable (title/body/repo/project) and has exactly one path to a GitHub write —
 * Approve & file. Save, Move to review and Dismiss are free local flips.
 *
 * In review is the lane meant to be worked to empty, so it drains itself by scroll; the
 * three settled lanes are archives, so they load one page and reveal older rows only on
 * demand. That asymmetry is the whole point: the review lane stays uncluttered however
 * much filed/dismissed history accumulates. A merged issue shows its provenance (every
 * entry it was synthesised from) on the muted sources line. The config (PAT → repos →
 * projects → source Docs) lives in a drawer inside the view, keeping this goal
 * independent of the settings restructure (g13).
 */
export function DevView() {
  const dev = useDevPanel();
  const cfg = useDevConfig();
  const [showConfig, setShowConfig] = useState(false);

  useEffect(() => {
    void cfg.load();
  }, [cfg.load]);

  const repos = cfg.config?.repos ?? [];
  const tab = dev.tabs[dev.activeTab];
  const totalDrafts = Object.values(dev.counts).reduce((a, b) => a + b, 0);

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

      {/* What the scan you just ran actually did. A scan that reads a Doc and draws no
          draft from it (the work is already filed) looks exactly like a Doc that was
          never read — unless the counts say otherwise. */}
      {dev.lastTally && (
        <p className="dev-scan-tally" role="status">
          {formatScanTally(dev.lastTally)}
        </p>
      )}

      <nav className="dev-tabs" role="tablist" aria-label="Draft lanes">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={dev.activeTab === t.key}
            className={`dev-tab${dev.activeTab === t.key ? " dev-tab--active" : ""}`}
            onClick={() => dev.setActiveTab(t.key)}
          >
            {t.label}
            <span className="dev-tab-count">{dev.counts[t.key]}</span>
          </button>
        ))}
      </nav>

      {dev.loading ? (
        <p className="dev-empty">Loading…</p>
      ) : !dev.configComplete && totalDrafts === 0 ? (
        <p className="dev-empty">
          Set up the <strong>Config</strong> (a GitHub token, at least one
          source Doc, and a target repo) — then <strong>Create now</strong> to
          draft issues from your notes.
        </p>
      ) : (
        <DraftLane
          tabKey={dev.activeTab}
          tab={tab}
          repos={repos}
          listProjects={cfg.listProjects}
          listMembers={cfg.listMembers}
          onLoadMore={dev.loadMore}
          onPatch={dev.patchDraft}
          onFile={dev.fileDraft}
          onSave={dev.saveDraft}
          onUnsave={dev.unsaveDraft}
          onDismiss={dev.dismissDraft}
        />
      )}
    </main>
  );
}

/**
 * One lane's card list. The review lane appends the next page as the user nears the
 * bottom (an IntersectionObserver sentinel, repeating until the cursor runs out); the
 * settled lanes stay at what they've loaded behind an explicit "Load older".
 */
function DraftLane({
  tabKey,
  tab,
  repos,
  listProjects,
  listMembers,
  onLoadMore,
  onPatch,
  onFile,
  onSave,
  onUnsave,
  onDismiss,
}: {
  tabKey: TabKey;
  tab: TabState;
  repos: { full_name: string; description: string; is_default: boolean }[];
  listProjects: (repo: string) => Promise<Project[]>;
  listMembers: (repo: string) => Promise<Member[]>;
  onLoadMore: (tab: TabKey) => void;
  onPatch: (id: number, patch: Partial<DevDraft>) => void;
  onFile: (id: number) => void | Promise<void>;
  onSave: (id: number) => void;
  onUnsave: (id: number) => void;
  onDismiss: (id: number) => void;
}) {
  const autoLoad = tabKey === AUTO_LOAD_TAB;
  const sentinel = useRef<HTMLDivElement | null>(null);
  const hasMore = tab.nextCursor !== null;

  useEffect(() => {
    if (!autoLoad || !hasMore) return;
    const node = sentinel.current;
    if (!node) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) onLoadMore(tabKey);
      },
      { rootMargin: "300px" },
    );
    io.observe(node);
    return () => io.disconnect();
  }, [autoLoad, hasMore, onLoadMore, tabKey, tab.items.length]);

  if (tab.loaded && tab.items.length === 0 && !tab.loading) {
    return <p className="dev-empty">{EMPTY_HINT[tabKey]}</p>;
  }

  return (
    <div className="dev-list">
      {tab.items.map((d) => (
        <DraftCard
          key={d.id}
          draft={d}
          repos={repos}
          listProjects={listProjects}
          listMembers={listMembers}
          onPatch={onPatch}
          onFile={onFile}
          onSave={onSave}
          onUnsave={onUnsave}
          onDismiss={onDismiss}
        />
      ))}

      {autoLoad && hasMore && (
        <div ref={sentinel} className="dev-scroll-sentinel" />
      )}
      {tab.loading && <p className="dev-lane-loading">Loading…</p>}
      {!autoLoad && hasMore && (
        <button
          className="dev-load-older"
          onClick={() => onLoadMore(tabKey)}
          disabled={tab.loading}
        >
          Load older
        </button>
      )}
    </div>
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
  listMembers,
  onPatch,
  onFile,
  onSave,
  onUnsave,
  onDismiss,
}: {
  draft: DevDraft;
  repos: { full_name: string; description: string; is_default: boolean }[];
  listProjects: (repo: string) => Promise<Project[]>;
  listMembers: (repo: string) => Promise<Member[]>;
  onPatch: (id: number, patch: Partial<DevDraft>) => void;
  onFile: (id: number) => void | Promise<void>;
  onSave: (id: number) => void;
  onUnsave: (id: number) => void;
  onDismiss: (id: number) => void;
}) {
  const [title, setTitle] = useState(draft.title);
  const [body, setBody] = useState(draft.body);
  const [projects, setProjects] = useState<Project[]>([]);
  const [filing, setFiling] = useState(false);
  // A shelved card is still actionable (goal 12a) — `saved` is a shelf, not a freeze.
  const editable = draft.status === "draft" || draft.status === "saved";
  // A comment draft's target is fixed (goal 12b): the repo/project dropdowns hide —
  // re-targeting a comment means dismissing it.
  const isComment = draft.kind === "comment";
  const similar = visibleMatches(draft);

  useEffect(() => setTitle(draft.title), [draft.title]);
  useEffect(() => setBody(draft.body), [draft.body]);

  // Load the ProjectsV2 projects for the current repo (repopulates on repo change).
  useEffect(() => {
    let live = true;
    if (!draft.repo || !editable || isComment) {
      setProjects([]);
      return;
    }
    void listProjects(draft.repo)
      .then((ps) => live && setProjects(ps))
      .catch(() => live && setProjects([]));
    return () => {
      live = false;
    };
  }, [draft.repo, editable, isComment, listProjects]);

  const commitTitle = () => {
    if (title.trim() !== draft.title)
      onPatch(draft.id, { title: title.trim() });
  };
  const commitBody = () => {
    if (body !== draft.body) onPatch(draft.id, { body });
  };

  const onRepoChange = (repo: string) => {
    // Changing the repo clears the project (the old one belongs to the old repo) AND
    // the similar-issue matches (they were judged against the old repo's issues/PRs —
    // the server clears them too; the next scan re-matches, no live re-match).
    onPatch(draft.id, {
      repo,
      project_node_id: null,
      project_title: null,
      related_issues: null,
    });
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
        : draft.status === "saved"
          ? " dev-card--saved"
          : "";

  return (
    <article className={`dev-card${statusClass}`}>
      {isComment && draft.target_issue_url && (
        <a
          className="dev-comment-target"
          href={draft.target_issue_url}
          target="_blank"
          rel="noopener noreferrer"
          title="This draft files as a comment on the existing issue"
        >
          Comment on {draft.repo}#{draft.target_issue_number} ↗
        </a>
      )}

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
        <MentionTextarea
          value={body}
          onChange={setBody}
          onBlur={commitBody}
          rows={Math.min(16, Math.max(4, body.split("\n").length + 1))}
          placeholder={
            isComment ? "Comment (markdown)" : "Issue body (markdown)"
          }
          loadMembers={() => listMembers(draft.repo)}
        />
      ) : (
        <pre className="dev-card-body dev-card-body--static">{draft.body}</pre>
      )}

      {/* A comment card's target is fixed — no repo/project to choose. */}
      {!isComment && (
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
      )}

      {draft.sources.length > 0 && (
        <div className="dev-card-sources">
          {draft.sources.map((s, i) => (
            <span key={i} className="dev-source-chip">
              {s.doc_path} · {formatEntryTs(s.entry_ts)}
            </span>
          ))}
        </div>
      )}

      {/* The clickable pre-file affordance (goal 12b): the body's `Related:` line is
          plain textarea text, so THIS line renders the same validated matches as real
          anchors — every similar issue/PR is one click away before deciding to file. */}
      {similar.length > 0 && (
        <div className="dev-card-similar">
          <span className="dev-similar-label">Similar:</span>
          {similar.map((m) => (
            <span key={`${m.type}-${m.number}`} className="dev-similar-entry">
              <a
                href={m.url}
                target="_blank"
                rel="noopener noreferrer"
                title={m.reason}
              >
                {matchLabel(m)} ↗
              </a>
              {m.nothing_new && (
                <em className="dev-nothing-new">
                  {" "}
                  — covered by it, nothing new to add
                </em>
              )}
            </span>
          ))}
        </div>
      )}

      <div className="dev-card-foot">
        {editable && (
          <>
            <button
              className="dev-file-btn"
              onClick={doFile}
              disabled={filing || !draft.repo}
            >
              {filing ? "Filing…" : "Approve & file"}
            </button>
            {draft.status === "draft" ? (
              <button
                className="dev-save-btn"
                onClick={() => onSave(draft.id)}
                disabled={filing}
                title="Set this aside without deciding — it moves to Saved for later"
              >
                Save for later
              </button>
            ) : (
              <button
                className="dev-unsave-btn"
                onClick={() => onUnsave(draft.id)}
                disabled={filing}
              >
                Move to review
              </button>
            )}
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
                {isComment ? "Comment on " : ""}
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
          <>
            <span className="dev-dismissed-tag">Dismissed</span>
            <button
              className="dev-unsave-btn"
              onClick={() => onUnsave(draft.id)}
              title="Put this back in the review lane"
            >
              Move to review
            </button>
          </>
        )}
      </div>
    </article>
  );
}
