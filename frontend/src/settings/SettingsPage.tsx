import { useCallback, useEffect, useState } from "react";
import { apiDelete, apiGet, apiPost, apiPut } from "../api";
import type { Me } from "../auth/useAuth";
import { NotesHierarchy } from "./NotesHierarchy";

interface CalendarOption {
  id: string;
  summary: string;
  primary: boolean;
  background_color: string | null;
  enabled: boolean;
}

interface SettingsResponse {
  calendars: CalendarOption[];
  enabled_calendar_ids: string[];
  notes_folder_id: string | null;
  notes_doc_id: string | null;
  notes_folder_url: string | null;
  notes_doc_url: string | null;
}

interface FeatureDef {
  key: string;
  label: string;
}

interface AllowedEmail {
  id: number;
  email: string;
  added_by: string | null;
  created_at: string;
  features: Record<string, boolean>;
}

interface AllowedEmailsResponse {
  features: FeatureDef[];
  allowed: AllowedEmail[];
}

export function SettingsPage({
  user,
  onClose,
}: {
  user: Me;
  onClose: () => void;
}) {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [calendars, setCalendars] = useState<CalendarOption[]>([]);
  const [addId, setAddId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState(false);

  useEffect(() => {
    let alive = true;
    apiGet<SettingsResponse>("/settings")
      .then((s) => {
        if (!alive) return;
        setData(s);
        setCalendars(s.calendars);
      })
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, []);

  const toggle = (id: string) =>
    setCalendars((cs) =>
      cs.map((c) =>
        c.id === id && !c.primary ? { ...c, enabled: !c.enabled } : c,
      ),
    );

  const addById = () => {
    const id = addId.trim();
    if (!id || calendars.some((c) => c.id === id)) {
      setAddId("");
      return;
    }
    setCalendars((cs) => [
      ...cs,
      {
        id,
        summary: id,
        primary: false,
        background_color: null,
        enabled: true,
      },
    ]);
    setAddId("");
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const ids = calendars
        .filter((c) => c.enabled && !c.primary)
        .map((c) => c.id);
      await apiPut("/settings/calendars", { calendar_ids: ids });
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 1500);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="settings-overlay" onClick={onClose}>
      <div className="settings-modal" onClick={(e) => e.stopPropagation()}>
        <header className="settings-head">
          <h2>Settings</h2>
          <button
            className="settings-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </header>

        {error && <p className="settings-error">{error}</p>}

        <section className="settings-section">
          <h3>Calendars</h3>
          <p className="settings-hint">
            Choose which calendars merge into the day strip. Your primary
            calendar is always shown.
          </p>
          <ul className="settings-cal-list">
            {calendars.map((c) => (
              <li key={c.id}>
                <label>
                  <input
                    type="checkbox"
                    checked={c.enabled}
                    disabled={c.primary}
                    onChange={() => toggle(c.id)}
                  />
                  {c.background_color && (
                    <span
                      className="settings-cal-dot"
                      style={{ background: c.background_color }}
                    />
                  )}
                  <span>{c.summary}</span>
                  {c.primary && <span className="settings-tag">primary</span>}
                </label>
              </li>
            ))}
          </ul>
          <div className="settings-add-row">
            <input
              placeholder="Add calendar by ID (e.g. team@group.calendar.google.com)"
              value={addId}
              onChange={(e) => setAddId(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addById()}
            />
            <button onClick={addById}>Add</button>
          </div>
          <div className="settings-actions">
            <button className="settings-save" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save calendars"}
            </button>
            {savedFlash && <span className="settings-saved">Saved ✓</span>}
          </div>
        </section>

        <section className="settings-section">
          <h3>Notes</h3>
          <p className="settings-hint">
            Routed notes are appended to a Google Doc the app created in your
            Drive.
          </p>
          {data?.notes_doc_url ? (
            <ul className="settings-links">
              <li>
                <a href={data.notes_doc_url} target="_blank" rel="noreferrer">
                  Open notes Doc ↗
                </a>
              </li>
              {data.notes_folder_url && (
                <li>
                  <a
                    href={data.notes_folder_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open notes folder ↗
                  </a>
                </li>
              )}
            </ul>
          ) : (
            <p className="settings-hint">
              Your notes Doc will be created on first use.
            </p>
          )}
        </section>

        <NotesHierarchy />

        {user.is_superuser && <AllowedEmails />}
      </div>
    </div>
  );
}

function AllowedEmails() {
  const [rows, setRows] = useState<AllowedEmail[]>([]);
  const [features, setFeatures] = useState<FeatureDef[]>([]);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    apiGet<AllowedEmailsResponse>("/settings/allowed-emails")
      .then((r) => {
        setRows(r.allowed);
        setFeatures(r.features);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => load(), [load]);

  const add = async () => {
    const e = email.trim().toLowerCase();
    if (!e) return;
    setError(null);
    try {
      await apiPost("/settings/allowed-emails", { email: e });
      setEmail("");
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const remove = async (e: string) => {
    setError(null);
    try {
      await apiDelete(`/settings/allowed-emails/${encodeURIComponent(e)}`);
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const toggleFeature = async (
    row: AllowedEmail,
    key: string,
    enabled: boolean,
  ) => {
    setError(null);
    // Optimistic: reflect the toggle immediately, roll back on failure.
    setRows((rs) =>
      rs.map((r) =>
        r.id === row.id
          ? { ...r, features: { ...r.features, [key]: enabled } }
          : r,
      ),
    );
    try {
      await apiPut(
        `/settings/allowed-emails/${encodeURIComponent(row.email)}/features`,
        { feature: key, enabled },
      );
    } catch (err) {
      setError((err as Error).message);
      load();
    }
  };

  return (
    <section className="settings-section">
      <h3>Allowed emails</h3>
      <p className="settings-hint">
        Only these Google accounts (plus you) can sign in. Removing one blocks
        future sign-ins. The checkboxes turn features on per person — you (the
        superuser) always have every feature.
      </p>
      {error && <p className="settings-error">{error}</p>}
      <ul className="settings-allow-list">
        {rows.map((r) => (
          <li key={r.id}>
            <span className="settings-allow-email">{r.email}</span>
            <span className="settings-allow-features">
              {features.map((f) => (
                <label key={f.key} className="settings-feature-toggle">
                  <input
                    type="checkbox"
                    checked={r.features[f.key] ?? false}
                    onChange={(e) => toggleFeature(r, f.key, e.target.checked)}
                  />
                  {f.label}
                </label>
              ))}
            </span>
            <button
              onClick={() => remove(r.email)}
              aria-label={`Remove ${r.email}`}
            >
              Remove
            </button>
          </li>
        ))}
        {rows.length === 0 && (
          <li className="settings-hint">No invites yet.</li>
        )}
      </ul>
      <div className="settings-add-row">
        <input
          type="email"
          placeholder="friend@gmail.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
        />
        <button onClick={add}>Invite</button>
      </div>
    </section>
  );
}
