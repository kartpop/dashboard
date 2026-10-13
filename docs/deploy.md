# Deploy (goal 8)

The app runs as **one container** — FastAPI serves both the API and the built Vite
SPA — behind **Caddy** (automatic Let's Encrypt TLS) on a **single small EC2** host,
with **Cloudflare DNS** (DNS-only) pointing at it. SQLite persists on a Docker volume
in WAL mode; a nightly `.backup` cron writes a second on-disk copy. Postgres and
multi-instance are deliberately out of scope (the in-process router scheduler shares
the single-instance assumption).

See `docs/goals/goal-8-owner-steps.md` for the exact, ordered owner checklist
(OAuth client, consent screen, EC2, DNS, secrets). This file is the reference.

## Architecture

```
Browser ──HTTPS──> Caddy (:443, Let's Encrypt) ──> app (:8010)
                                                     ├── FastAPI API (/auth, /tasks, /calendar, /scratch, /settings, ...)
                                                     └── StaticFiles: the built SPA (index.html + assets)
                                    SQLite (WAL) on the `dashboard-data` volume (/data/overlay.db)
```

- **Auth:** Google Sign-In = the OAuth grant. Per-user refresh tokens are stored
  Fernet-encrypted in the DB; a signed `HttpOnly`/`Secure`/`SameSite=Lax` session
  cookie carries identity. An `allowed_email` table (edited by the superuser) gates
  who can sign in; `SUPERUSER_EMAIL` bootstraps the first admin.
- **Tenancy:** every user-owned row has `user_id`; one `current_user` dependency
  scopes every query.

## Durable per-user config — two invariants (goal 8a)

Each user's notes **folder + Doc are app-created once** in their Drive and their ids are
stored in `user_settings`. They stay the same file across every release **only if both of
these hold** — treat them as hard operational invariants:

1. **The `overlay.db` volume persists.** The stored ids live in SQLite on the
   `dashboard-data` volume. `docker compose up -d --build` (a normal release) keeps named
   volumes — but **`docker compose down -v` deletes them**, and a host migration must copy
   the volume. Lose it and every user re-bootstraps a fresh folder/Doc (old ones orphaned).
2. **The OAuth `client_id` never changes.** Under `drive.file`, Google keys per-file access
   to the **client id that created the file** — a *new* client id (or new GCP project) is a
   different app and gets **404** on the existing folder/Doc. Rotating the client **secret**
   is fine; replacing the **client id** is not.

The app **self-heals** the client-id case (goal 8a): `ensure_notes_target` probes a stored
id and, on a definite 404, re-bootstraps a fresh folder/Doc so notes never 404 forever. But
that starts a *new* Doc and orphans the old one — the self-heal is a safety net, **not** a
reason to change the client id. Keep the client id and the volume stable and every user's
notes id is stable forever, across any number of releases.

## Images / compose

- `Dockerfile` — multi-stage: build the SPA (`node`), then a Python image that
  `uv sync --no-dev`s the backend, copies the SPA into `/srv/frontend/dist`, and runs
  `docker-entrypoint.sh` (`alembic upgrade head` → `uvicorn`).
- `docker-compose.yml` — `app` + `caddy`. Secrets via `.env.prod` (see
  `.env.prod.example`). `client_secret.json` is mounted read-only (never baked in).
- `Caddyfile` — `{$DASHBOARD_DOMAIN}` → `reverse_proxy app:8010`.

## Prod bring-up (on the EC2 host)

```sh
git clone <repo> dashboard && cd dashboard
cp .env.prod.example .env.prod   # then fill in — see owner-steps
# place the web-client client_secret.json at the repo root (mounted by compose)
docker compose --env-file .env.prod up -d --build
```

The `--env-file .env.prod` makes `DASHBOARD_DOMAIN` available to compose (Caddy);
`app` reads the rest from `env_file`. First request triggers `alembic upgrade head`.

### Nightly backup cron (host crontab)

```
15 3 * * *  cd /home/ubuntu/dashboard && docker compose exec -T app uv run python scripts/backup.py >> /var/log/dashboard-backup.log 2>&1
```

Writes WAL-safe `.backup` copies under `/data/backups` (on the volume) and prunes to
`BACKUP_KEEP` (default 14). Off-box copies are intentionally not configured.

## Redeploy (subsequent releases, app already running)

To ship new features/fixes to a host that's already running, rebuild the image
**in place** — this keeps the `dashboard-data` volume (and every user's notes ids)
intact per the invariants above. On the EC2 host:

```sh
cd /home/ubuntu/dashboard
git pull
docker compose --env-file .env.prod up -d --build
```

`up -d --build` rebuilds only what changed, recreates the `app` container from the new
image, and leaves `caddy` and the named volumes untouched. There is **no downtime beyond
the container restart** (a few seconds). On startup the entrypoint runs `alembic upgrade
head`, so any new migrations apply automatically — no manual migration step.

Notes on specific changes:

- **Frontend-only changes** still need `--build`: the SPA is baked into the image at build
  time, not mounted.
- **`.env.prod` / secrets changed?** `up -d` recreates the container with the new env; no
  rebuild needed for env-only changes, but running `--build` is harmless.
- **`client_secret.json` rotated?** Only the client **secret** may change — never the
  **client id** (see the invariants above). It's mounted read-only, so replace the file on
  the host and `docker compose --env-file .env.prod up -d` to pick it up.
- **Never** use `docker compose down -v` — the `-v` deletes the data volume and every user
  re-bootstraps a fresh folder/Doc. Plain `down` (no `-v`) is safe but unnecessary; `up -d
  --build` recreates in place.

Roll back by checking out the previous commit/tag and re-running the same `up -d --build`.
Down migrations are not part of the flow — a rollback that spans a schema change needs a
restore from `/data/backups` (see the cron above).

### Verify the release

```sh
docker compose --env-file .env.prod ps           # app + caddy both Up
docker compose --env-file .env.prod logs -f app  # watch alembic + uvicorn start
curl -fsSI https://$DASHBOARD_DOMAIN/            # 200 + serves the SPA; then sign in
```

## Local dev (unchanged flow, web OAuth)

Local dev uses the **same web OAuth client** with a second redirect URI:

- `OAUTH_REDIRECT_URI=http://localhost:8010/auth/callback`
- `FRONTEND_ORIGIN=http://localhost:5173`
- `COOKIE_SECURE=0` (http, so the Secure flag would drop the cookie)
- `SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY`, `SUPERUSER_EMAIL`, `ANTHROPIC_API_KEY` in
  `backend/.env`.

Run backend (`cd backend && uv run alembic upgrade head && uv run uvicorn app.main:app
--reload --port 8010`) and frontend (`cd frontend && npm run dev`). Sign in at
`http://localhost:5173`. `localhost:5173` and `localhost:8010` are the same *site*, so
the `SameSite=Lax` cookie flows.

## Env vars

| Var | Purpose |
| :-- | :-- |
| `DASHBOARD_DOMAIN` | Hostname Caddy certs + serves (compose/Caddy). |
| `OAUTH_REDIRECT_URI` | Must match a redirect URI on the OAuth web client. |
| `FRONTEND_ORIGIN` | Post-sign-in redirect target. |
| `SESSION_SECRET` | Signs the session cookie. |
| `TOKEN_ENCRYPTION_KEY` | Fernet key encrypting refresh tokens at rest. |
| `SUPERUSER_EMAIL` | Bootstraps the superuser + is always allowed. |
| `ANTHROPIC_API_KEY` | One app-level key for the router (owner pays). |
| `COOKIE_SECURE` | `1` in prod (HTTPS), `0` for local http dev. |
| `CLIENT_SECRET_PATH` | Override the OAuth client secret path (defaults to repo root). |
| `DATABASE_URL` | SQLite path (compose sets `sqlite:////data/overlay.db`). |
