import { AppShell } from "./AppShell";
import { SignInPage } from "./auth/SignInPage";
import { useAuth } from "./auth/useAuth";

export function App() {
  const auth = useAuth();
  if (auth.status === "loading") {
    return <div className="app-loading">Loading…</div>;
  }
  if (auth.status === "signedOut" || !auth.user) {
    return <SignInPage />;
  }
  return <AppShell user={auth.user} onSignOut={auth.signOut} />;
}
