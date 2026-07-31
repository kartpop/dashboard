import { useState, type ReactNode } from "react";
import type { Me } from "./auth/useAuth";
import { DashboardPage } from "./DashboardPage";
import { DevView } from "./dev/DevView";
import { NewsView } from "./news/NewsView";
import { SettingsPage } from "./settings/SettingsPage";

type View = "home" | "news" | "dev";

/* Flat, single-weight line icons for the nav rail — matched to the flat logo
   mark. currentColor lets the active/hover states tint them. */
const NAV_ICON: Record<View, ReactNode> = {
  home: (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 10.5 12 4l8 6.5" />
      <path d="M6 9.5V20h4v-6h4v6h4V9.5" />
    </svg>
  ),
  news: (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="5" width="16" height="14" rx="1.5" />
      <path d="M7.5 9h9M7.5 12.5h9M7.5 16h5" />
    </svg>
  ),
  dev: (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m9 8-4 4 4 4M15 8l4 4-4 4" />
    </svg>
  ),
};

/* Bottom-rail utility icons — same flat line treatment as the nav icons. */
const SETTINGS_ICON: ReactNode = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.6"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);
const SIGNOUT_ICON: ReactNode = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.6"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M12 4v8" />
    <path d="M7.5 7a7 7 0 1 0 9 0" />
  </svg>
);

/**
 * The app shell (goal 11): a collapsed left nav rail is now the app's spine. It
 * switches between the Home dashboard and the News view, and anchors the account
 * controls — settings + avatar + sign-out — at the bottom-left (moved off the Home
 * header). The News entry only appears when the feature is enabled for the user;
 * the settings-modal restructure and per-user flag UI are goal 12.
 */
export function AppShell({
  user,
  onSignOut,
}: {
  user: Me;
  onSignOut: () => void;
}) {
  const [view, setView] = useState<View>("home");
  const [showSettings, setShowSettings] = useState(false);
  const newsEnabled = user.news_enabled === true;
  const devEnabled = user.dev_enabled === true;

  // If a feature gets disabled out from under the current view, fall back Home.
  const activeView: View =
    (view === "news" && !newsEnabled) || (view === "dev" && !devEnabled)
      ? "home"
      : view;

  return (
    <div className="app-shell">
      <nav className="nav-rail" aria-label="Primary">
        <div className="nav-rail-top">
          {/* The brand mark is the Home button — clicking it returns to the
              dashboard; it carries the active state when Home is showing, so the
              separate "Home" nav item is gone. */}
          <button
            className={`rail-logo-btn${activeView === "home" ? " rail-logo-btn--active" : ""}`}
            onClick={() => setView("home")}
            title="Home"
            aria-label="Home"
            aria-current={activeView === "home" ? "page" : undefined}
          >
            <img className="nav-rail-logo" src="/logo-mark.svg" alt="" />
          </button>
          {newsEnabled && (
            <RailButton
              label="News"
              icon={NAV_ICON.news}
              active={activeView === "news"}
              onClick={() => setView("news")}
            />
          )}
          {devEnabled && (
            <RailButton
              label="Dev"
              icon={NAV_ICON.dev}
              active={activeView === "dev"}
              onClick={() => setView("dev")}
            />
          )}
        </div>
        <div className="nav-rail-bottom">
          <button
            className="rail-btn"
            onClick={() => setShowSettings(true)}
            title="Settings"
            aria-label="Settings"
          >
            <span className="rail-icon">{SETTINGS_ICON}</span>
          </button>
          {user.picture ? (
            <img className="rail-avatar" src={user.picture} alt={user.email} />
          ) : (
            <span className="rail-avatar rail-avatar--fallback">
              {(user.name ?? user.email).charAt(0).toUpperCase()}
            </span>
          )}
          <button
            className="rail-btn"
            onClick={onSignOut}
            title="Sign out"
            aria-label="Sign out"
          >
            <span className="rail-icon">{SIGNOUT_ICON}</span>
          </button>
        </div>
      </nav>

      <div className="app-main">
        {activeView === "home" ? (
          <DashboardPage />
        ) : activeView === "news" ? (
          <NewsView />
        ) : (
          <DevView />
        )}
      </div>

      {showSettings && (
        <SettingsPage user={user} onClose={() => setShowSettings(false)} />
      )}
    </div>
  );
}

function RailButton({
  label,
  icon,
  active,
  onClick,
}: {
  label: string;
  icon: ReactNode;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className={`rail-btn rail-nav${active ? " rail-nav--active" : ""}`}
      onClick={onClick}
      title={label}
      aria-current={active ? "page" : undefined}
    >
      <span className="rail-icon">{icon}</span>
      <span className="rail-label">{label}</span>
    </button>
  );
}
