import { useState } from "react";
import type { Me } from "./auth/useAuth";
import { DashboardPage } from "./DashboardPage";
import { NewsView } from "./news/NewsView";
import { SettingsPage } from "./settings/SettingsPage";

type View = "home" | "news";

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

  // If News gets disabled out from under the current view, fall back Home.
  const activeView: View = view === "news" && !newsEnabled ? "home" : view;

  return (
    <div className="app-shell">
      <nav className="nav-rail" aria-label="Primary">
        <div className="nav-rail-top">
          <img className="nav-rail-logo" src="/logo-mark.svg" alt="" />
          <RailButton
            label="Home"
            icon="🏠"
            active={activeView === "home"}
            onClick={() => setView("home")}
          />
          {newsEnabled && (
            <RailButton
              label="News"
              icon="📰"
              active={activeView === "news"}
              onClick={() => setView("news")}
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
            <span className="rail-icon">⚙</span>
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
            <span className="rail-icon">⏻</span>
          </button>
        </div>
      </nav>

      <div className="app-main">
        {activeView === "home" ? <DashboardPage /> : <NewsView />}
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
  icon: string;
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
