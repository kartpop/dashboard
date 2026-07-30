"""Thin GitHub client — the app's FIRST non-Google write surface (goal 12).

One concern per function, no orchestration (that lives in `dev.service`): validate a
PAT, enumerate the repos + ProjectsV2 projects the PAT can see (config population), and
the two write steps of filing — create an issue (REST) and attach it to a project
(GraphQL). Every call is authenticated with the user's fine-grained PAT, passed in
explicitly (never read from a global) and never logged.

Deterministic code only — no LLM ever reaches this module. A non-2xx response raises
`GithubError(status, message)`, which the service maps to an `ApiError`; the caller
records partial state (the issue number the moment creation succeeds) so a retry never
double-creates. The module-level `httpx` calls are wrapped so tests monkeypatch the
public functions directly.
"""

from __future__ import annotations

import logging

import httpx

from app.dev import config

_log = logging.getLogger("dev.github")

_REST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GithubError(RuntimeError):
    """A GitHub API call failed. `status` is the HTTP status (0 for a transport error);
    `message` is a human-readable reason (never contains the PAT)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _auth(pat: str) -> dict[str, str]:
    return {**_REST_HEADERS, "Authorization": f"Bearer {pat}"}


def _reason(resp: httpx.Response) -> str:
    """A safe human message from a GitHub error response (its `message` field, capped)."""
    try:
        data = resp.json()
        msg = data.get("message") if isinstance(data, dict) else None
    except Exception:
        msg = None
    return (msg or resp.reason_phrase or "GitHub request failed")[:300]


async def validate_pat(pat: str) -> dict:
    """Ping the viewer endpoint (`GET /user`) to confirm the PAT is valid.

    Returns `{login, name}` on success; raises `GithubError` on any non-2xx (401 for a
    bad/expired token). Used on PAT save and by the scan's config-complete gate."""
    async with httpx.AsyncClient(timeout=config.GITHUB_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{config.GITHUB_API_BASE}/user", headers=_auth(pat)
            )
        except httpx.HTTPError as exc:
            raise GithubError(0, f"Could not reach GitHub: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise GithubError(resp.status_code, _reason(resp))
    data = resp.json()
    return {"login": data.get("login"), "name": data.get("name")}


async def list_repos(pat: str, *, max_pages: int = 10) -> list[dict]:
    """Enumerate the repos the PAT can see (`GET /user/repos`, paginated).

    A fine-grained PAT is scoped to a chosen repo set at mint time, so this list is
    exactly the granted repos — the user picks issue targets from it, never hand-typing
    `org/repo`. Returns `[{full_name, description, private}]`, sorted by full name."""
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=config.GITHUB_TIMEOUT) as client:
        for page in range(1, max_pages + 1):
            try:
                resp = await client.get(
                    f"{config.GITHUB_API_BASE}/user/repos",
                    headers=_auth(pat),
                    params={"per_page": 100, "page": page, "sort": "full_name"},
                )
            except httpx.HTTPError as exc:
                raise GithubError(0, f"Could not reach GitHub: {exc}") from exc
            if resp.status_code // 100 != 2:
                raise GithubError(resp.status_code, _reason(resp))
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            for r in batch:
                out.append(
                    {
                        "full_name": r.get("full_name"),
                        "description": r.get("description") or "",
                        "private": bool(r.get("private")),
                    }
                )
            if len(batch) < 100:
                break
    out.sort(key=lambda r: (r["full_name"] or "").lower())
    return out


_PROJECTS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    projectsV2(first: 50) {
      nodes { id title number }
    }
  }
}
""".strip()


async def _graphql(pat: str, query: str, variables: dict) -> dict:
    async with httpx.AsyncClient(timeout=config.GITHUB_TIMEOUT) as client:
        try:
            resp = await client.post(
                config.GITHUB_GRAPHQL_URL,
                headers=_auth(pat),
                json={"query": query, "variables": variables},
            )
        except httpx.HTTPError as exc:
            raise GithubError(0, f"Could not reach GitHub: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise GithubError(resp.status_code, _reason(resp))
    data = resp.json()
    if isinstance(data, dict) and data.get("errors"):
        first = data["errors"][0] if data["errors"] else {}
        raise GithubError(422, (first.get("message") or "GraphQL error")[:300])
    return data.get("data") or {}


async def list_projects_for_repo(pat: str, owner: str, repo: str) -> list[dict]:
    """List the ProjectsV2 projects linked to a repo (`repository.projectsV2`).

    Returns `[{node_id, title, number}]`. The user picks the repo's default project from
    this list; the per-card dropdown can override at file time."""
    data = await _graphql(pat, _PROJECTS_QUERY, {"owner": owner, "name": repo})
    nodes = ((data.get("repository") or {}).get("projectsV2") or {}).get("nodes") or []
    return [
        {"node_id": n.get("id"), "title": n.get("title"), "number": n.get("number")}
        for n in nodes
        if n.get("id")
    ]


async def create_issue(pat: str, owner: str, repo: str, title: str, body: str) -> dict:
    """Create an issue (`POST /repos/{owner}/{repo}/issues`).

    Returns `{number, url, node_id}` — the url + number are recorded the moment this
    succeeds (partial-state idempotency), so a project-attach failure never causes a
    double-create on retry. Raises `GithubError` on any non-2xx."""
    async with httpx.AsyncClient(timeout=config.GITHUB_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{config.GITHUB_API_BASE}/repos/{owner}/{repo}/issues",
                headers=_auth(pat),
                json={"title": title, "body": body},
            )
        except httpx.HTTPError as exc:
            raise GithubError(0, f"Could not reach GitHub: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise GithubError(resp.status_code, _reason(resp))
    data = resp.json()
    return {
        "number": data.get("number"),
        "url": data.get("html_url"),
        "node_id": data.get("node_id"),
    }


_ADD_TO_PROJECT = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
""".strip()


async def add_issue_to_project(
    pat: str, project_node_id: str, issue_node_id: str
) -> str:
    """Attach an already-created issue to a ProjectsV2 project (`addProjectV2ItemById`).

    Returns the created project item id. This is the ONLY step a partial-state retry
    re-runs (the issue already exists). Raises `GithubError` on failure."""
    data = await _graphql(
        pat,
        _ADD_TO_PROJECT,
        {"projectId": project_node_id, "contentId": issue_node_id},
    )
    item = (data.get("addProjectV2ItemById") or {}).get("item") or {}
    return item.get("id") or ""
