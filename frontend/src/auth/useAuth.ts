import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, setUnauthorizedHandler } from "../api";

export interface Me {
  id: number;
  email: string;
  name: string | null;
  picture: string | null;
  is_superuser: boolean;
  // Goal 11: whether the News feature is on for this user (drives the rail entry).
  news_enabled?: boolean;
}

export type AuthStatus = "loading" | "signedOut" | "signedIn";

/**
 * Single source of truth for auth (goal 8). Checks `/auth/me` on mount, flips the
 * whole app to the sign-in screen on any 401 (via the api.ts unauthorized handler),
 * and exposes sign-out. Lifted in `App` so the dashboard renders only when signed in.
 */
export function useAuth() {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<Me | null>(null);

  const check = useCallback(async () => {
    try {
      const me = await apiGet<Me>("/auth/me");
      setUser(me);
      setStatus("signedIn");
    } catch {
      setUser(null);
      setStatus("signedOut");
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      setStatus("signedOut");
    });
    void check();
    return () => setUnauthorizedHandler(null);
  }, [check]);

  const signOut = useCallback(async () => {
    try {
      await apiPost("/auth/logout", {});
    } finally {
      setUser(null);
      setStatus("signedOut");
    }
  }, []);

  return { status, user, signOut, refresh: check };
}
