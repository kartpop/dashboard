"""Entry parser: an app-created notes Doc → its list of entries (goal 12).

Pure structural parsing over a `documents.get` payload — no Google call, no LLM, so it
unit-tests against a fixture document dict. Splits a Doc into entries by the goal-10
uniform shape: **H3 one-liner → H4 timestamp → optional H5 keywords → body → delimiter**
(goal-10 flipped goal-9's order — the **timestamp is the H4 line**, the H3 is the
human-readable one-liner). The body contributes no heading, ever (the goal-10
`_render_body` invariant), so the H3/H4/H5 chrome reliably delimits every entry.

Docs are **newest-first** (captures prepend at the top), so downstream new-entry
selection is by the parsed timestamp, never by document position — the descending order
is only an early-exit hint. Multiple entries can legitimately share one minute-granular
timestamp (batch pastes), so each entry also carries a stable `key` (a hash of its
timestamp + one-liner) that the per-doc cursor uses to disambiguate same-minute
boundary entries.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

# The goal-10 entry chrome named styles.
_H3 = "HEADING_3"
_H4 = "HEADING_4"
_H5 = "HEADING_5"

# Inverse of `writes.service.format_note_heading`: `6-July-2026, 8:41 PM IST`.
_TS_RE = re.compile(
    r"^\s*(\d{1,2})-([A-Za-z]+)-(\d{4}),\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*IST\s*$"
)


@dataclass
class Entry:
    """One parsed Doc entry. `ts` is the IST wall-clock timestamp (tz-naive,
    minute-granular) read from the H4 line; `one_liner` is the H3 title; `keywords` is
    the optional H5 line; `body` is the verbatim entry body. `key` is stable across
    scans (timestamp + one-liner) and disambiguates same-minute entries at the cursor
    boundary."""

    one_liner: str
    ts: datetime
    body: str
    keywords: str | None = None
    key: str = field(default="")

    def __post_init__(self) -> None:
        if not self.key:
            self.key = entry_key(self.ts, self.one_liner)


def entry_key(ts: datetime, one_liner: str) -> str:
    """A stable key for an entry — hash of its (minute-granular) timestamp + one-liner.

    Used as the cursor boundary key so a same-minute entry captured after a scan is
    processed exactly once (not skipped by a strictly-newer rule, not re-processed)."""
    stamp = ts.strftime("%Y-%m-%dT%H:%M")
    raw = f"{stamp}|{(one_liner or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_timestamp(text: str) -> datetime | None:
    """Parse an H4 timestamp line back to a tz-naive IST datetime, or None if the line
    is not in the locked format. Minute granularity (seconds are always 0)."""
    m = _TS_RE.match(text or "")
    if not m:
        return None
    day, month_name, year, hour12, minute, ampm = m.groups()
    try:
        month = datetime.strptime(month_name, "%B").month
    except ValueError:
        return None
    hour = int(hour12) % 12
    if ampm.upper() == "PM":
        hour += 12
    try:
        return datetime(int(year), month, int(day), hour, int(minute))
    except ValueError:
        return None


def _paragraph_text(paragraph: dict) -> str:
    """Concatenate a paragraph's text runs, stripping the trailing newline Docs adds."""
    parts: list[str] = []
    for el in paragraph.get("elements", []) or []:
        run = el.get("textRun")
        if run and isinstance(run.get("content"), str):
            parts.append(run["content"])
    return "".join(parts).rstrip("\n")


def extract_paragraphs(document: dict) -> list[tuple[str, str]]:
    """Flatten a `documents.get` payload to `[(named_style, text), ...]` in doc order.

    Only paragraph structural elements contribute; tables / section breaks are ignored
    (the notes writer only ever inserts paragraphs). `named_style` is the paragraph's
    `namedStyleType` (e.g. HEADING_3, NORMAL_TEXT), defaulting to NORMAL_TEXT."""
    out: list[tuple[str, str]] = []
    content = ((document or {}).get("body") or {}).get("content") or []
    for element in content:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        style = (paragraph.get("paragraphStyle") or {}).get(
            "namedStyleType", "NORMAL_TEXT"
        )
        out.append((style, _paragraph_text(paragraph)))
    return out


def parse_entries(document: dict) -> list[Entry]:
    """Parse a Doc into its entries (goal-10 shape).

    An entry starts at each H3 (the one-liner); the first following H4 is its timestamp,
    an optional H5 immediately after is its keywords, and every subsequent paragraph up
    to the next H3 is body. Paragraphs before the first H3 (document preamble) are
    ignored. An entry whose H4 timestamp is missing/unparseable is dropped (it cannot be
    placed against the cursor) — a malformed heading never silently shifts later
    entries because the body carries no headings by the goal-10 invariant.
    """
    paragraphs = extract_paragraphs(document)

    entries: list[Entry] = []
    cur_one_liner: str | None = None
    cur_ts: datetime | None = None
    cur_keywords: str | None = None
    cur_body: list[str] = []
    seen_ts = False  # has the current entry's H4 timestamp been consumed yet?

    def flush() -> None:
        nonlocal cur_one_liner, cur_ts, cur_keywords, cur_body, seen_ts
        if cur_one_liner is not None and cur_ts is not None:
            body = "\n".join(cur_body).strip()
            entries.append(
                Entry(
                    one_liner=cur_one_liner.strip(),
                    ts=cur_ts,
                    body=body,
                    keywords=(cur_keywords.strip() if cur_keywords else None),
                )
            )
        cur_one_liner = None
        cur_ts = None
        cur_keywords = None
        cur_body = []
        seen_ts = False

    for style, text in paragraphs:
        if style == _H3:
            flush()
            cur_one_liner = text
            continue
        if cur_one_liner is None:
            continue  # preamble before the first entry
        if style == _H4 and not seen_ts:
            cur_ts = parse_timestamp(text)
            seen_ts = True
            continue
        if style == _H5 and seen_ts and cur_keywords is None and not cur_body:
            cur_keywords = text
            continue
        # Everything else is body. Skip the blank delimiter paragraph.
        if text.strip():
            cur_body.append(text)

    flush()
    return entries
