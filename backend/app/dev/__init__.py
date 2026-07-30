"""The Dev view (goal 12): meeting-notes Docs → synthesised GitHub issue drafts.

A cron (+ manual Create now) reads the not-yet-processed entries across a user's
configured notes subtree (per-doc DB cursor, no in-doc marker), a synthesising LLM
(opus) reads the whole day's new entries at once and proposes a **de-duplicated** set
of issue drafts, and the owner reviews/edits/approves cards. On approve, deterministic
code files the issue via the GitHub REST API and attaches it to the repo's ProjectsV2
project via GraphQL (fine-grained PAT, Fernet-encrypted).

This is the app's **first non-Google write surface** (GitHub) and its **first Docs
read path** (`documents.get` on app-created Docs — no scope change, `drive.file`
already grants read on files the app created). Same LLM-proposes / code-disposes ethos
as the router and news: the LLM never touches GitHub; only a human approve does,
through deterministic code. See `.claude/rules/dev.md`.
"""
