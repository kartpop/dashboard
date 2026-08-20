/**
 * The @-mention typeahead's text mechanics (goal 12b) — pure, DOM-free.
 *
 * Mentions are PLAIN TEXT: picking a login inserts `@login` at the caret and nothing
 * more — GitHub linkifies and notifies once the issue/comment is filed. There is no
 * new write surface here, and the member list these functions filter is never fed to
 * any LLM (it exists for the human's editor only).
 */

export interface Member {
  login: string;
  name: string | null;
}

/** An in-progress mention: the `@` position and what's been typed after it. */
export interface MentionQuery {
  /** Index of the `@` character in the text. */
  start: number;
  /** What follows the `@`, up to the caret. */
  query: string;
}

// GitHub logins: alphanumerics and hyphens.
const QUERY_CHARS = /^[a-zA-Z0-9-]*$/;

/**
 * The active mention at the caret, if any: the nearest `@` at a word boundary whose
 * run of login characters reaches the caret. `hi @oct|` → {start: 3, query: "oct"};
 * `a@b.com|` → null (mid-word `@` is an email, not a mention).
 */
export function mentionQuery(text: string, caret: number): MentionQuery | null {
  const upto = text.slice(0, caret);
  const at = upto.lastIndexOf("@");
  if (at === -1) return null;
  if (at > 0 && !/[\s(]/.test(upto[at - 1])) return null; // mid-word: an email etc.
  const query = upto.slice(at + 1);
  if (!QUERY_CHARS.test(query)) return null; // whitespace/punctuation ended the mention
  return { start: at, query };
}

/** Members whose login (or name) continues the typed query — login-prefix matches
 * first, capped so the dropdown stays a shortlist. */
export function filterMembers(
  members: Member[],
  query: string,
  cap = 8,
): Member[] {
  const q = query.toLowerCase();
  const starts = members.filter((m) => m.login.toLowerCase().startsWith(q));
  const contains = members.filter(
    (m) =>
      !m.login.toLowerCase().startsWith(q) &&
      (m.login.toLowerCase().includes(q) ||
        (m.name ?? "").toLowerCase().includes(q)),
  );
  return [...starts, ...contains].slice(0, cap);
}

/**
 * Replace the in-progress `@query` with the picked `@login ` (trailing space, so
 * typing continues naturally). Returns the new text and caret position.
 */
export function insertMention(
  text: string,
  mention: MentionQuery,
  caret: number,
  login: string,
): { text: string; caret: number } {
  const inserted = `@${login} `;
  const next = text.slice(0, mention.start) + inserted + text.slice(caret);
  return { text: next, caret: mention.start + inserted.length };
}
