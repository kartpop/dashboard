# Goal 11 — owner steps (non-code actions)

Claude Code wrote the code; **only you** can register the API key, set the env vars, and
review the seed feed list. The news pipeline is **entirely non-Google** — there is no new
Google scope, no re-auth, and no token step here.

## A. Turn News on for an account

News is a **per-user feature flag** toggled in the UI — there is **no env var** for it.

- [ ] **You (the superuser) always have News on** — nothing to do; the **News** entry is
      in the left nav rail after sign-in.
- [ ] To turn it on for an **invited user**: Settings (⚙ bottom-left) → **Allowed emails**
      → invite the email if needed → tick its **News** checkbox. They see the News rail
      entry on their next `/auth/me` (a refresh); a non-enabled user gets no rail entry and
      `/news` 403s them.
- [ ] The flag is stored on the `allowed_email` row (JSON `features` column), so it needs
      the migration in step D — no restart required to change a flag.

## B. (Optional) Guardian Open Platform key — free

The Guardian source is skipped (logged, never a crash) when no key is set; RSS + Hacker
News already cover most of the feed. To add the Guardian:

- [ ] Register a free developer key at <https://open-platform.theguardian.com/access/>
      (the "developer" tier is free and non-commercial — fine for a personal dashboard).
- [ ] In `backend/.env` add `GUARDIAN_API_KEY=<your key>` and restart the backend.
- [ ] The daily run then pulls the Guardian **science** + **technology** sections
      (standfirst via `show-fields=trailText` — headline + synopsis only, no body).

## C. Review the seed feed list

- [ ] Skim `backend/app/news/config.py` → `DEFAULT_FEEDS`. This hand-curated name→URL map
      **is** the authenticity guarantee (you chose the sources). Add/remove feeds in code
      and restart. (A per-user chip editor over this catalog is goal 13.)
- [ ] A dead/renamed feed URL is logged and skipped, never fatal — but a feed that 404s
      every run contributes nothing, so prune it.

## D. First run + verify

- [ ] Apply the migration once: `cd backend && uv run alembic upgrade head` (creates
      `news_item`, `news_feedback`, `news_profile`).
- [ ] Open **News** and click **Fetch now** (the manual trigger; the scheduler otherwise
      runs it each IST morning). A short, day-grouped card list should appear.
- [ ] Confirm each **headline links out to the source in a new tab**, ~3 cards wear a
      **✨ serendipity** badge, and 👍/👎 + the 💬 comment persist across a reload.
- [ ] Open the **Profile** drawer, edit the markdown, Save — the next Fetch now reflects
      it. The weekly job rewrites this doc from your feedback (one previous version kept →
      **Revert**).

## E. (Optional) Scheduler tuning

Defaults are sensible; override in `backend/.env` if needed:

- `NEWS_SCHEDULER_ENABLED=0` — disable the in-process daily/weekly loop (use Fetch now).
- `NEWS_DAILY_HOUR_IST` (default `7`) — earliest IST hour the daily run fires.
- `NEWS_MODEL` (default `claude-haiku-4-5`) — the curator/rewriter model.
- `NEWS_CURATION_PICKS` / `NEWS_SERENDIPITY_SLOTS` — feed size knobs.
- `ANTHROPIC_API_KEY` — the **same** key the router already uses; no new secret. Without
  it the curator degrades to a code-only recency feed (logged), never a crash.

---

Notes:
- No Google step, no re-auth, no scope change — the news path never touches a Google API.
- The only cost is a few pennies of LLM tokens per day (headlines + capped synopses only;
  article bodies are never fetched or sent).
- Never commit `backend/.env` (already gitignored).
