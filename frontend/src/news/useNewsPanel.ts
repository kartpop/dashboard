import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, apiPut } from "../api";

export interface NewsItem {
  id: number;
  source: string;
  feed: string;
  title: string;
  url: string;
  synopsis: string;
  domain: string | null;
  why_line: string | null;
  is_serendipity: boolean;
  published_at: string | null;
  run_date: string;
  vote: number; // +1 / -1 / 0
  comment: string | null;
}

interface FeedResponse {
  items: NewsItem[];
  last_run_at: string | null;
}

export interface NewsProfile {
  profile: string;
  has_prev: boolean;
  last_daily_at: string | null;
  last_weekly_at: string | null;
}

/**
 * The News view's data hook (goal 11) — owns the feed, feedback, and the profile doc.
 * Feedback is optimistic (the card updates immediately; the POST fires without a
 * reload), matching the dashboard's optimistic-write convention.
 */
export function useNewsPanel() {
  const [items, setItems] = useState<NewsItem[]>([]);
  const [lastRunAt, setLastRunAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await apiGet<FeedResponse>("/news");
      setItems(data.items);
      setLastRunAt(data.last_run_at);
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

  const fetchNow = useCallback(async () => {
    setFetching(true);
    try {
      await apiPost("/news/fetch-now", {});
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setFetching(false);
    }
  }, [load]);

  const setFeedback = useCallback(
    (itemId: number, vote: number, comment: string | null) => {
      // Optimistic: update the card now, fire the write without awaiting/reloading.
      setItems((prev) =>
        prev.map((i) =>
          i.id === itemId ? { ...i, vote, comment: comment ?? null } : i,
        ),
      );
      void apiPost(`/news/${itemId}/feedback`, { vote, comment }).catch((e) =>
        setError((e as Error).message),
      );
    },
    [],
  );

  return {
    items,
    lastRunAt,
    loading,
    fetching,
    error,
    reload: load,
    fetchNow,
    setFeedback,
  };
}

/** Separate hook for the in-view profile drawer (view/edit + revert). */
export function useNewsProfile() {
  const [profile, setProfile] = useState<NewsProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setProfile(await apiGet<NewsProfile>("/news/profile"));
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const save = useCallback(async (text: string) => {
    setProfile(await apiPut<NewsProfile>("/news/profile", { profile: text }));
  }, []);

  const revert = useCallback(async () => {
    setProfile(await apiPost<NewsProfile>("/news/profile/revert", {}));
  }, []);

  return { profile, error, load, save, revert };
}
