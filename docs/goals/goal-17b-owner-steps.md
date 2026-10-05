# Goal 17b — owner steps (drop the Google Tasks scope)

- [ ] **Precondition:** `docker compose exec -T app sqlite3 /data/overlay.db 'SELECT email, tasks_imported_at FROM user;'`
  shows a timestamp on every row.
- [ ] Deploy goal 17b phase 1 (scope no longer requested; legacy tolerance on).
- [ ] **Each user re-consents:**
  1. Open myaccount.google.com/permissions.
  2. Find the dashboard app → **Remove access**.
  3. Sign in to the dashboard again.

  **On the consent screen:** it should list Calendar (read-only) and "only the specific Google
  Drive files you use with this app", **and no Tasks**. If Tasks is listed, abort and report it.
- [ ] Confirm no row's `granted_scopes` contains `auth/tasks`:
  `docker compose exec -T app sqlite3 /data/overlay.db 'SELECT email, granted_scopes FROM user;'`
- [ ] Deploy phase 2 (tolerance removed).
- [ ] Cloud Console → APIs & Services → **Google Tasks API → Disable**.
- [ ] Use the dashboard for a day: tasks, threads, router capture, and Export now should all still
  work.
