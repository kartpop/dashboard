"""Dev-view configuration (goal 12).

Everything tunable about the meeting-notes → issue-draft pipeline. The synthesising
LLM defaults to **opus** — this step is not classification: it must recognise that a
login bug raised in standup and "auth failing" written up after a 1:1 are the *same*
issue and merge them. Cross-conversation synthesis is genuine reasoning and the volume
is ~once a day, so the stronger model is worth it. The model id stays env-configurable
so it can drop to a cheaper model if it proves good enough in practice.
"""

from __future__ import annotations

import os

# The synthesising LLM. Opus by default (see module docstring); env-overridable.
DEV_MODEL = os.environ.get("DEV_MODEL", "claude-opus-4-8")

# Output-budget cap for the synthesiser. A day's drafts are a structured payload whose
# body_markdown fields add up fast — dense JSON tokenises at ~2.5 chars/token, so the old
# 8192 truncated on a busy day (the cut-off JSON fails to parse and the whole scan yields
# no drafts). `synth.py` streams, so this is not bounded by the ~16k non-streaming HTTP
# timeout; 32000 comfortably covers normal scans. A first run with a long backlog (many
# days of notes at once) can emit far more — set DEV_MAX_TOKENS=64000 for that scan. It's
# only a ceiling (you pay for tokens actually generated), so a high value is harmless.
DEV_MAX_TOKENS = int(os.environ.get("DEV_MAX_TOKENS", "32000"))

# How many still-open drafts / recently-filed issue titles to feed the model as
# do-not-redraft context (cross-scan dedup). Newest-first truncation.
DO_NOT_REDRAFT_LIMIT = int(os.environ.get("DEV_DO_NOT_REDRAFT_LIMIT", "60"))

# GitHub API endpoints. Overridable for a self-hosted GitHub Enterprise host.
GITHUB_API_BASE = os.environ.get("GITHUB_API_BASE", "https://api.github.com")
GITHUB_GRAPHQL_URL = os.environ.get(
    "GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"
)
# Per-call GitHub HTTP timeout (seconds).
GITHUB_TIMEOUT = float(os.environ.get("DEV_GITHUB_TIMEOUT", "20"))

# ── Scheduler cadence (in-process asyncio loop, same pattern as news/router) ───
# Disable with DEV_SCHEDULER_ENABLED=0. The loop wakes on this interval and runs the
# daily scan when a user's bookmark says they are due AND the dev flag is on (cost
# gating — an unflagged user is never read from Docs, never sent to the model).
SCHEDULER_ENABLED = os.environ.get("DEV_SCHEDULER_ENABLED", "1") not in (
    "0",
    "false",
    "",
)
SCHEDULER_INTERVAL = float(os.environ.get("DEV_SCHEDULER_INTERVAL", "1800"))
# End-of-day daily: the scan fires at/after this IST hour AND ≥ DAILY_MIN_HOURS since
# the last run. Whole-day synthesis wants the day's mentions together, so one late run
# beats fragmenting the day across cron windows. First run (no bookmark) fires once the
# hour condition holds. Manual "Create now" bypasses the cadence entirely.
DAILY_HOUR_IST = int(os.environ.get("DEV_DAILY_HOUR_IST", "21"))
DAILY_MIN_HOURS = float(os.environ.get("DEV_DAILY_MIN_HOURS", "20"))
