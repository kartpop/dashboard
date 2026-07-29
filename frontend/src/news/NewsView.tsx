import { useEffect, useMemo, useState } from "react";
import { formatRelative, formatRunDay } from "../formatDate";
import { type NewsItem, useNewsPanel, useNewsProfile } from "./useNewsPanel";

/**
 * The News view (goal 11): a vertical list of cards grouped by run-day. Each card's
 * headline links out to the original article (new tab) — the app is a launcher, not
 * a reader. Serendipity picks are interleaved with a ✨ badge (not siloed), honoring
 * the anti-filter-bubble intent. Feedback (👍/👎 + a collapsed comment) sits quiet on
 * the bottom row so the resting view stays a clean skim.
 */
export function NewsView() {
  const news = useNewsPanel();
  const [showProfile, setShowProfile] = useState(false);

  const groups = useMemo(() => groupByRunDay(news.items), [news.items]);

  return (
    <main className="news-view">
      <header className="news-header">
        <h1>News</h1>
        <div className="news-header-meta">
          {news.lastRunAt && (
            <span className="news-last-run">
              Updated {formatRelative(news.lastRunAt)}
            </span>
          )}
          <button
            className="news-profile-btn"
            onClick={() => setShowProfile((s) => !s)}
          >
            Profile
          </button>
          <button
            className="news-fetch-btn"
            onClick={news.fetchNow}
            disabled={news.fetching}
          >
            {news.fetching ? "Fetching…" : "Fetch now"}
          </button>
        </div>
      </header>

      {showProfile && <ProfileDrawer onClose={() => setShowProfile(false)} />}

      {news.error && <p className="news-error">{news.error}</p>}

      {news.loading ? (
        <p className="news-empty">Loading…</p>
      ) : news.items.length === 0 ? (
        <p className="news-empty">
          No news yet. Hit <strong>Fetch now</strong> to pull today's feed.
        </p>
      ) : (
        <div className="news-list">
          {groups.map(([runDate, items]) => (
            <section key={runDate} className="news-daygroup">
              <h2 className="news-dayhead">{formatRunDay(runDate)}</h2>
              {items.map((item) => (
                <NewsCard
                  key={item.id}
                  item={item}
                  onFeedback={news.setFeedback}
                />
              ))}
            </section>
          ))}
        </div>
      )}
    </main>
  );
}

function groupByRunDay(items: NewsItem[]): [string, NewsItem[]][] {
  const map = new Map<string, NewsItem[]>();
  for (const item of items) {
    const list = map.get(item.run_date) ?? [];
    list.push(item);
    map.set(item.run_date, list);
  }
  // Server returns newest run first; preserve that insertion order.
  return [...map.entries()];
}

function NewsCard({
  item,
  onFeedback,
}: {
  item: NewsItem;
  onFeedback: (id: number, vote: number, comment: string | null) => void;
}) {
  const [showComment, setShowComment] = useState(false);
  const [draft, setDraft] = useState(item.comment ?? "");

  useEffect(() => {
    setDraft(item.comment ?? "");
  }, [item.comment]);

  const toggleVote = (vote: number) =>
    onFeedback(item.id, item.vote === vote ? 0 : vote, item.comment);

  const commitComment = () => {
    const next = draft.trim();
    if (next !== (item.comment ?? ""))
      onFeedback(item.id, item.vote, next || null);
  };

  return (
    <article
      className={`news-card${item.is_serendipity ? " news-card--serendipity" : ""}`}
    >
      <div className="news-card-top">
        <span className="news-source">{item.feed || item.source}</span>
        {item.published_at && (
          <span className="news-time">
            · {formatRelative(item.published_at)}
          </span>
        )}
        {item.is_serendipity && (
          <span
            className="news-badge"
            title="An off-profile pick — outside your usual"
          >
            ✨ serendipity
          </span>
        )}
        {item.domain && !item.is_serendipity && (
          <span className="news-domain">{item.domain}</span>
        )}
      </div>

      <a
        className="news-title"
        href={item.url}
        target="_blank"
        rel="noopener noreferrer"
      >
        {item.title}
      </a>

      {item.why_line && <p className="news-why">{item.why_line}</p>}

      <div className="news-card-foot">
        <button
          className={`news-vote${item.vote === 1 ? " news-vote--on" : ""}`}
          onClick={() => toggleVote(1)}
          aria-label="Thumbs up"
        >
          👍
        </button>
        <button
          className={`news-vote${item.vote === -1 ? " news-vote--on" : ""}`}
          onClick={() => toggleVote(-1)}
          aria-label="Thumbs down"
        >
          👎
        </button>
        <button
          className={`news-comment-toggle${item.comment ? " news-comment-toggle--has" : ""}`}
          onClick={() => setShowComment((s) => !s)}
          aria-label="Comment"
          title={item.comment ?? "Add a comment"}
        >
          💬{item.comment ? " ·" : ""}
        </button>
      </div>

      {showComment && (
        <textarea
          className="news-comment-box"
          value={draft}
          placeholder="Why? (e.g. too incremental — I only care about capability jumps)"
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitComment}
          rows={2}
          autoFocus
        />
      )}
    </article>
  );
}

/**
 * The minimal in-view profile editor (goal 11). The full settings treatment —
 * braindump + LLM-recreate + a chip feed picker — is goal 12; here it is a raw
 * markdown box the curator reads, with a one-click revert to the retained version.
 */
function ProfileDrawer({ onClose }: { onClose: () => void }) {
  const { profile, error, load, save, revert } = useNewsProfile();
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (profile) setDraft(profile.profile);
  }, [profile]);

  const onSave = async () => {
    setSaving(true);
    try {
      await save(draft);
      setFlash("Saved ✓");
    } catch (e) {
      setFlash((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const onRevert = async () => {
    setSaving(true);
    try {
      await revert();
      setFlash("Reverted to previous version");
    } catch (e) {
      setFlash((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="news-profile-drawer">
      <div className="news-profile-head">
        <h3>Your news profile</h3>
        <button
          className="news-profile-close"
          onClick={onClose}
          aria-label="Close"
        >
          ✕
        </button>
      </div>
      <p className="news-profile-hint">
        The curator reads this markdown to pick your feed. A weekly job rewrites
        it from your 👍/👎 + comments — you can also edit it by hand here.
      </p>
      {error && <p className="news-error">{error}</p>}
      <textarea
        className="news-profile-text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={12}
      />
      <div className="news-profile-actions">
        <button onClick={onSave} disabled={saving}>
          Save
        </button>
        <button
          onClick={onRevert}
          disabled={saving || !profile?.has_prev}
          title={
            profile?.has_prev
              ? "Restore the previous version"
              : "No previous version"
          }
        >
          Revert
        </button>
        {flash && <span className="news-profile-flash">{flash}</span>}
      </div>
    </section>
  );
}
