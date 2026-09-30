# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A personal dashboard that surfaces Google Tasks, Calendar, and Drive alongside a small
task-metadata overlay.

## Stack

- Backend: FastAPI (Python)
- Frontend: React
- Storage: SQLite locally, Postgres in production, for the task-metadata overlay only

## Repo map

```
CLAUDE.md
.claude/
├── rules/            # path-scoped conventions (backend.md, frontend.md)
└── skills/           # empty for now
.mcp.json             # Google Workspace MCP registration
docs/goals/           # goal specs, one per milestone (+ architecture/ decision records)
backend/              # FastAPI app
frontend/             # React app
```

## Before starting work

Read `docs/goals/<current-goal>.md` before starting work. It defines the objective, scope, and
acceptance criteria for the active milestone — do not work outside that scope.

## Run / test

- Backend: `cd backend && uv run python -m app.google.auth` once to authorize, then
  `uv run alembic upgrade head` once per schema change, then
  `uv run uvicorn app.main:app --reload --port 8010`.
- Frontend: `cd frontend && npm install && npm run dev` (serves on `http://localhost:5173`).
- Or both at once: `./dev.sh up|down|restart|status|logs` from the repo root (migrates first;
  pids + logs in `.dev/`).

## Hard constraints

- Never commit OAuth tokens, `CLAUDE.local.md`, or `.claude/settings.local.json`.
- This is a public repo. When the owner gives examples in chat that name people, organizations, partners, or projects, never carry those names into goal docs, code, tests, fixtures, sample data, or any other committed file. Always use generic examples (e.g. "a partner NGO", "teammate A").
- Dashboard read paths call the Google API client directly. Do not use MCP or an LLM to read tasks, calendar, or drive.
- Google Drive/Docs OAuth scope is `drive.file` only — never `documents` or `drive`. Doc/folder IDs come from config, never from LLM output. (ADR: `docs/goals/architecture/drive-access-scoping.md`.)
