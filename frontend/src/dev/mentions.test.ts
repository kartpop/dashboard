import { describe, expect, it } from "vitest";
import { filterMembers, insertMention, mentionQuery } from "./mentions";

const MEMBERS = [
  { login: "octocat", name: "Octo Cat" },
  { login: "kartikeya", name: null },
  { login: "cat-herder", name: "Herder" },
];

describe("mentionQuery", () => {
  it("finds the @ under construction at the caret", () => {
    const text = "cc @oct";
    expect(mentionQuery(text, text.length)).toEqual({ start: 3, query: "oct" });
  });

  it("opens on a bare @ at a word boundary", () => {
    expect(mentionQuery("@", 1)).toEqual({ start: 0, query: "" });
    expect(mentionQuery("see (@", 6)).toEqual({ start: 5, query: "" });
  });

  it("ignores a mid-word @ (emails) and closed-off mentions", () => {
    expect(mentionQuery("mail a@b.com", 12)).toBeNull();
    expect(mentionQuery("@octocat done", 13)).toBeNull(); // space ended it
  });

  it("only tracks the mention up to the caret", () => {
    // Caret inside "@octo|cat" → query is what's typed so far.
    expect(mentionQuery("@octocat", 5)).toEqual({ start: 0, query: "octo" });
  });
});

describe("filterMembers", () => {
  it("ranks login-prefix matches first, then substring/name hits", () => {
    expect(filterMembers(MEMBERS, "cat").map((m) => m.login)).toEqual([
      "cat-herder",
      "octocat",
    ]);
  });

  it("offers everyone on an empty query and respects the cap", () => {
    expect(filterMembers(MEMBERS, "")).toHaveLength(3);
    expect(filterMembers(MEMBERS, "", 2)).toHaveLength(2);
  });

  it("offers nothing when nothing matches — typing by hand still works", () => {
    expect(filterMembers(MEMBERS, "zzz")).toHaveLength(0);
  });
});

describe("insertMention", () => {
  it("replaces the typed query with plain @login text and moves the caret", () => {
    const text = "cc @oct please";
    const caret = 7; // after "@oct"
    const out = insertMention(
      text,
      { start: 3, query: "oct" },
      caret,
      "octocat",
    );
    expect(out.text).toBe("cc @octocat  please");
    expect(out.caret).toBe("cc @octocat ".length);
  });

  it("inserts plain text only — no markup, no metadata", () => {
    const out = insertMention("@", { start: 0, query: "" }, 1, "kartikeya");
    expect(out.text).toBe("@kartikeya ");
  });
});
