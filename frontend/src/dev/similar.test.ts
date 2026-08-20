import { describe, expect, it } from "vitest";
import type { DevDraft, RelatedMatch } from "./draftTabs";
import { matchLabel, nothingNewMatch, visibleMatches } from "./similar";

function match(over: Partial<RelatedMatch> = {}): RelatedMatch {
  return {
    number: 12,
    type: "issue",
    state: "open",
    url: "https://github.com/org/repo/issues/12",
    title: "Login is broken",
    confidence: "high",
    reason: "same bug",
    ...over,
  };
}

function draft(over: Partial<DevDraft> = {}): DevDraft {
  return {
    id: 1,
    title: "Fix login",
    body: "",
    repo: "org/repo",
    status: "draft",
    kind: "issue",
    target_issue_number: null,
    target_issue_url: null,
    related_issues: null,
    sources: [],
    project_node_id: null,
    project_title: null,
    issue_url: null,
    issue_number: null,
    project_attached: false,
    created_at: "2026-08-11T00:00:00Z",
    ...over,
  };
}

describe("matchLabel", () => {
  it("labels a same-repo issue by number + title", () => {
    expect(matchLabel(match({ repo: "org/repo" }), "org/repo")).toBe(
      "#12 Login is broken",
    );
    // Pre-12b.1 stored matches have no repo — treated as same-repo.
    expect(matchLabel(match(), "org/repo")).toBe("#12 Login is broken");
  });

  it("labels a PR with its type badge and state", () => {
    expect(
      matchLabel(
        match({ number: 45, type: "pr", state: "merged", title: "Fix login" }),
        "org/repo",
      ),
    ).toBe("PR #45 (merged) Fix login");
  });

  it("prefixes a cross-repo match with its repo (12b.1)", () => {
    expect(matchLabel(match({ repo: "org/backend" }), "org/frontend")).toBe(
      "org/backend#12 Login is broken",
    );
    expect(
      matchLabel(
        match({ repo: "org/backend", number: 45, type: "pr", state: "open" }),
        "org/frontend",
      ),
    ).toBe("PR org/backend#45 (open) Login is broken");
  });
});

describe("visibleMatches", () => {
  it("shows every stored match on an issue card", () => {
    const d = draft({
      related_issues: [match(), match({ number: 45, type: "pr" })],
    });
    expect(visibleMatches(d)).toHaveLength(2);
  });

  it("renders nothing for unmatched (null) and matched-empty ([]) alike", () => {
    expect(visibleMatches(draft())).toHaveLength(0);
    expect(visibleMatches(draft({ related_issues: [] }))).toHaveLength(0);
  });

  it("keeps a comment card's secondary matches but drops its own target", () => {
    const d = draft({
      kind: "comment",
      target_issue_number: 12,
      related_issues: [
        match(),
        match({ number: 45, type: "pr", state: "merged" }),
      ],
    });
    const visible = visibleMatches(d);
    expect(visible).toHaveLength(1);
    expect(visible[0].type).toBe("pr");
  });
});

describe("nothingNewMatch", () => {
  it("surfaces the covered-by flag only when the drafter set it", () => {
    expect(nothingNewMatch(draft({ related_issues: [match()] }))).toBeNull();
    const flagged = draft({
      related_issues: [match({ nothing_new: true })],
    });
    expect(nothingNewMatch(flagged)?.number).toBe(12);
  });
});
