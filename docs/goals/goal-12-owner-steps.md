# Goal 12 — owner steps (non-code actions)

Claude Code wrote the code; **only you** can flip the org PAT policy, mint the
fine-grained token with the right permissions, set up a scratch test repo + project, and
pick your source Docs + repos in the UI. There is **no new Google scope and no re-auth** —
the Docs read the scan does is inside the `drive.file` grant the app already has (it reads
only Docs the app created).

## A. Turn Dev on for an account

Dev is a **per-user feature flag** toggled in the UI — there is **no env var** for it (the
goal-11 mechanism, verbatim).

- [ ] **You (the superuser) always have Dev on** — nothing to do; the **Dev** entry (🛠) is
      in the left nav rail after sign-in.
- [ ] To turn it on for an **invited user**: Settings (⚙ bottom-left) → **Allowed emails**
      → invite the email if needed → tick its **Dev** checkbox. They see the Dev rail entry
      on their next refresh; a non-enabled user gets no rail entry, `/dev/*` 403s them, and
      **the scheduled scan skips them entirely** (no Doc read, no LLM cost).

## B. Allow fine-grained PATs on the org (one-time org policy)

A fine-grained PAT scoped to org repos requires the org to permit them:

- [ ] GitHub → your org → **Settings → Third-party Access → Personal access tokens →
      Settings** → set **Fine-grained personal access tokens** to *Allow access* (and
      decide whether tokens require admin approval).
- [ ] If approval is required, you'll approve your own token in **Pending requests** after
      step C.

## C. Mint the fine-grained PAT(s) — one per resource owner (exact permissions)

A fine-grained PAT is bound to a **single resource owner** (a personal account *or* one
org). To file into more than one owner — e.g. your personal `your-username/*` **and**
`your-org/*` — mint **one token per owner** and add each in the Dev config; the app picks
the right token by the target repo's owner. Repeat this section for each owner:

- [ ] GitHub → your profile → **Settings → Developer settings → Personal access tokens →
      Fine-grained tokens → Generate new token**.
- [ ] **Resource owner:** the account/org for this token (your username for personal
      repos, or the org). **Repository access:** *Only select repositories* → pick the
      repos you want issues filed into (this exact set is what the Dev config's "Refresh
      from GitHub" lists for that owner — no hand-typing `org/repo`).
- [ ] **Permissions:**
  - Repository → **Issues: Read and write** (create issues)
  - Repository → **Metadata: Read-only** (required; auto-selected)
  - Organization → **Projects: Read and write** (attach issues to a ProjectsV2 board) —
    for a **personal-account** token this is *Account → Projects: Read and write*.
- [ ] Generate, copy the token (starts `github_pat_…`), and if org approval is on, approve
      it (step B). The token is shown once — paste it into the Dev config next. The
      resource owner it covers is **inferred from the repos it can see**, so a token that
      can reach no repos is rejected.

## D. (Recommended) A scratch test repo + project

So you can verify filing without polluting a real backlog:

- [ ] Create a throwaway repo, e.g. `zz-dev-verifier`, under the org, and grant the PAT
      access to it (step C's repository list).
- [ ] Create a ProjectsV2 board in the org and **link it to that repo** (repo → Projects →
      link/create) so it shows up under the repo's projects.
- [ ] After a test file, **close the created issue** to clean up (the app never deletes on
      GitHub — filing is one-way by design).

## E. Configure Dev in the app + first run

- [ ] Apply the migrations once: `cd backend && uv run alembic upgrade head` (creates
      `dev_config`, `dev_doc_cursor`, `dev_issue_draft`, and `dev_pat` — the per-owner
      token table; any existing single token is migrated into it automatically).
- [ ] Open **Dev → Config**:
  1. **GitHub tokens** — paste each PAT and **Add** (one per resource owner from step C).
     Each is validated (a viewer ping), stored encrypted, and never shown again — the
     list shows only the owner it covers + a masked hint. Add both your personal token
     and any org token(s); **Remove** drops one (local only, nothing is revoked on
     GitHub).
  2. **Repos** — **Refresh from GitHub** (the union of what *all* your tokens can see),
     tick your issue targets across owners, mark one **default**,
     write a **one-line description** each (this feeds the issue router's repo pick), and
     for each repo **Load projects** → pick its default board. Save.
  3. **Source Docs** — tick the meeting-notes Docs or folders (a folder = every Doc under
     it, including ones added later). Save.
- [ ] Hit **Create now**. New entries across your source subtree are read once, synthesised
      in a single opus call, and draft cards appear.
- [ ] On a card: tweak title/body, switch repo/project if needed, **Approve & file** → the
      issue is created in the repo **and** attached to the project (check your board). A
      merged card's **sources line** shows every entry it was synthesised from. **Dismiss**
      is free and files nothing.

## F. (Optional) Scheduler + model tuning

Defaults are sensible; override in `backend/.env` if needed:

- `DEV_SCHEDULER_ENABLED=0` — disable the in-process daily scan (use Create now).
- `DEV_DAILY_HOUR_IST` (default `21`) — earliest IST hour the end-of-day scan fires.
- `DEV_MODEL` (default `claude-opus-4-8`) — the synthesiser model; drop to a cheaper model
  only if dedup quality holds.
- `ANTHROPIC_API_KEY` — the **same** key the router/news already use; no new secret.

---

Notes:
- No Google step, no re-auth, no scope change — the scan reads only app-created Docs, which
  `drive.file` already covers.
- Filing is one-way: the app never edits/closes/syncs an issue after creating it, and never
  deletes on GitHub. Dismiss and edits are local until you Approve & file.
- Never commit `backend/.env` or the PAT (the token lives Fernet-encrypted in the DB).
