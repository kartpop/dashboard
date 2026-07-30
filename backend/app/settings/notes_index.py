"""The notes index — one JSON folder/Doc forest per user (goal 9).

Pure tree logic: parsing, validation, path rendering, and the diff-by-`node_id`
that the Drive materialization in `settings.service` drives. NO Google calls live
here (that is `service.set_notes_index`); this module is unit-testable without
creds.

A node is a dict `{node_id, name, kind, drive_id, children}`:
- **Leaves are Docs** (`kind == "doc"`, no children).
- **Inner nodes are folders** (`kind == "folder"`; children may be empty).
- `node_id` is a stable app-generated key (uuid) — it rides the settings widget so
  the diff can tell *rename* from *delete + add*.
- `drive_id` is the materialized Drive file/folder id (None until created). It is
  **never** trusted from a request payload (ID hygiene, ADR layer 3) — it is read
  from the stored index or produced by a Drive create.

The forest is rooted under the app-created "Dashboard Notes" folder
(`notes_folder_id`); the default Doc (`notes_doc_id`) stays OUTSIDE it and is the
routing fallback.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Optional

from app.errors import ApiError

# Validation caps — these also bound the classifier prompt (the hierarchy is
# injected into it), so keep them tight (goal 9, item 1).
MAX_NAME_LEN = 60
MAX_DEPTH = 5
MAX_LEAF_DOCS = 50

KIND_FOLDER = "folder"
KIND_DOC = "doc"


# ── Parse / serialize ─────────────────────────────────────────────────────────


def parse(raw: Optional[str]) -> list[dict[str, Any]]:
    """Parse the stored JSON forest into a list of node dicts (empty on junk)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def serialize(forest: list[dict[str, Any]]) -> str:
    return json.dumps(forest)


# ── Traversal helpers ─────────────────────────────────────────────────────────


def iter_nodes(forest: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Depth-first over every node in the forest (parents before children)."""
    for node in forest:
        yield node
        yield from iter_nodes(node.get("children") or [])


def index_by_node_id(forest: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {n["node_id"]: n for n in iter_nodes(forest) if n.get("node_id")}


def leaf_paths(forest: list[dict[str, Any]]) -> list[str]:
    """Every Doc leaf as a slash-joined path, e.g. `conversations/john/growth`.

    Only `doc` nodes are leaves in the routing sense (folders are never a note
    destination). Order is depth-first, stable — this is what the classifier
    prompt and the review dropdown render.
    """
    out: list[str] = []

    def walk(nodes: list[dict[str, Any]], prefix: list[str]) -> None:
        for n in nodes:
            path = prefix + [n.get("name", "")]
            if n.get("kind") == KIND_DOC:
                out.append("/".join(path))
            walk(n.get("children") or [], path)

    walk(forest, [])
    return out


def locate_chain(
    forest: list[dict[str, Any]], path: Optional[str]
) -> Optional[list[dict[str, Any]]]:
    """Return the root→leaf node chain for a Doc-leaf `path`, else None.

    Case-insensitive exact match on each segment; the final node must be a `doc`
    (a folder path or an unknown path → None). The chain lets the self-heal
    re-create missing ancestor folders in order.
    """
    if not path:
        return None
    segments = [s.strip().lower() for s in path.split("/") if s.strip()]
    if not segments:
        return None
    nodes = forest
    chain: list[dict[str, Any]] = []
    for seg in segments:
        node = next(
            (n for n in nodes if (n.get("name") or "").strip().lower() == seg), None
        )
        if node is None:
            return None
        chain.append(node)
        nodes = node.get("children") or []
    return chain if chain[-1].get("kind") == KIND_DOC else None


def resolve_path(forest: list[dict[str, Any]], path: Optional[str]) -> Optional[dict]:
    """Return the Doc-leaf node at `path` (case-insensitive exact match), else None.

    Deterministic dispose (ADR layer 3): the LLM proposes a *path*; this maps it to
    the stored node (and its `drive_id`). A folder path or an unknown path → None →
    the caller falls back to the default Doc.
    """
    chain = locate_chain(forest, path)
    return chain[-1] if chain else None


def canonical_path(chain: list[dict[str, Any]]) -> str:
    """The stored-casing path for a node chain, e.g. `conversations/john/growth`."""
    return "/".join((n.get("name") or "").strip() for n in chain)


def selected_doc_leaves(
    forest: list[dict[str, Any]], node_ids: set[str]
) -> list[dict[str, Any]]:
    """Resolve selected `node_id`s → the concrete Doc leaves they cover (goal 12).

    A selected **Doc** node contributes itself; a selected **folder** contributes
    *every* Doc under it, recursively — so a folder selection auto-includes a Doc added
    beneath it later (resolution happens at scan time against the live index). Returns
    `[{drive_id, path}]`, de-duplicated by `drive_id` (a Doc selected both directly and
    via an ancestor folder appears once); leaves with no materialized `drive_id` are
    skipped. Order is depth-first, stable. Never trusts an id from a payload — drive_ids
    come from the stored index only.
    """
    out: dict[str, str] = {}

    def walk(nodes: list[dict[str, Any]], prefix: list[str], under: bool) -> None:
        for n in nodes:
            path = prefix + [(n.get("name") or "").strip()]
            selected = under or (n.get("node_id") in node_ids)
            if n.get("kind") == KIND_DOC:
                drive_id = n.get("drive_id")
                if selected and drive_id and drive_id not in out:
                    out[drive_id] = "/".join(path)
            else:
                walk(n.get("children") or [], path, selected)

    walk(forest, [], False)
    return [{"drive_id": did, "path": path} for did, path in out.items()]


# ── Validation (422 on violation) ─────────────────────────────────────────────


def _bad(msg: str) -> ApiError:
    return ApiError(422, "invalid_notes_index", msg)


def validate(forest: list[dict[str, Any]]) -> None:
    """Enforce the goal-9 caps; raise ApiError(422) on the first violation.

    - sibling names unique (case-insensitive), non-empty
    - names ≤ 60 chars, no newlines / control chars
    - `doc` nodes have no children (a Doc is a leaf); `kind` is folder|doc
    - node_ids present + globally unique
    - depth ≤ 5, ≤ 50 leaf Docs total
    """
    seen_ids: set[str] = set()
    leaf_docs = 0

    def check(nodes: list[dict[str, Any]], depth: int) -> None:
        nonlocal leaf_docs
        if depth > MAX_DEPTH:
            raise _bad(f"Hierarchy is too deep (max {MAX_DEPTH} levels).")
        names_lower: set[str] = set()
        for n in nodes:
            if not isinstance(n, dict):
                raise _bad("Malformed node.")
            node_id = n.get("node_id")
            if not node_id or not isinstance(node_id, str):
                raise _bad("Every node needs a node_id.")
            if node_id in seen_ids:
                raise _bad("Duplicate node_id in the tree.")
            seen_ids.add(node_id)

            name = n.get("name")
            if not isinstance(name, str) or not name.strip():
                raise _bad("Node names must not be empty.")
            if len(name) > MAX_NAME_LEN:
                raise _bad(f"Name too long (max {MAX_NAME_LEN} chars): {name!r}.")
            if any(ord(ch) < 32 for ch in name):
                raise _bad("Names may not contain newlines or control characters.")
            key = name.strip().lower()
            if key in names_lower:
                raise _bad(f"Duplicate sibling name: {name!r}.")
            names_lower.add(key)

            kind = n.get("kind")
            if kind not in (KIND_FOLDER, KIND_DOC):
                raise _bad("Node kind must be 'folder' or 'doc'.")
            children = n.get("children") or []
            if not isinstance(children, list):
                raise _bad("children must be a list.")
            if kind == KIND_DOC:
                if children:
                    raise _bad("A Doc node cannot have children.")
                leaf_docs += 1
            else:
                check(children, depth + 1)

    check(forest, 1)
    if leaf_docs > MAX_LEAF_DOCS:
        raise _bad(f"Too many Docs ({leaf_docs}); the limit is {MAX_LEAF_DOCS}.")


# ── Sanitize an incoming forest (drop client-supplied drive_ids) ──────────────


def sanitize_incoming(forest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the forest keeping only node_id/name/kind/children.

    Client-supplied `drive_id`s are dropped here (never trusted): materialization
    re-derives every drive_id from the stored index (by node_id) or a fresh create.
    """
    out: list[dict[str, Any]] = []
    for n in forest:
        out.append(
            {
                "node_id": n.get("node_id"),
                "name": (n.get("name") or "").strip(),
                "kind": n.get("kind"),
                "children": sanitize_incoming(n.get("children") or []),
            }
        )
    return out
