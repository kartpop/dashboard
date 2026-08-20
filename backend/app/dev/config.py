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

# ── Live-GitHub dedup (goal 12b): the matcher + comment drafter ────────────────

# The issue matcher — per repo with unmatched drafts, wide-but-cheap input
# (candidate titles, not bodies). Sonnet by default: this is per-pair judgment, not
# cross-conversation synthesis, and the candidate lists are large.
DEV_MATCH_MODEL = os.environ.get("DEV_MATCH_MODEL", "claude-sonnet-5")
# Output budget for the matcher. It streams (the g12 `5c6b48e` truncation lesson), so
# this is only a ceiling; a truncated call is treated as a failure and retried next
# scan rather than half-parsed.
DEV_MATCH_MAX_TOKENS = int(os.environ.get("DEV_MATCH_MAX_TOKENS", "16000"))
# Drafts per matcher call. A repo's unmatched drafts are chunked code-side (candidates
# repeated per chunk, matches merged) so no single call's OUTPUT can outgrow the budget
# — the first prod backlog run (78 drafts in one call) truncated at 16k and matched
# nothing. ~20 drafts of matches is a few thousand tokens: comfortable headroom.
DEV_MATCH_DRAFT_CHUNK = int(os.environ.get("DEV_MATCH_DRAFT_CHUNK", "20"))

# The comment drafter reuses DEV_MODEL (this text faces humans on GitHub) with its own
# output budget — one call per converted draft, narrow-but-deep input (one issue's
# whole thread).
DEV_COMMENT_MAX_TOKENS = int(os.environ.get("DEV_COMMENT_MAX_TOKENS", "8000"))

# Candidate-fetch caps — these (not a token knob) govern the matcher's input size.
# Open issues per repo, most-recently-updated first. Open state, deliberately NOT a
# recency window: a six-month-old open issue is exactly the duplicate that matters.
DEV_ISSUE_FETCH_CAP = int(os.environ.get("DEV_ISSUE_FETCH_CAP", "200"))
# Open + merged PRs per repo (closed-unmerged are skipped as abandoned).
DEV_PR_FETCH_CAP = int(os.environ.get("DEV_PR_FETCH_CAP", "100"))

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
