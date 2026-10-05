"""Deterministic route/dispose step — code disposes what the LLM proposes (goal 5).

The classifier returns a `RouterClassification`; THIS module decides what happens
and performs (or withholds) the write. The safety contract lives in router.md:

- **LLM-proposes-code-disposes:** no write lives in the classifier; every write is here.
- **Insert-only blast radius:** the only writes reachable from routing are
  `create_task` + `reschedule` (the g4a date path) for tasks — local task-store
  writes since goal 17 — and `append_note` (goal 7, insert-only into the notes Doc)
  for notes. The router NEVER
  calls `delete_task`, the complete/uncomplete status write, `update_content`, or any
  Docs delete/overwrite — it is *not* a sanctioned `delete_task` caller (writes.md).
- **Confidence gate / schema gate / allowed-destination gate:** below threshold, or
  `unknown`/`event`, never auto-writes — it goes to the review queue.
- **Route-once:** routing flips `routing_state` off `UNROUTED`, so a re-run no-ops.

Goal 8: routing is per-user. `route_entry` takes the current `user` + their live
`creds`; every Google call uses those creds, the notes Doc is the user's own
(auto-bootstrapped via `settings_svc`), and every scratch/review row is user-scoped.
"""

from __future__ import annotations

import json
import logging
import re
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from app.auth.models import User
from app.errors import ApiError
from app.router import config
from app.router.classifier import classify
from app.router.models import (
    CONFIRMED,
    DISMISSED,
    IN_REVIEW,
    KEPT_NOTE,
    PENDING,
    RESOLVED,
    ROUTED_TASK,
    ROUTING,
    UNROUTED,
    ReviewItem,
    ScratchEntry,
)
from app.router.schema import RouterClassification, RouterFields
from app.settings import notes_index
from app.settings import service as settings_svc
from app.tasks_store import service as store
from app.tasks_store.deps import ensure_ready
from app.writes import service as writes_svc

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials


_log = logging.getLogger("router.service")

# The router is opinionated: every routed task is filed into one of exactly these
# two lists — the same lists the dashboard renders (frontend `PINNED_LIST_TITLES`
# in TasksPanel.tsx) — and NEVER into any other Google list the user may have. A
# task filed into an unrendered list is created successfully but never shown.
# DEFAULT_LIST_TITLE is where an unclassified task lands. Keep both in sync with
# the frontend constant and `schema.TargetList`.
PINNED_LIST_TITLES = ("My Tasks", "Follow-ups")
DEFAULT_LIST_TITLE = "My Tasks"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# A routing claim older than this is presumed dead (the process died mid-route) and is
# reclaimed by the scheduler backstop. Must comfortably exceed a real inline route
# (classifier + Docs append ≈ 25s observed) so a slow route is never stolen from itself.
_STALE_CLAIM_SECONDS = 300


def _claim_for_routing(session: Session, entry: ScratchEntry) -> bool:
    """Atomically claim `entry` for routing: compare-and-swap UNROUTED → ROUTING.

    Route-once (goal 5) was a check-then-act: `route_entry` read `routing_state ==
    UNROUTED` and only wrote the final state after the classifier call and the Docs
    append. Goal-7c moved routing inline and goal-10 brought long meeting-notes pastes,
    stretching that window to ~25 seconds — wide enough for a concurrent router to read
    the same UNROUTED row and append the SAME note to the Doc a second time (observed:
    one capture, two Doc entries). The readers that can collide are the scheduler
    backstop, a "Route now" click, and a retried POST.

    A conditional UPDATE is the fix: the WHERE clause re-checks UNROUTED inside the
    database's own atomic write, so exactly one caller can flip it. Losers get
    rowcount 0 and no-op. `routed_at` doubles as the claim timestamp (overwritten with
    the completion time on success) so a dead claim is detectable without a new column.
    """
    result = session.execute(
        sa_update(ScratchEntry)
        .where(ScratchEntry.id == entry.id)
        .where(ScratchEntry.routing_state == UNROUTED)
        .values(routing_state=ROUTING, routed_at=_now())
    )
    session.commit()
    session.refresh(entry)
    return result.rowcount == 1


def _release_claim(session: Session, entry: ScratchEntry) -> None:
    """Hand a claimed entry back to UNROUTED after a failed route (re-routable).

    Preserves the goal-5 contract that a Google-write failure leaves nothing
    half-written and the backstop can retry. The rollback discards the partial work
    first; the release is then committed on its own."""
    session.rollback()
    entry.routing_state = UNROUTED
    session.add(entry)
    session.commit()


# ── The routing header (goal 10) ──────────────────────────────────────────────
#
# The first few words of a capture — everything before the first "-"/newline —
# are ORDER-INSENSITIVE routing words the user steers with. The LLM interprets the
# open-vocabulary parts (free word order, multi-word doc paths, named weekdays);
# code deterministically enforces the CLOSED-vocabulary tokens here so an explicit
# keyword header never bounces to review (the observed bug) and an unambiguous
# relative date backstops a null LLM `due_date`. router.md carries the contract.

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")
_HEADER_MAX_WORDS = 8
_DELIM_RE = re.compile(r"[-\n]")
_TASK_KEYWORDS = {"task", "todo"}
_NOTE_KEYWORDS = {"note", "notes"}
# Leading closed-vocabulary tokens dropped when degrading a header to a bare body
# (the no-delimiter fallback). Doc-path words are open-vocabulary — left to the LLM.
_ROUTING_TOKENS = (
    _TASK_KEYWORDS | _NOTE_KEYWORDS | {"today", "tomorrow", "day", "after"}
)


def _today_ist() -> datetime:
    """Today's date (IST), as a datetime at midnight — the relative-date base."""
    now = datetime.now(_IST)
    return datetime(now.year, now.month, now.day)


def _resolve_header_date(header_low: str) -> str | None:
    """Resolve an UNAMBIGUOUS relative-date word in the header to YYYY-MM-DD (IST).

    Only `today` / `tomorrow` / `day after (tomorrow)` — the closed-vocabulary tokens
    from the observed bug. Named weekdays stay LLM-resolved (eval-graded). "day after"
    is checked before "tomorrow" since it contains it."""
    base = _today_ist()
    if "day after" in header_low:
        return (base + timedelta(days=2)).date().isoformat()
    if "tomorrow" in header_low:
        return (base + timedelta(days=1)).date().isoformat()
    if "today" in header_low:
        return base.date().isoformat()
    return None


@dataclass
class _Header:
    """A parsed routing header. `forced_dest` is set only by an unambiguous single
    destination keyword; `date_backstop` only by an unambiguous relative-date word;
    `body` is the capture minus the header segment."""

    forced_dest: str | None = None
    date_backstop: str | None = None
    body: str | None = None

    @property
    def determinate(self) -> bool:
        """Whether the leading segment is PROVABLY routing words rather than content.

        `body` is always populated (the segment before the first "-"/newline), but that
        segment is only a header when a closed-vocabulary token proves it — a `task`/
        `note` keyword or an unambiguous date word. Without that proof the first line is
        just the note's first line, and dropping it would silently eat the user's words:
        "line 0\\nline 1..." would file as "line 1...". So `body` is only trustworthy as
        a body when this is True; otherwise callers must fall back to the raw capture."""
        return self.forced_dest is not None or self.date_backstop is not None


def _parse_header(raw: str) -> _Header:
    """Parse the routing-header segment (before the first "-"/newline), capped at
    ~8 words; longer or absent → no header (body inference as today).

    Deterministic and closed-vocabulary only: a lone `task`/`note` keyword forces the
    destination; a lone unambiguous date word backstops the due date. `daily syncup`
    and other doc-path / free words are left entirely to the LLM."""
    match = _DELIM_RE.search(raw)
    if match:
        segment = raw[: match.start()]
        remainder: str | None = raw[match.end() :].strip()
    else:
        segment = raw
        remainder = None

    words = segment.split()
    if not words or len(words) > _HEADER_MAX_WORDS:
        return _Header()

    lowered = [w.lower().strip(".,;:!?#") for w in words]
    has_task = any(w in _TASK_KEYWORDS for w in lowered)
    has_note = any(w in _NOTE_KEYWORDS for w in lowered)
    forced = (
        "task"
        if (has_task and not has_note)
        else "note"
        if (has_note and not has_task)
        else None
    )
    date_backstop = _resolve_header_date(" ".join(lowered))

    if remainder is not None:
        body = remainder
    else:
        # No delimiter: strip leading closed-vocabulary routing tokens, keep the rest.
        rest = list(words)
        while rest and rest[0].lower().strip(".,;:!?#") in _ROUTING_TOKENS:
            rest.pop(0)
        body = " ".join(rest)

    return _Header(forced_dest=forced, date_backstop=date_backstop, body=body or None)


def _guarded_note_body(
    raw: str, note_text: str | None, header: "_Header | None" = None
) -> str:
    """The body to write: the prefix-stripped `note_text`, guarded (goal 9).

    Truncation guard — a mangled extraction must never silently lose words: if
    `note_text` is missing, empty, or suspiciously short (< 50% of the fallback's
    length), fall back to the **user's own words verbatim**. Otherwise the stripped
    text (prefix removed, rest verbatim) wins.

    Goal 10a — the fallback is header-aware. It used to be the whole `raw` capture,
    which re-introduced the routing header ("note t4d test") as the note's first line.
    That was invisible while the fallback was a rare mangled-extraction rescue, but
    it is now a NORMAL path: past `config.CLASSIFY_MAX_CHARS` the classifier only sees
    the head of the capture and its `note_text` is discarded outright (an echo of an
    excerpt is a body missing its tail), so every long note lands here. `header.body` is the
    same capture with the routing segment deterministically removed, which is exactly
    what the echo was producing — so code does the strip the LLM no longer does, and
    the words still come from the user, never from the model."""
    fallback = (
        header.body if header and header.determinate and header.body else None
    ) or raw
    candidate = note_text or ""
    if not candidate.strip() or len(candidate.strip()) < 0.5 * len(fallback.strip()):
        return fallback
    return candidate


async def _dispose_note(
    session: Session,
    creds: "Credentials",
    user_id: int,
    entry: ScratchEntry,
    fields: RouterFields,
    body_override: str | None = None,
    header: "_Header | None" = None,
    degraded: bool = False,
) -> str:
    """Dispose a `note`: route it to the best-matching hierarchy Doc (goal 9).

    The body is the prefix-stripped `note_text` under the truncation guard
    (`_guarded_note_body`); `body_override` (a review edit) wins verbatim. The Doc is
    resolved deterministically from `fields.target_doc_path` (path → stored id;
    unknown/null → default Doc), self-healing a 404'd hierarchy Doc at the same path.
    `entry.routed_doc_path` records where it landed (null = default). `summary` +
    optional `keywords` are the only LLM-authored lines. Returns KEPT_NOTE.

    Goal 10 `degraded`: a note FORCED by a `note`/`notes` header whose fields the LLM
    didn't produce (it proposed a task) degrades safe — body = the raw capture minus
    the header (`header.body`), no summary/keywords; the doc path still comes from the
    LLM (null → default Doc, so `resolve_note_target` handles it as always).

    A Drive/Docs failure raises (entry left re-routable, rollback) so route-once only
    marks the entry routed after a successful append — same contract as the task path.
    """
    if body_override is not None:
        body_text = body_override
    elif degraded:
        body_text = (
            header.body if header and header.determinate and header.body else None
        ) or entry.text
    else:
        body_text = _guarded_note_body(entry.text, fields.note_text, header)

    summary = None if degraded else fields.summary
    keywords = None if degraded else fields.keywords

    doc_id, folder_id, canonical = await settings_svc.resolve_note_target(
        session, creds, user_id, fields.target_doc_path
    )
    try:
        await writes_svc.append_note(
            creds,
            doc_id,
            folder_id,
            body_text,
            summary=summary,
            keywords=keywords,
        )
    except ApiError:
        session.rollback()
        raise
    entry.routed_doc_path = canonical
    entry.routed_doc_id = doc_id
    return KEPT_NOTE


def _resolve_list_id(session: Session, user_id: int, target_list: str | None) -> str:
    """Resolve the classifier's `target_list` to a real task-list id.

    Opinionated: routing files ONLY into the two pinned lists (`PINNED_LIST_TITLES`)
    the dashboard renders — never into any other list. Matches the requested
    list by title (case-insensitive); an unset/unknown target defaults to "My Tasks".
    If the requested pinned list is missing but the other exists, falls back to the
    other pinned list (never a third list). Raises if NEITHER pinned list exists
    (the two-list prerequisite is unmet) — the caller then leaves the entry
    re-routable.
    """
    raw_lists = store.get_tasklist_refs(session, user_id)
    # title(lower) → id, restricted to the two pinned lists we are willing to write.
    pinned = {t.lower(): None for t in PINNED_LIST_TITLES}
    for tl in raw_lists:
        title = (tl.get("title") or "").strip().lower()
        if title in pinned and pinned[title] is None:
            pinned[title] = tl["id"]

    requested = (target_list or DEFAULT_LIST_TITLE).strip().lstrip("#").lower()
    if requested not in pinned:
        requested = DEFAULT_LIST_TITLE.lower()

    # Prefer the requested list, then the primary default, then the other pinned list.
    for title in (requested, DEFAULT_LIST_TITLE.lower(), *pinned):
        if pinned.get(title) is not None:
            return pinned[title]

    raise ApiError(
        502,
        "no_pinned_lists",
        "This account has neither 'My Tasks' nor 'Follow-ups' — the dashboard "
        "requires them.",
    )


async def _create_task_from_fields(
    session: Session,
    creds: "Credentials",
    user_id: int,
    fields: RouterFields,
    header: "_Header | None" = None,
) -> dict:
    """Create a task from extracted fields, applying list-hint + due date.

    Two sanctioned writes only: `create_task` (always) and `reschedule` (only when a
    due date was extracted — the g4a date path). Nothing destructive is reachable.

    Goal 10 header backstops: an unambiguous relative-date word in the routing header
    (`header.date_backstop`) resolves a null LLM `due_date`; when the LLM proposed a
    non-task (but a `task` header forced this path) and produced no title, the header's
    residual body is used so a forced task is never rejected for an empty title.
    """
    title = (fields.title or "").strip()
    if not title and header is not None and header.body:
        title = header.body.strip().splitlines()[0].strip()
    if not title:
        raise ApiError(422, "empty_title", "Router produced no task title.")
    # Goal 17: a user not yet imported is imported first (503 → entry re-routable).
    user = session.get(User, user_id)
    if user is not None:
        await ensure_ready(session, user, creds)
    list_id = _resolve_list_id(session, user_id, fields.target_list)

    # 1) create (lands undated in NO_DATE) — the router's primary write. Notes are
    #    intentionally dropped: writing them would need `update_content`, which is NOT
    #    a sanctioned router write (create-only). A reviewer can add notes by hand.
    created = await writes_svc.create_task(session, user_id, list_id, title, rank=None)

    # 2) set the due date via the g4a reschedule path (metadata write, non-destructive).
    #    The header's unambiguous relative date backstops a null LLM due_date.
    due_date = fields.due_date or (header.date_backstop if header else None)
    if due_date:
        await writes_svc.reschedule(
            session,
            user_id,
            tasklist_id=list_id,
            task_id=created["id"],
            due_date=due_date,
            rank=created.get("rank"),
            group_id=None,
        )
    return {"list_id": list_id, "task_id": created["id"]}


def _new_review_item(
    entry: ScratchEntry,
    user_id: int,
    destination: str,
    fields: RouterFields,
    confidence: float,
    reason: str,
    header: "_Header | None" = None,
) -> ReviewItem:
    """Build a pending review item with the note body **guarded at creation** (goal 10).

    The truncation guard (`_guarded_note_body`) is applied to `note_text` before the
    fields are frozen into `fields_json`, so every consumer — the editor prefill and
    the confirm fallback — sees the raw capture verbatim when the LLM's extraction was
    missing/short, never the mangled low-confidence extraction it just declined to
    auto-file. Single source of truth server-side; the frontend keeps its `?? entry_text`."""
    guarded = fields.model_copy(
        update={"note_text": _guarded_note_body(entry.text, fields.note_text, header)}
    )
    return ReviewItem(
        user_id=user_id,
        entry_id=entry.id,  # type: ignore[arg-type]
        destination=destination,
        fields_json=guarded.model_dump_json(),
        confidence=confidence,
        reason=reason,
        status=PENDING,
    )


async def classify_text(
    session: Session, user_id: int, text: str
) -> RouterClassification:
    """Classify a capture WITHOUT disposing it — a pure, side-effect-free LLM call.

    Split out from `route_entry` so the capture UI can run the (slow) classifier
    during its undo window (the deferred-write toast) and then commit the already-
    computed classification when the window lapses — the LLM latency hides behind the
    toast instead of adding to it. No DB write, no Google write; safe to discard on
    undo. The user's notes hierarchy is injected so the LLM can propose a doc path.
    """
    doc_paths = notes_index.leaf_paths(settings_svc.get_notes_index(session, user_id))
    return await classify(text, doc_paths)


async def route_entry(
    session: Session,
    user: "User",
    creds: "Credentials",
    entry: ScratchEntry,
    classification: RouterClassification | None = None,
) -> str:
    """Classify + dispose one entry for `user`. Returns the resulting routing_state.

    Idempotent: if the entry is already routed it is left untouched. A Google-write
    failure in the deterministic step leaves the entry `UNROUTED` (re-routable) and
    raises an `ApiError` — nothing is half-written, the error is never swallowed.

    `classification` lets a caller inject a pre-computed classification (from
    `classify_text`, run during the capture undo window) so the LLM call is not
    repeated inline; when omitted the classifier runs here as before. Dispose is
    deterministic either way — the confidence/schema/destination gates still apply,
    and a note's Doc still comes from path→id resolution, never from the payload.
    """
    if entry.routing_state != UNROUTED:
        return entry.routing_state
    # Claim it atomically BEFORE the slow work (goal 10a) — the check above is only a
    # cheap early-out; the CAS is what actually makes route-once safe against a
    # concurrent router appending the same note twice.
    if not _claim_for_routing(session, entry):
        return entry.routing_state

    user_id = user.id
    if classification is None:
        classification = await classify_text(session, user_id, entry.text)
    entry.route_result = classification.model_dump_json()
    dest = classification.destination
    conf = classification.confidence
    fields = classification.fields

    # Goal 10 routing header: a destination keyword FORCES the destination and bypasses
    # the confidence gate FOR DESTINATION ONLY — an explicit `task`/`note` is user
    # intent, not a probability, and must never bounce to review. Absent/ambiguous
    # header → the LLM's destination + the gate, as before (goal-9 body inference).
    header = _parse_header(entry.text)
    eff_dest = header.forced_dest or dest
    forced = header.forced_dest is not None
    act = forced or conf >= config.CONFIDENCE_THRESHOLD

    if eff_dest == "task" and act:
        try:
            await _create_task_from_fields(session, creds, user_id, fields, header)
        except ApiError:
            # Re-routable: release the claim, persist no routed state, surface the error.
            _release_claim(session, entry)
            raise
        entry.routing_state = ROUTED_TASK
    elif eff_dest == "note" and act:
        # Degrade only when the header forced a note the LLM didn't extract as one.
        try:
            entry.routing_state = await _dispose_note(
                session,
                creds,
                user_id,
                entry,
                fields,
                header=header,
                degraded=forced and dest != "note",
            )
        except ApiError:
            _release_claim(session, entry)
            raise
    else:
        if eff_dest in ("task", "note"):
            reason = f"low confidence ({conf:.2f}) for {eff_dest}"
        elif eff_dest == "event":
            reason = "events need a manual calendar add (read-only v1)"
        else:
            reason = "unclassifiable"
        session.add(
            _new_review_item(entry, user_id, eff_dest, fields, conf, reason, header)
        )
        entry.routing_state = IN_REVIEW

    entry.routed_at = _now()
    session.add(entry)
    session.commit()
    return entry.routing_state


async def route_unrouted(session: Session, user: "User", creds: "Credentials") -> dict:
    """Route every `UNROUTED` entry for `user` exactly once. Per-entry write failures
    are tallied and skipped (entry left re-routable) so one bad entry can't stall the
    batch. Returns a summary tally.

    Entries mid-flight in another router are `ROUTING` and are simply not selected —
    that is what stops this backstop from double-appending a note that inline routing
    is still working on (goal 10a). A claim older than `_STALE_CLAIM_SECONDS` means the
    router holding it died, so it is reclaimed first: the crash-recovery guarantee is
    the whole point of the backstop, and a claim must never become a permanent grave.
    """
    session.execute(
        sa_update(ScratchEntry)
        .where(ScratchEntry.user_id == user.id)
        .where(ScratchEntry.routing_state == ROUTING)
        .where(
            ScratchEntry.routed_at < _now() - timedelta(seconds=_STALE_CLAIM_SECONDS)
        )
        .values(routing_state=UNROUTED)
    )
    session.commit()

    entries = session.exec(
        select(ScratchEntry)
        .where(ScratchEntry.user_id == user.id)
        .where(ScratchEntry.routing_state == UNROUTED)
    ).all()
    tally = {"routed_task": 0, "kept_note": 0, "in_review": 0, "failed": 0}
    for entry in entries:
        try:
            state = await route_entry(session, user, creds, entry)
            tally[state] = tally.get(state, 0) + 1
        except ApiError:
            tally["failed"] += 1
    return tally


# ── Review-queue dispositions ─────────────────────────────────────────────────


def _get_scoped_review(session: Session, user_id: int, item_id: int) -> ReviewItem:
    """Fetch a PENDING review item that belongs to `user_id` (404 otherwise —
    no cross-tenant read by id)."""
    item = session.get(ReviewItem, item_id)
    if item is None or item.user_id != user_id or item.status != PENDING:
        raise ApiError(404, "review_not_found", "No pending review item with that id.")
    return item


async def confirm_review(
    session: Session,
    user: "User",
    creds: "Credentials",
    item_id: int,
    destination: str | None = None,
    fields: RouterFields | None = None,
) -> dict:
    """Confirm a pending review item (optionally edit-then-confirm).

    A `task` confirmation fires exactly one `create_task` (+ date). `note` keeps it
    in the user's Doc. `event`/`unknown` are acknowledged with NO write (manual-add).
    """
    item = _get_scoped_review(session, user.id, item_id)
    entry = session.get(ScratchEntry, item.entry_id)
    if entry is None:
        raise ApiError(404, "entry_not_found", "Source entry missing.")

    dest = destination or item.destination
    eff_fields = fields or RouterFields(**json.loads(item.fields_json or "{}"))
    # Same helpers as auto-route, so a confirmed item gets the same guarantees — the
    # header's relative-date backstop applies to a confirmed task too (goal 10).
    header = _parse_header(entry.text)

    if dest == "task":
        try:
            await _create_task_from_fields(session, creds, user.id, eff_fields, header)
        except ApiError:
            session.rollback()
            raise
        entry.routing_state = ROUTED_TASK
    elif dest == "note":
        # The review dropdown only offers real leaves, so a non-null path that
        # doesn't validate means a stale tree → 422 (goal 9, item 7). Null = default.
        if eff_fields.target_doc_path:
            forest = settings_svc.get_notes_index(session, user.id)
            if notes_index.resolve_path(forest, eff_fields.target_doc_path) is None:
                raise ApiError(
                    422, "unknown_doc_path", "That notes Doc no longer exists."
                )
        # Review edits win: a user-edited note body / one-liner is what lands. An
        # empty note_text falls back to the verbatim entry text (bypasses the guard).
        edited = (eff_fields.note_text or "").strip()
        entry.routing_state = await _dispose_note(
            session,
            creds,
            user.id,
            entry,
            eff_fields,
            body_override=edited or entry.text,
        )
    else:
        # event / unknown: acknowledged, no write (calendar read-only v1).
        entry.routing_state = RESOLVED

    item.status = CONFIRMED
    item.destination = dest
    item.fields_json = eff_fields.model_dump_json()
    entry.routed_at = _now()
    session.add(item)
    session.add(entry)
    session.commit()
    return {
        "item_id": item.id,
        "status": item.status,
        "entry_state": entry.routing_state,
    }


async def dismiss_review(session: Session, user: "User", item_id: int) -> dict:
    """Dismiss a pending review item — writes nothing; the entry is resolved."""
    item = _get_scoped_review(session, user.id, item_id)
    entry = session.get(ScratchEntry, item.entry_id)
    item.status = DISMISSED
    if entry is not None:
        entry.routing_state = RESOLVED
        entry.routed_at = _now()
        session.add(entry)
    session.add(item)
    session.commit()
    return {"item_id": item.id, "status": item.status}
