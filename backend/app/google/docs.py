"""Thin Google Docs / Drive client for the notes writer (goal 7).

The app's ENTIRE Docs/Drive surface — insert a note, read a doc's parents (the
folder-ancestry gate), and the one sanctioned file-create (the bootstrap). Each
function is one Google call, no orchestration (that lives in `app.writes.service`);
same sync `_fn` / `async def` + `asyncio.to_thread` split as the other clients.

**Scope is `drive.file` only** (ADR: docs/goals/architecture/drive-access-scoping.md).
Google enforces that the token can touch only files the app created, so the sole
create path (`create_doc_in_folder`) hard-codes `parents=[folder_id]` — every doc the
app can ever reach lives under that folder. This module NEVER deletes a file and never
overwrites a doc's contents — the insert is the only mutation of a doc's *body*, and it
is insert-only. The one `files.update` here (`rename_file`, goal 9) is **metadata-only**
(`{"name": ...}` — never content, parents, or trashed) and is called only from the
settings/rename path (never the router — AST-asserted).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

_DOC_MIME = "application/vnd.google-apps.document"
_FOLDER_MIME = "application/vnd.google-apps.folder"

# When the LLM gives no one-liner, the H3 headline still exists (a placeholder) so
# the heading levels stay uniform — H3 = one-liner, H4 = timestamp, H5 = keywords —
# across every note. A future "extract all H4s" search then reliably yields every
# timestamp without the level shifting per-entry (goal 9).
_NO_SUMMARY_PLACEHOLDER = "—"


def _docs_service(creds: "Credentials"):
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def _drive_service(creds: "Credentials"):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── Read helper: the folder-ancestry gate's one Drive call ────────────────────


def _get_parents(creds: "Credentials", file_id: str) -> list[str]:
    service = _drive_service(creds)
    meta = service.files().get(fileId=file_id, fields="parents").execute()
    return meta.get("parents", []) or []


async def get_parents(creds: "Credentials", file_id: str) -> list[str]:
    """Return a file's direct parent folder ids (for the ancestry gate)."""
    return await asyncio.to_thread(_get_parents, creds, file_id)


# ── Accessibility probe: is a stored file still reachable by THIS client? ──────


def _file_accessible(creds: "Credentials", file_id: str) -> bool:
    """True if this OAuth client can still see `file_id` (a plain `files.get` read).

    Under `drive.file`, per-file access is keyed to the OAuth **client id** that
    created the file — so a file created by a *different* client id (or a file the
    user deleted) returns **404** to this client even though the id is well-formed.
    That 404 is the definitive "no longer ours" signal → `False`. Any other error
    (403/5xx/transient) is NOT a not-found signal, so we re-raise and let the caller
    fail closed rather than discard a still-good id. Never a mutation — a read.
    """
    service = _drive_service(creds)
    try:
        service.files().get(fileId=file_id, fields="id").execute()
        return True
    except HttpError as exc:
        if getattr(exc, "resp", None) is not None and exc.resp.status == 404:
            return False
        raise


async def file_accessible(creds: "Credentials", file_id: str) -> bool:
    """Return whether the current client can still reach a stored file (see above)."""
    return await asyncio.to_thread(_file_accessible, creds, file_id)


# ── The sanctioned Docs READ (goal 12: the dev-view entry scan) ───────────────


def _get_document(creds: "Credentials", doc_id: str) -> dict:
    """Read an app-created Doc's full structured content (`documents.get`).

    The app's FIRST Docs read path (goal 12). No scope change: `drive.file` grants
    read *and* write on files the app created, and every Doc the app can reach is
    app-created (the create path hard-codes the notes folder as parent), so reading
    one back is already inside the granted scope. This is a **read** — no `batchUpdate`,
    no mutation — used only to parse entries for the issue-draft synthesiser. The Docs
    *write* surface (insert-only `batchUpdate`) is untouched; the AST test pins this as
    a read (`documents().get`, never a write method inside `_get_document`).
    """
    service = _docs_service(creds)
    return service.documents().get(documentId=doc_id).execute()


async def get_document(creds: "Credentials", doc_id: str) -> dict:
    """Return an app-created Doc's structured content for the dev-view entry scan."""
    return await asyncio.to_thread(_get_document, creds, doc_id)


# ── Insert-only note write (Docs batchUpdate) ─────────────────────────────────


def _keyword_line(keywords: list[str] | None) -> str:
    """Render keywords as one comma-separated line, or "" when there are none."""
    if not keywords:
        return ""
    return ", ".join(k.strip() for k in keywords if k and k.strip())


# ── The structured-body renderer (goal 10) ────────────────────────────────────
#
# A note body is parsed as *light* markdown and rendered with Docs styling instead
# of one flat text run. This is a DETERMINISTIC, code-only transform — no LLM, no
# new Docs method (still one `documents.batchUpdate`, insert-only). Only markdown
# *markers* are consumed as styling; every word of the body survives verbatim.
#
# The one hard invariant (the point of this goal): a markdown heading in the body
# renders as a **bold NORMAL_TEXT** line, NEVER a `HEADING_*` paragraph — so the
# entry's H3/H4/H5 chrome stays the only heading structure and a future
# "extract all H4s" search can never be polluted by a pasted `## Agenda`.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\d+\.\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# Per markdown-heading level, a small paragraph indent conveys depth WITHOUT a
# heading style (depth may be shown by indent; never by `HEADING_*`).
_HEADING_INDENT_PT = 18.0


def _consume_bold(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Strip `**bold**` markers, returning (clean_text, bold_offset_ranges).

    Ranges are (start, end) offsets into the RETURNED clean text. Everything that is
    not a marker passes through verbatim; text with no `**` returns unchanged, [].
    """
    parts: list[str] = []
    ranges: list[tuple[int, int]] = []
    last = 0
    pos = 0
    for m in _BOLD_RE.finditer(text):
        lead = text[last : m.start()]
        parts.append(lead)
        pos += len(lead)
        inner = m.group(1)
        parts.append(inner)
        ranges.append((pos, pos + len(inner)))
        pos += len(inner)
        last = m.end()
    parts.append(text[last:])
    return "".join(parts), ranges


@dataclass
class _BodyPara:
    """One rendered body paragraph. `text` is inserted verbatim (with leading tabs
    for a nested bullet, which `createParagraphBullets` later consumes for its level);
    `bold_ranges` are offsets into `text`; `bullet` is "ul"/"ol"/None; `indent_pt`
    conveys a markdown-heading's depth (paragraph indent, never a heading style)."""

    text: str
    bold_ranges: list[tuple[int, int]] = field(default_factory=list)
    bullet: str | None = None
    indent_pt: float = 0.0


def _parse_markdown(body: str) -> list[_BodyPara]:
    """Parse a body into rendered paragraphs (light markdown, line-based).

    A markdown heading → a whole-line-bold plain paragraph (NEVER a heading style).
    A `- `/`* `/`1. ` line → a bullet paragraph, nesting from its leading indent
    (2 spaces or a tab per level). Any other line passes through verbatim. Inline
    `**bold**` is consumed to bold runs on every kind."""
    paras: list[_BodyPara] = []
    for line in body.split("\n"):
        stripped = line.lstrip(" \t")
        indent = line[: len(line) - len(stripped)]
        depth = indent.count("\t") + indent.count(" ") // 2

        m = _HEADING_RE.match(stripped)
        if m:
            clean, _ = _consume_bold(m.group(2))
            paras.append(
                _BodyPara(
                    clean,
                    [(0, len(clean))] if clean else [],
                    None,
                    (len(m.group(1)) - 1) * _HEADING_INDENT_PT,
                )
            )
            continue

        bullet = None
        content = None
        mu = _UL_RE.match(stripped)
        if mu:
            bullet, content = "ul", mu.group(1)
        else:
            mo = _OL_RE.match(stripped)
            if mo:
                bullet, content = "ol", mo.group(1)

        if bullet is not None:
            clean, ranges = _consume_bold(content)
            tabs = "\t" * depth
            shifted = [(a + depth, b + depth) for a, b in ranges]
            paras.append(_BodyPara(tabs + clean, shifted, bullet, 0.0))
            continue

        # Plain line: kept verbatim (leading indent and all), only inline bold consumed.
        clean, ranges = _consume_bold(line)
        paras.append(_BodyPara(clean, ranges, None, 0.0))
    return paras


def _bold_request(start: int, end: int) -> dict:
    return {
        "updateTextStyle": {
            "range": {"startIndex": start, "endIndex": end},
            "textStyle": {"bold": True},
            "fields": "bold",
        }
    }


def _render_body(body_text: str, start: int) -> tuple[str, list[dict], list[dict], int]:
    """Render a body as light markdown → (inserted_text, style_reqs, bullet_reqs, end).

    `inserted_text` is the body's contribution to the insert block (each paragraph
    followed by "\\n"), using absolute doc indices from `start`. `style_reqs` are the
    paragraph-style + bold text-style requests (safe at the original indices).
    `bullet_reqs` are the `createParagraphBullets` requests, returned SEPARATELY and
    sorted descending by start index: they consume leading tabs (shifting indices of
    text *after* them), so the caller applies them LAST and top-down, leaving every
    already-computed lower index valid. `end` is the doc index just past the body."""
    paras = _parse_markdown(body_text)
    text_parts: list[str] = []
    style_reqs: list[dict] = []
    bullet_reqs: list[dict] = []
    idx = start
    run: list[tuple[int, int]] = []
    run_ordered: bool | None = None

    def _flush_run() -> None:
        nonlocal run, run_ordered
        if run:
            bullet_reqs.append(
                {
                    "createParagraphBullets": {
                        "range": {"startIndex": run[0][0], "endIndex": run[-1][1]},
                        "bulletPreset": (
                            "NUMBERED_DECIMAL_ALPHA_ROMAN"
                            if run_ordered
                            else "BULLET_DISC_CIRCLE_SQUARE"
                        ),
                    }
                }
            )
        run = []
        run_ordered = None

    for p in paras:
        end = idx + len(p.text) + 1  # +1 for the paragraph's trailing "\n"
        text_parts.append(p.text)

        style: dict = {"namedStyleType": "NORMAL_TEXT"}
        fields = "namedStyleType"
        if p.indent_pt:
            style["indentStart"] = {"magnitude": p.indent_pt, "unit": "PT"}
            style["indentFirstLine"] = {"magnitude": p.indent_pt, "unit": "PT"}
            fields += ",indentStart,indentFirstLine"
        style_reqs.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": idx, "endIndex": end},
                    "paragraphStyle": style,
                    "fields": fields,
                }
            }
        )
        for bs, be in p.bold_ranges:
            if be > bs:
                style_reqs.append(_bold_request(idx + bs, idx + be))

        if p.bullet is not None:
            ordered = p.bullet == "ol"
            if run and run_ordered != ordered:
                _flush_run()
            run.append((idx, end))
            run_ordered = ordered
        else:
            _flush_run()
        idx = end
    _flush_run()

    bullet_reqs.sort(
        key=lambda r: r["createParagraphBullets"]["range"]["startIndex"], reverse=True
    )
    inserted = "".join(f"{t}\n" for t in text_parts)
    return inserted, style_reqs, bullet_reqs, idx


def _insert_note(
    creds: "Credentials",
    doc_id: str,
    heading_text: str,
    body_text: str,
    summary_text: str | None = None,
    keywords: list[str] | None = None,
) -> None:
    """Insert an H3 one-liner → H4 timestamp → H5 keywords → body → delimiter at the TOP.

    Newest note always lands first. Index 1 is the start of the block; a single
    batchUpdate applies its requests sequentially, so the style ranges see the
    just-inserted text. Insert-only — nothing existing is deleted or overwritten.

    Entry shape (goal 9): the LLM one-liner is **always the H3** headline, the timestamp
    **always the H4** beneath it, then optional **H5** keywords (comma-separated), the
    verbatim body, and the delimiter. The heading levels are **stable per note** — a
    missing summary uses a placeholder H3 (never promotes the timestamp) so the timestamp
    stays H4 for **every** entry; empty/missing keywords skip only the (leaf-level) H5.
    That uniformity is deliberate: a future "extract all H4s" search must reliably yield
    every timestamp, which only holds if the level never shifts per-entry. Nothing blocks
    the write.

    A trailing empty paragraph is styled as a light-gray delimiter with spacing
    above/below (goal 7a) — the Docs API has no horizontal-rule request, so a
    `borderBottom` on an empty paragraph is the closest insert-only equivalent —
    so consecutive notes read as separated entries, not a run-on wall.

    Goal 10: the body is rendered as **light markdown** (`_render_body`) — headings
    become bold NORMAL_TEXT lines (never `HEADING_*`), bullets become real Docs
    bullets, inline `**bold**` becomes bold runs — instead of one flat text run. A
    body with no markdown renders byte-identically to before (one NORMAL_TEXT line).
    Still ONE insert-only `batchUpdate`; `createParagraphBullets` requests apply last
    (top-down) because they consume leading tabs and shift later indices.
    """
    service = _docs_service(creds)

    summary_text = (summary_text or "").strip() or _NO_SUMMARY_PLACEHOLDER
    keyword_text = _keyword_line(keywords)

    # The entry chrome — ALWAYS H3 one-liner → H4 timestamp → optional H5 keywords.
    # The one-liner is a placeholder when blank so the timestamp never shifts off H4
    # (stable levels for later heading-extraction search). The body contributes NO
    # heading, ever — that is the goal-10 invariant enforced by `_render_body`.
    chrome: list[tuple[str, str]] = [
        (summary_text, "HEADING_3"),
        (heading_text, "HEADING_4"),
    ]
    if keyword_text:
        chrome.append((keyword_text, "HEADING_5"))

    chrome_text = "".join(f"{text}\n" for text, _ in chrome)
    body_start = 1 + len(chrome_text)
    body_rendered, body_style_reqs, bullet_reqs, body_end = _render_body(
        body_text, body_start
    )

    # Block: the chrome lines, the rendered body, then a trailing EMPTY paragraph
    # (the delimiter). One insertText for the whole block at the top (index 1).
    block = chrome_text + body_rendered + "\n"
    requests: list[dict] = [{"insertText": {"location": {"index": 1}, "text": block}}]

    idx = 1
    for text, style in chrome:
        end = idx + len(text) + 1
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": idx, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": style},
                    "fields": "namedStyleType",
                }
            }
        )
        idx = end

    # Body paragraph/bold styles (safe at original indices — no index shift).
    requests.extend(body_style_reqs)

    # The trailing empty paragraph → a light-gray horizontal delimiter with
    # breathing room, separating this note from the one below. Insert-only: this
    # styles the just-inserted empty paragraph, never any pre-existing content.
    # Applied BEFORE the bullet requests so its index is still valid (bullets remove
    # leading tabs and shift everything after them).
    delim_end = body_end + 1
    requests.append(
        {
            "updateParagraphStyle": {
                "range": {"startIndex": body_end, "endIndex": delim_end},
                "paragraphStyle": {
                    "namedStyleType": "NORMAL_TEXT",
                    "spaceAbove": {"magnitude": 8, "unit": "PT"},
                    "spaceBelow": {"magnitude": 8, "unit": "PT"},
                    "borderBottom": {
                        "color": {
                            "color": {
                                "rgbColor": {"red": 0.8, "green": 0.8, "blue": 0.8}
                            }
                        },
                        "width": {"magnitude": 1, "unit": "PT"},
                        "padding": {"magnitude": 0, "unit": "PT"},
                        "dashStyle": "SOLID",
                    },
                },
                "fields": "namedStyleType,spaceAbove,spaceBelow,borderBottom",
            }
        }
    )

    # Bullet requests LAST and top-down: `createParagraphBullets` removes each
    # paragraph's leading tabs (its nesting level), shifting the indices of text
    # after it — so every earlier request has already run against valid indices, and
    # each remaining (lower-index) bullet request is unaffected by higher removals.
    requests.extend(bullet_reqs)

    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()


async def insert_note(
    creds: "Credentials",
    doc_id: str,
    heading_text: str,
    body_text: str,
    summary_text: str | None = None,
    keywords: list[str] | None = None,
) -> None:
    """Insert a timestamped note at the top of the configured Doc (insert-only).

    Optional `summary_text` → the H3 one-liner headline (a placeholder when absent, so
    the timestamp stays H4); optional `keywords` → an H5 line (goal 9). Both are the
    only LLM-authored lines; the body stays verbatim.
    """
    await asyncio.to_thread(
        _insert_note, creds, doc_id, heading_text, body_text, summary_text, keywords
    )


# ── The sanctioned file-creates (bootstrap only) ──────────────────────────────


def _create_folder(
    creds: "Credentials", name: str, parent_id: str | None = None
) -> str:
    """Create a folder and return its id.

    Goal 8: each user's notes folder is app-created (`drive.file` can't write into a
    user-chosen folder). No `parent_id` → Drive root (the root "Dashboard Notes"
    folder). Goal 9: hierarchy folders pass `parent_id` = their parent folder's id
    so the whole tree stays under the root notes folder. Still `files.create` — the
    app's entire reachable set stays app-created.
    """
    service = _drive_service(creds)
    body: dict = {"name": name, "mimeType": _FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    created = service.files().create(body=body, fields="id").execute()
    return created["id"]


async def create_folder(
    creds: "Credentials", name: str, parent_id: str | None = None
) -> str:
    """Create a folder (root notes folder, or a hierarchy folder under `parent_id`)."""
    return await asyncio.to_thread(_create_folder, creds, name, parent_id)


def _create_doc_in_folder(creds: "Credentials", title: str, folder_id: str) -> str:
    """Create an empty Google Doc INSIDE `folder_id` and return its id.

    The parent is hard-coded to the caller-supplied folder — there is no code path
    that creates a file anywhere else, so the app's entire reachable file set lives
    under the notes folder. Called only by the bootstrap path.
    """
    service = _drive_service(creds)
    created = (
        service.files()
        .create(
            body={"name": title, "mimeType": _DOC_MIME, "parents": [folder_id]},
            fields="id",
        )
        .execute()
    )
    return created["id"]


async def create_doc_in_folder(creds: "Credentials", title: str, folder_id: str) -> str:
    """Create the notes Doc inside the designated folder (bootstrap; returns its id)."""
    return await asyncio.to_thread(_create_doc_in_folder, creds, title, folder_id)


# ── The one sanctioned metadata mutation: rename (goal 9) ─────────────────────


def _rename_file(creds: "Credentials", file_id: str, name: str) -> None:
    """Rename a file/folder — a **metadata-only** `files.update` (goal 9).

    The request body is **exactly `{"name": name}`** — never content, never
    `parents` (no add/remove), never `trashed`. This is the ONLY `files.update` in
    the module and is a **settings-path-only** caller (the router never reaches it —
    AST-asserted), so the Drive name and the routing name never drift. Not a
    content overwrite; the insert-only note write is untouched.
    """
    service = _drive_service(creds)
    service.files().update(fileId=file_id, body={"name": name}, fields="id").execute()


async def rename_file(creds: "Credentials", file_id: str, name: str) -> None:
    """Rename a Drive file/folder (metadata-only; settings/rename path only)."""
    await asyncio.to_thread(_rename_file, creds, file_id, name)
