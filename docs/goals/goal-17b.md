# Goal 17b — Drop the Google Tasks scope and the import scaffolding

**One line:** Once every user has been imported (goal 17), remove the `tasks` OAuth scope, delete
the importer and the Google Tasks client, and drop the legacy `task_overlay` table. After this,
the app holds exactly `{identity, calendar.readonly, drive.file}`. Depends on goal 17. It is
independent of goal 17a.

## Precondition (check before starting)

Every row in `user` has `tasks_imported_at` set. The owner steps include a one-line query for
this. If a user is still pending, run the importer CLI for them first (`--dry-run`, then for
real), or explicitly accept that they start empty.

## Why

- **Least privilege.** A leaked token should not be able to read or write anyone's Google Tasks
  when the app no longer uses them. This is the same reasoning as the drive ADR's Layer 1.
- **Remove dead code.** The importer is the last Tasks API caller. Keeping it invites someone to
  use it again.

## The catch: the per-token scope assertion

`ALLOWED_SCOPES` refuses to serve any token that is **broader** than the allowlist. Every existing
token was granted `tasks`, and Google does not take back a scope just because a later consent
asks for fewer. So removing `tasks` from `ALLOWED_SCOPES` straight away would 403 every user.

The rollout:

1. **Stop requesting the scope.** Remove `tasks` from `API_SCOPES`, so new consents are narrower.
2. **Tolerate it temporarily.** Add a `LEGACY_TOLERATED_SCOPES = {tasks}` set to the assertion.
   Tokens carrying it are still served, and a warning is logged.
3. **Users re-consent.** Each user revokes the app at myaccount.google.com/permissions and signs
   in again (owner steps). Each new grant is narrow, and `granted_scopes` shows it.
4. **Remove the tolerance.** Once every user's `granted_scopes` lacks `tasks`, a follow-up commit
   in this same goal deletes `LEGACY_TOLERATED_SCOPES`. The assertion is then strict again.

## What ships

- **`app/google/auth.py`:** the scope changes above. Settings shows a small "Re-consent needed"
  hint to a user whose token still carries `tasks`. It's a nudge, not a block.
- **Deleted:**
  - `app/google/tasks.py`;
  - `app/tasks_store/importer.py` and its CLI and tests;
  - the startup and hourly import sweep and the first-load import hook;
  - the `503 tasks_import_pending` path and the "Importing your tasks…" panel state.
- **New users:** they get both pinned lists created empty on first sign-in.
- **Alembic:** drop `task_overlay`. The `google_tasklist_id` and `google_task_id` columns **stay**
  as inert provenance, so nothing needs a rewrite.
- **The AST pin from goal 17** flips to "no module imports `app.google.tasks`", because the
  module no longer exists.

## Acceptance criteria

- **Fresh consent:** the screen lists only identity, Calendar (read-only), and Drive (file
  scoped), with no Tasks.
- **Legacy tokens:** a token still carrying `tasks` is served during the tolerance phase, with a
  warning logged and the Settings hint shown. After the tolerance is removed, the same token gets
  a 403 (the test covers both phases).
- **New users:** a new user's first `GET /tasks` returns two empty pinned lists.
- **The Google Tasks API is disabled** in the Cloud project (owner step), and the app runs
  normally afterwards.
- **Regression:** all tests, `tsc`, the build, and `ruff` pass. The migration applies on SQLite
  (prod runs SQLite).

## Harness upkeep

- **The drive ADR, Layer 4:** the allowlist becomes `{calendar readonly, drive.file}` plus
  identity.
- **`CLAUDE.md` and `docs/goals/README.md`:** remove the remaining Tasks API mentions, and add the
  goal-17b line.
- **Owner steps:** `goal-17b-owner-steps.md`.
