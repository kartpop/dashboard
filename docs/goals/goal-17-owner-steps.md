# Goal 17 — owner steps (cutover from Google Tasks to the app DB)

Do these in order around the goal-17 deploy.

## Before the deploy

- [ ] **Pick the window.** Google's daily API quotas reset at **midnight Pacific time**. Until
  1 Nov 2026 that is **12:30 PM IST**; after the US DST change it becomes 1:30 PM IST. Deploy
  in the **afternoon IST, a few hours after the reset**. The import needs only a few hundred
  Tasks calls for all users, but it needs *some* quota left.
  - **Check it:** Cloud Console → APIs & Services → Google Tasks API → Quotas. "Queries per day"
    should be well below 50k.
- [ ] **Reduce the quota burn before the deploy.** Close the dashboard tabs on every device (laptop
  and phone) for the hour before. Each open tab polls the Tasks API about twice a minute through
  the old code.
- [ ] **Back up the prod SQLite DB** on the EC2 host, right before deploying:
  `docker compose exec -T app uv run python scripts/backup.py`. It writes a WAL-safe copy under
  `/data/backups`. Also copy that file off the box once
  (`docker compose cp app:/data/backups/<file> ./`, then `scp` it to the laptop). This is the
  real rollback point for app state.
- [ ] **Optional dry run on prod, before the deploy:** none is possible. The importer ships with
  the new build. Do the dry run straight after deploying instead (below). It is read-only, and
  the startup sweep waits about 10 seconds, so if you want the dry run to happen first, set
  `TASKS_IMPORT_SWEEP_ENABLED=0` in `.env.prod` for the first boot.

## The deploy

- [ ] Deploy as usual: `docker compose --env-file .env.prod up -d --build`. The entrypoint runs
  `alembic upgrade head`.
- [ ] *(If you disabled the sweep)* do the dry run, check the counts, then import for real:
  `docker compose exec -T app uv run python -m app.tasks_store.importer --user <email> --dry-run`,
  then the same command without `--dry-run`, for each user. Alternatively, remove
  `TASKS_IMPORT_SWEEP_ENABLED=0` and restart.
- [ ] **Watch the logs** for the startup import sweep. Expect one "imported user N: L lists, T
  open, C completed, O overlay rows, S steps repointed, U unlinked" line per user.
- [ ] **Confirm every user is imported:**
  `docker compose exec -T app sqlite3 /data/overlay.db 'SELECT email, tasks_imported_at FROM user;'`
  Every row should have a timestamp. If a user is pending, run the CLI for them, or let their
  next load import them. The hourly sweep also retries.

## Right after

- [ ] Open the dashboard on the laptop and the phone. Your lists, groups, order, and thread
  badges should match what you had.
- [ ] Complete a task and watch it for a minute. It should not come back.
- [ ] **Watch the quota** for a day: Cloud Console → Google Tasks API traffic should drop to
  roughly zero after the import.
- [ ] Tell the other users: "Your tasks now live in the dashboard. The Google Tasks app is frozen
  as of today and no longer syncs."

## Rollback (only if the import went badly)

Google Tasks is untouched and frozen at cutover, and `task_overlay` is still present. To roll
back:

1. Run `docker compose exec -T app uv run alembic downgrade b8c9d0e1f2a3`. This drops the new
   tables and column.
2. Redeploy the pre-17 build (`git checkout` the previous `main` commit, then `up -d --build`).

You lose only task changes made after the cutover.
