import { useCallback, useState, type ReactNode } from "react";
import type { Me } from "./auth/useAuth";
import { DashboardPage } from "./DashboardPage";
import { DevView } from "./dev/DevView";
import { NewsView } from "./news/NewsView";
import { CaptureUndoToast } from "./panels/scratch/CapturePanel";
import { QuickCaptureSheet } from "./panels/scratch/QuickCaptureSheet";
import { useCapture } from "./panels/scratch/useCapture";
import { SettingsPage } from "./settings/SettingsPage";
import { Sheet } from "./Sheet";
import { useIsMobile } from "./useIsMobile";

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

/* Bottom-bar extras (goal 15): the quick-capture "+" and the "Me" account entry. */
const PLUS_ICON: ReactNode = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <path d="M12 5v14M5 12h14" />
  </svg>
);
const ME_ICON: ReactNode = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.6"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <circle cx="12" cy="9" r="3.5" />
    <path d="M5 20c1.2-3.6 4-5 7-5s5.8 1.4 7 5" />
  </svg>
);

/**
 * The app shell (goal 11): a collapsed left nav rail is now the app's spine. It
 * switches between the Home dashboard and the News view, and anchors the account
 * controls — settings + avatar + sign-out — at the bottom-left (moved off the Home
 * header). The News entry only appears when the feature is enabled for the user;
 * the settings-modal restructure and per-user flag UI are goal 12.
 *
 * At phone width (goal 15, `useIsMobile`) the rail is not rendered: a fixed bottom
 * bar (Home · News · + · Dev · Me) navigates instead, "+" opens quick capture from
 * any view, and "Me" holds the rail's account controls in a sheet. The scratchpad's
 * capture state is lifted here so quick capture and the Scratch tab share it.
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
  const isMobile = useIsMobile();
  const capture = useCapture();
  const [showMe, setShowMe] = useState(false);
  const [showCapture, setShowCapture] = useState(false);
  // The quick-capture draft outlives the sheet, so closing it keeps the text.
  const [captureDraft, setCaptureDraft] = useState("");

  // Keep each view mounted once visited so switching rails is instant and no
  // panel refetches from scratch (see .claude/rules/frontend.md — no global store;
  // hidden React subtrees retain their per-panel hook state). Home is the default
  // view so it's mounted from the start; News/Dev mount only on first visit, so we
  // don't eagerly fetch their data on page load.
  const [visited, setVisited] = useState<Set<View>>(
    () => new Set<View>(["home"]),
  );
  const navigate = useCallback((next: View) => {
    setVisited((prev) => (prev.has(next) ? prev : new Set(prev).add(next)));
    setView(next);
  }, []);

  // If a feature gets disabled out from under the current view, fall back Home.
  const activeView: View =
    (view === "news" && !newsEnabled) || (view === "dev" && !devEnabled)
      ? "home"
      : view;

  const avatar = user.picture ? (
    <img className="rail-avatar" src={user.picture} alt={user.email} />
  ) : (
    <span className="rail-avatar rail-avatar--fallback">
      {(user.name ?? user.email).charAt(0).toUpperCase()}
    </span>
  );

  return (
    <div className={`app-shell${isMobile ? " app-shell--mobile" : ""}`}>
      {!isMobile && (
        <nav className="nav-rail" aria-label="Primary">
          <div className="nav-rail-top">
            {/* The brand mark is the Home button — clicking it returns to the
              dashboard; it carries the active state when Home is showing, so the
              separate "Home" nav item is gone. */}
            <button
              className={`rail-logo-btn${activeView === "home" ? " rail-logo-btn--active" : ""}`}
              onClick={() => navigate("home")}
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
                onClick={() => navigate("news")}
              />
            )}
            {devEnabled && (
              <RailButton
                label="Dev"
                icon={NAV_ICON.dev}
                active={activeView === "dev"}
                onClick={() => navigate("dev")}
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
            {avatar}
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
      )}

      <div className="app-main">
        {/* Every visited view stays mounted; only the active one is displayed
            (`.view-pane` is display:contents, so the hidden ones drop out and the
            visible one's root behaves as a direct child of .app-main). This keeps
            per-panel state alive across rail switches — no refetch on return. */}
        <div className="view-pane" hidden={activeView !== "home"}>
          <DashboardPage capture={capture} />
        </div>
        {visited.has("news") && (
          <div className="view-pane" hidden={activeView !== "news"}>
            <NewsView />
          </div>
        )}
        {visited.has("dev") && (
          <div className="view-pane" hidden={activeView !== "dev"}>
            <DevView />
          </div>
        )}
      </div>

      {isMobile && (
        <nav className="bottom-bar" aria-label="Primary">
          <BarButton
            label="Home"
            icon={NAV_ICON.home}
            active={activeView === "home"}
            onClick={() => navigate("home")}
          />
          {newsEnabled && (
            <BarButton
              label="News"
              icon={NAV_ICON.news}
              active={activeView === "news"}
              onClick={() => navigate("news")}
            />
          )}
          <button
            className="bottom-bar-btn bottom-bar-plus"
            onClick={() => setShowCapture(true)}
            aria-label="Quick capture"
          >
            <span className="bottom-bar-plus-icon">{PLUS_ICON}</span>
          </button>
          {devEnabled && (
            <BarButton
              label="Dev"
              icon={NAV_ICON.dev}
              active={activeView === "dev"}
              onClick={() => navigate("dev")}
            />
          )}
          <BarButton
            label="Me"
            icon={ME_ICON}
            active={false}
            onClick={() => setShowMe(true)}
          />
        </nav>
      )}

      {isMobile && showMe && (
        <Sheet label="Account" onClose={() => setShowMe(false)}>
          <div className="me-sheet-head">
            {avatar}
            <div className="me-sheet-who">
              <h3 className="sheet-title">{user.name ?? "Signed in"}</h3>
              <span className="sheet-sub">{user.email}</span>
            </div>
          </div>
          <div className="sheet-list">
            <button
              onClick={() => {
                setShowMe(false);
                setShowSettings(true);
              }}
            >
              <span className="sheet-list-icon">{SETTINGS_ICON}</span>
              Settings
            </button>
            <button className="sheet-list--danger" onClick={onSignOut}>
              <span className="sheet-list-icon">{SIGNOUT_ICON}</span>
              Sign out
            </button>
          </div>
        </Sheet>
      )}

      {isMobile && showCapture && (
        <QuickCaptureSheet
          capture={capture}
          draft={captureDraft}
          setDraft={setCaptureDraft}
          onClose={() => setShowCapture(false)}
          onRestore={(held) => {
            setCaptureDraft((cur) => (cur.trim() ? `${held}\n\n${cur}` : held));
            setShowCapture(true);
          }}
        />
      )}

      <CaptureUndoToast capture={capture} />

      {showSettings && (
        <SettingsPage user={user} onClose={() => setShowSettings(false)} />
      )}
    </div>
  );
}

/** A bottom-bar item (goal 15): icon over label, the phone's version of RailButton. */
function BarButton({
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
      className={`bottom-bar-btn${active ? " bottom-bar-btn--active" : ""}`}
      onClick={onClick}
      aria-current={active ? "page" : undefined}
    >
      <span className="bottom-bar-icon">{icon}</span>
      <span className="bottom-bar-label">{label}</span>
    </button>
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
