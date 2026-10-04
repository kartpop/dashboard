# Dashboard

A personal, **multi-user** productivity dashboard. It pulls your Google Tasks and Calendar onto
one screen, adds a smarter layer for organizing tasks, and gives you a **scratchpad** that reads
whatever you dump into it and files it in the right place automatically. Everyone signs in with
their own Google account and gets a private, fully-isolated dashboard.

---

## What it does

### ✅ Tasks — "My Tasks" and "Follow-ups"

Two task lists, backed by **Google Tasks** — so anything you change here syncs straight to Google
and shows up on your phone.

- **Opinionated on purpose:** the dashboard shows exactly two lists and nothing else —
  **My Tasks** (your own to-dos) and **Follow-ups** (things you're waiting on or chasing someone
  else about). You need both lists, named *exactly* `My Tasks` and `Follow-ups`, in your Google
  Tasks. Every Google account already has `My Tasks`; add `Follow-ups` yourself. (Tasks in any
  other Google list simply aren't shown.)
- **Drag to prioritize** — reorder tasks within a day, and drag them across day-buckets or even
  across the two lists in one motion. Position *is* the priority — there's no separate priority
  field.
- **Group related tasks** into named clusters within a day. Ordering and grouping are a local
  layer on top of Google: Google still sees a plain list, but the dashboard remembers your
  arrangement.
- **Due dates** via a date picker or by dragging into a day; overdue tasks roll up into an
  **Overdue** bucket at the top.
- **Edit, complete, and delete** tasks inline. No confirm dialogs — a delete gets a ~5-second
  **Undo** toast instead, and a completion can be undone the same way.
- **Subtasks aren't supported yet** — they render flat for now (proper nesting comes later).

### 🧵 Threads — the story behind each task

My Tasks and Follow-ups show only the latest step of each piece of work. The **Threads** panel
(under the two task columns) keeps the whole chain: what already happened, plus the open **next
steps**, each a real task in My Tasks (your move) or Follow-ups (their move). A thread can have
several open at once (visit one partner, email another) — they stack side by side.

- **Complete the task anywhere** (here, your phone, Google Tasks) and the thread logs it as done; once
  nothing is left open it asks **"What's next?"**. Threads with nothing scheduled sort to the top, so dropped threads are
  hard to miss.
- **Log updates** as you go, open any step for its note (a next step's note *is* the task's
  description), switch a next step between My Tasks and Follow-ups, or archive a thread. Archiving
  or removing a step never deletes the Google task.
- Tasks that belong to a thread carry a small badge; click it to jump to the thread.

### 📝 Scratchpad — dump a thought, it files itself

A capture box for half-formed thoughts. Whatever you type gets **auto-routed**: an actionable
to-do becomes a task (in My Tasks or Follow-ups), and a "remember this" note gets appended to one
of your Google Docs.

- **You define your own notes structure** in **Settings** — a tree of folders and Docs (e.g.
  `work / conversations / john-doe`). The router reads each note and files it into the
  best-matching Doc; anything that doesn't clearly fit goes to a default Doc.
- **Ambiguous captures go to a Review queue** instead of being guessed. You confirm or redirect
  them with a click — pick the destination Doc, tweak the text, or turn it into a task.
- **Notes are written verbatim** into your Doc (newest at the top, timestamped, with a one-line
  summary headline). The app only ever *adds* to your Docs — it never edits or deletes what's
  already there.
- **Every capture has a short undo window** — hit Undo and nothing is written anywhere.

### 📅 Calendar — read-only day strip

A compact strip of the day's meetings across the top of the dashboard.

- **Read-only** — add or change events in Google Calendar as usual; the dashboard just displays
  them (with Meet links and a live "now" marker).
- **Add multiple calendars** in **Settings** (e.g. a work calendar shared into your account). Your
  primary Google account needs at least **read** access to any calendar you want shown.

### 👤 Multi-user

Sign in with Google; each person gets their own isolated dashboard — their tasks, notes, settings,
and an encrypted refresh token, never visible to anyone else. An email allowlist controls who can
sign in.

---

## Under the hood

- **Backend:** FastAPI (Python). Calls the Google APIs directly (`backend/app/google/`). The only
  place an LLM runs is the scratchpad router.
- **Frontend:** React + Vite. One self-contained panel per surface (`frontend/src/panels/`).
- **Storage:** SQLite (WAL) for the task-metadata overlay (custom order + grouping), per-user
  settings, and auth rows. Google remains the source of truth for tasks, calendar, and docs.
- **Auth:** Google Sign-In is the OAuth grant; per-user refresh tokens stored Fernet-encrypted; a
  signed session cookie; an email allowlist gates who can sign in.

See `CLAUDE.md` for the repo map and conventions, [docs/api-reference.md](docs/api-reference.md)
for the HTTP endpoints, and [docs/deploy.md](docs/deploy.md) +
[the owner checklist](docs/goals/goal-8-owner-steps.md) for deployment.

---

## Local dev setup

### 1. Google Tasks lists

Make sure your Google Tasks has two lists titled **exactly** `My Tasks` and `Follow-ups`. A new
account already has `My Tasks`; add `Follow-ups`. Without both, routed tasks are created but never
shown, and captured tasks stay unrouted.

### 2. Google Cloud — OAuth web client (`client_secret.json`)

The app signs users in with Google, so you need a Google Cloud OAuth **Web application** client:

- In the [Google Cloud Console](https://console.cloud.google.com/), pick or create a project.
- **APIs & Services → Enabled APIs:** enable the **Tasks**, **Calendar**, **Drive**, and **Docs**
  APIs.
- **OAuth consent screen:** User type **External**. Add exactly these scopes — `openid`,
  `userinfo.email`, `userinfo.profile`, `tasks`, `calendar.readonly`, `drive.file`. **Do not** add
  `documents` or full `drive` — the app is `drive.file`-scoped by design (it can only touch files
  it created; ADR: [drive-access-scoping](docs/goals/architecture/drive-access-scoping.md)). For
  local dev you can leave publishing status at *Testing* and add yourself as a test user.
- **Credentials → Create credentials → OAuth client ID → Web application.** Add the redirect URI
  `http://localhost:8010/auth/callback`. Download the JSON and save it as **`client_secret.json`**
  at the **repo root** — it must have a top-level `"web"` key (not `"installed"`).

> ⚠️ Once users start filing notes, **don't replace this OAuth client id.** Under `drive.file`,
> Google keys per-file access to the client id that *created* a file, so a new client id 404s every
> user's existing notes folder/Doc. Rotating the client *secret* is fine.

Full step-by-step (including cleanup from older single-user setups and a two-user isolation test):
[docs/goals/goal-8-owner-steps-local.md](docs/goals/goal-8-owner-steps-local.md).

### 3. Backend env (`backend/.env`)

Generate the two secrets:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"                                # SESSION_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_ENCRYPTION_KEY
```

Create `backend/.env` (gitignored — never commit it):

```dotenv
SESSION_SECRET=<from above>            # signs the session cookie
TOKEN_ENCRYPTION_KEY=<from above>      # Fernet key encrypting refresh tokens at rest
SUPERUSER_EMAIL=you@gmail.com          # bootstraps the superuser (always allowed; edits the invite list)
ANTHROPIC_API_KEY=<your key>           # powers the scratchpad router — without it, captures fall to the
                                       # review queue and everything else still works
OAUTH_REDIRECT_URI=http://localhost:8010/auth/callback
FRONTEND_ORIGIN=http://localhost:5173
COOKIE_SECURE=0                        # local http dev (the Secure cookie flag breaks on plain http)
```

The old `NOTES_FOLDER_ID` / `NOTES_DOC_ID` / `EXTRA_CALENDAR_IDS` vars are **gone** — the notes
folder + Docs are app-created per user from the Settings tree, and calendars are toggled per user.

### 4. Run it

**The quick way** — `dev.sh` at the repo root runs both in the background (it applies migrations
first, and runs `npm install` if `node_modules/` is missing):

```sh
./dev.sh up                             # backend :8010 + frontend :5173
./dev.sh status                         # UP/DOWN per service
./dev.sh logs                           # tail both logs (or: ./dev.sh logs backend|frontend)
./dev.sh restart                        # down + up
./dev.sh down                           # stop both
```

PIDs and logs live in `.dev/` (gitignored). `down` also stops anything else holding `:8010` or
`:5173`, even if it wasn't started by the script.

**Or run each by hand. Backend:**

```sh
cd backend
uv sync
uv run alembic upgrade head             # run once, and again after any schema change (creates overlay.db)
uv run uvicorn app.main:app --reload --port 8010
```

**Frontend:**

```sh
cd frontend
npm install
npm run dev                             # http://localhost:5173, expects the backend on :8010
```

Open `http://localhost:5173`, click **Sign in with Google**, and grant the scopes. There's no CLI
auth step — the web sign-in stores each user's encrypted token in the DB. `localhost:5173` and
`localhost:8010` are the same *site*, so the `SameSite=Lax` session cookie flows in dev. If a Google
write ever 403s with a scope error, sign out and back in to re-grant.

Then open **Settings** to build your notes hierarchy (and toggle any extra calendars) — the
scratchpad starts filing captures into it.

---

## Deploying

One container (FastAPI serves the built SPA) behind Caddy on a small EC2 host with Cloudflare DNS.
See [docs/deploy.md](docs/deploy.md) and the ordered
[owner checklist](docs/goals/goal-8-owner-steps.md).

```sh
cp .env.prod.example .env.prod   # fill in; place the web client_secret.json at the repo root
docker compose --env-file .env.prod up -d --build
```
