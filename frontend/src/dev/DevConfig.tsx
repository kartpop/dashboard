import { useEffect, useState } from "react";
import {
  type AvailableRepo,
  type Project,
  type RepoCfg,
  type TreeNode,
  useDevConfig,
} from "./useDevPanel";

/**
 * The in-view Dev config drawer (goal 12). GitHub tokens are the key that unlocks the
 * rest — a fine-grained PAT is bound to one resource owner, so filing into a personal
 * account AND an org needs one token each; they're listed by owner here. Once at least
 * one token exists, repos are *enumerated from GitHub* across all tokens (never
 * hand-typed), the user ticks targets + marks a default + writes the one-line
 * description each (the only hand-typed field — it feeds the LLM's repo pick), and picks
 * the default ProjectsV2 project per repo. Everything below the tokens is disabled until
 * a valid token exists.
 */
export function DevConfigDrawer({
  cfg,
  onClose,
}: {
  cfg: ReturnType<typeof useDevConfig>;
  onClose: () => void;
}) {
  const config = cfg.config;

  return (
    <section className="dev-config-drawer">
      <div className="dev-config-head">
        <h3>Dev config</h3>
        <button
          className="dev-config-close"
          onClick={onClose}
          aria-label="Close"
        >
          ✕
        </button>
      </div>

      {cfg.error && <p className="dev-error">{cfg.error}</p>}
      {!config ? (
        <p className="dev-empty">Loading config…</p>
      ) : (
        <>
          <TokensSection cfg={cfg} />
          {config.tokens.length > 0 ? (
            <>
              <ReposSection cfg={cfg} />
              <SourcesSection cfg={cfg} />
            </>
          ) : (
            <p className="dev-config-hint">
              Add a GitHub token above to configure repos and source Docs.
            </p>
          )}
        </>
      )}
    </section>
  );
}

function TokensSection({ cfg }: { cfg: ReturnType<typeof useDevConfig> }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const tokens = cfg.config?.tokens ?? [];

  const add = async () => {
    if (!value.trim()) return;
    setBusy(true);
    setFlash(null);
    try {
      const owners = await cfg.addToken(value.trim());
      setValue("");
      setFlash(`Token added for ${owners.join(", ")} ✓`);
    } catch (e) {
      setFlash((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (owner: string) => {
    setFlash(null);
    try {
      await cfg.removeToken(owner);
    } catch (e) {
      setFlash((e as Error).message);
    }
  };

  return (
    <div className="dev-config-section">
      <h4>GitHub tokens</h4>
      <p className="dev-config-hint">
        A fine-grained PAT (Issues read/write, org Projects read/write, Metadata
        read) is scoped to <strong>one resource owner</strong> — add one token
        per account/org you file into (e.g. your personal username and each
        org). The owner is inferred from the repos the token can see. Stored
        encrypted, never shown again.
      </p>
      {tokens.length > 0 && (
        <ul className="dev-token-list">
          {tokens.map((t) => (
            <li key={t.owner} className="dev-token-item">
              <span className="dev-token-owner">{t.owner}</span>
              <span className="dev-token-hint">{t.hint}</span>
              {t.login && <span className="dev-token-login">@{t.login}</span>}
              <button
                className="dev-token-remove"
                onClick={() => remove(t.owner)}
                title={`Remove the token for ${t.owner}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="dev-pat-row">
        <input
          className="dev-pat-input"
          type="password"
          value={value}
          placeholder={tokens.length > 0 ? "Add another token" : "Paste token"}
          onChange={(e) => setValue(e.target.value)}
        />
        <button onClick={add} disabled={busy || !value.trim()}>
          {busy ? "Validating…" : "Add"}
        </button>
      </div>
      {flash && <span className="dev-config-flash">{flash}</span>}
    </div>
  );
}

function ReposSection({ cfg }: { cfg: ReturnType<typeof useDevConfig> }) {
  const [available, setAvailable] = useState<AvailableRepo[] | null>(null);
  const [selected, setSelected] = useState<RepoCfg[]>(cfg.config?.repos ?? []);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    setSelected(cfg.config?.repos ?? []);
  }, [cfg.config?.repos]);

  const refresh = async () => {
    setBusy(true);
    setFlash(null);
    try {
      setAvailable(await cfg.refreshRepos());
    } catch (e) {
      setFlash((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const isSel = (full: string) => selected.some((r) => r.full_name === full);

  const toggle = (repo: AvailableRepo) => {
    setSelected((prev) => {
      if (prev.some((r) => r.full_name === repo.full_name))
        return prev.filter((r) => r.full_name !== repo.full_name);
      return [
        ...prev,
        {
          full_name: repo.full_name,
          description: repo.description ?? "",
          is_default: prev.length === 0,
        },
      ];
    });
  };

  const setDescription = (full: string, description: string) =>
    setSelected((prev) =>
      prev.map((r) => (r.full_name === full ? { ...r, description } : r)),
    );

  const setDefault = (full: string) =>
    setSelected((prev) =>
      prev.map((r) => ({ ...r, is_default: r.full_name === full })),
    );

  const save = async () => {
    setBusy(true);
    setFlash(null);
    try {
      await cfg.saveRepos(selected);
      setFlash("Repos saved ✓");
    } catch (e) {
      setFlash((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // The tickable universe: fetched repos, or (before a refresh) the already-selected set.
  const universe: AvailableRepo[] =
    available ??
    selected.map((r) => ({
      full_name: r.full_name,
      description: r.description,
      private: false,
    }));

  return (
    <div className="dev-config-section">
      <h4>Repos</h4>
      <div className="dev-config-row">
        <button onClick={refresh} disabled={busy}>
          {busy ? "Loading…" : "Refresh from GitHub"}
        </button>
        <button onClick={save} disabled={busy}>
          Save repos
        </button>
      </div>
      {flash && <span className="dev-config-flash">{flash}</span>}
      <ul className="dev-repo-list">
        {universe.map((repo) => {
          const sel = selected.find((r) => r.full_name === repo.full_name);
          return (
            <li key={repo.full_name} className="dev-repo-item">
              <label className="dev-repo-tick">
                <input
                  type="checkbox"
                  checked={isSel(repo.full_name)}
                  onChange={() => toggle(repo)}
                />
                <span className="dev-repo-name">{repo.full_name}</span>
              </label>
              {sel && (
                <div className="dev-repo-detail">
                  <input
                    className="dev-repo-desc"
                    value={sel.description}
                    placeholder="one-line description (feeds the issue router)"
                    onChange={(e) =>
                      setDescription(repo.full_name, e.target.value)
                    }
                  />
                  <label className="dev-repo-default">
                    <input
                      type="radio"
                      name="dev-default-repo"
                      checked={sel.is_default}
                      onChange={() => setDefault(repo.full_name)}
                    />
                    default
                  </label>
                  <ProjectPicker cfg={cfg} repo={repo.full_name} />
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {universe.length === 0 && (
        <p className="dev-config-hint">
          Hit <strong>Refresh from GitHub</strong> to list the repos your token
          can see.
        </p>
      )}
    </div>
  );
}

function ProjectPicker({
  cfg,
  repo,
}: {
  cfg: ReturnType<typeof useDevConfig>;
  repo: string;
}) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [busy, setBusy] = useState(false);
  const current = cfg.config?.projects?.[repo];

  const load = async () => {
    setBusy(true);
    try {
      const ps = await cfg.listProjects(repo);
      setProjects(ps);
      // Auto-select when exactly one exists and none is set yet (the kaapi-backend case).
      if (ps.length === 1 && !current) await choose(ps[0].node_id, ps);
    } catch {
      setProjects([]);
    } finally {
      setBusy(false);
    }
  };

  const choose = async (nodeId: string, list = projects ?? []) => {
    const next = { ...(cfg.config?.projects ?? {}) };
    if (!nodeId) {
      delete next[repo];
    } else {
      const proj = list.find((p) => p.node_id === nodeId);
      next[repo] = { node_id: nodeId, title: proj?.title ?? "" };
    }
    await cfg.saveProjects(next);
  };

  return (
    <div className="dev-project-picker">
      {projects === null ? (
        <button onClick={load} disabled={busy}>
          {busy ? "…" : "Load projects"}
        </button>
      ) : (
        <select
          value={current?.node_id ?? ""}
          onChange={(e) => void choose(e.target.value)}
        >
          <option value="">(no default project)</option>
          {current?.node_id &&
            !projects.some((p) => p.node_id === current.node_id) && (
              <option value={current.node_id}>{current.title}</option>
            )}
          {projects.map((p) => (
            <option key={p.node_id} value={p.node_id}>
              {p.title}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

function SourcesSection({ cfg }: { cfg: ReturnType<typeof useDevConfig> }) {
  const [selected, setSelected] = useState<Set<string>>(
    new Set(cfg.config?.sources ?? []),
  );
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const tree = cfg.config?.notes_tree ?? [];

  useEffect(() => {
    setSelected(new Set(cfg.config?.sources ?? []));
  }, [cfg.config?.sources]);

  const toggle = (nodeId: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });

  const save = async () => {
    setBusy(true);
    setFlash(null);
    try {
      await cfg.saveSources([...selected]);
      setFlash("Sources saved ✓");
    } catch (e) {
      setFlash((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dev-config-section">
      <h4>Source Docs</h4>
      <p className="dev-config-hint">
        Tick meeting-notes Docs or folders. A folder means every Doc under it —
        including ones added later.
      </p>
      {tree.length === 0 ? (
        <p className="dev-config-hint">
          No notes hierarchy yet — create one in Settings → Notes.
        </p>
      ) : (
        <ul className="dev-tree">
          {tree.map((n) => (
            <TreeRow
              key={n.node_id}
              node={n}
              selected={selected}
              onToggle={toggle}
            />
          ))}
        </ul>
      )}
      <div className="dev-config-row">
        <button onClick={save} disabled={busy}>
          Save sources
        </button>
        {flash && <span className="dev-config-flash">{flash}</span>}
      </div>
    </div>
  );
}

function TreeRow({
  node,
  selected,
  onToggle,
}: {
  node: TreeNode;
  selected: Set<string>;
  onToggle: (id: string) => void;
}) {
  return (
    <li className="dev-tree-node">
      <label className="dev-tree-label">
        <input
          type="checkbox"
          checked={selected.has(node.node_id)}
          onChange={() => onToggle(node.node_id)}
        />
        <span className={`dev-tree-name dev-tree-name--${node.kind}`}>
          {node.kind === "folder" ? "📁" : "📄"} {node.name}
        </span>
      </label>
      {node.children.length > 0 && (
        <ul className="dev-tree-children">
          {node.children.map((c) => (
            <TreeRow
              key={c.node_id}
              node={c}
              selected={selected}
              onToggle={onToggle}
            />
          ))}
        </ul>
      )}
    </li>
  );
}
