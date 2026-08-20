/**
 * The card's "Similar" line (goal 12b) — pure, so what renders as a clickable anchor
 * is unit-testable without a DOM.
 *
 * The body's code-appended `Related: #N` line is NOT clickable on the card (the body
 * is a plain textarea; `#N` auto-links only once filed on GitHub), so this line is the
 * pre-file affordance: the SAME validated matches, rendered as real anchors. Every
 * url/title here came from the code-fetched candidate list, never from the LLM.
 */

import type { DevDraft, RelatedMatch } from "./draftTabs";

/**
 * The anchor text for one match: `#12 Login is broken` / `PR #45 (merged) Fix login`.
 * A match living outside the draft's own repo (12b.1: matching is catalog-wide) is
 * prefixed with its repo — `org/backend#12 …` — so a cross-repo hit reads as one.
 */
export function matchLabel(m: RelatedMatch, draftRepo: string): string {
  const sameRepo = !m.repo || m.repo === draftRepo;
  const num = sameRepo ? `#${m.number}` : `${m.repo}#${m.number}`;
  const ref = m.type === "pr" ? `PR ${num} (${m.state})` : num;
  return m.title ? `${ref} ${m.title}` : ref;
}

/**
 * The matches the Similar line shows. A comment card keeps its SECONDARY matches
 * (e.g. the matched PR) — the target issue itself already has the header badge, so it
 * is excluded here. An unmatched draft (null) and a matched-empty one ([]) both render
 * no line.
 */
export function visibleMatches(draft: DevDraft): RelatedMatch[] {
  const matches = draft.related_issues ?? [];
  if (draft.kind !== "comment" || draft.target_issue_number === null) {
    return matches;
  }
  // After conversion the draft's repo IS the target's repo, so a same-numbered issue
  // in another repo is a genuine secondary match, not the target.
  return matches.filter(
    (m) =>
      !(
        m.type === "issue" &&
        m.number === draft.target_issue_number &&
        (!m.repo || m.repo === draft.repo)
      ),
  );
}

/**
 * The nothing-new callout: the drafter confirmed the top match already covers
 * everything the draft says. Rendered as "covered by #N — nothing new to add"; the
 * human dismisses — nothing is ever auto-dismissed.
 */
export function nothingNewMatch(draft: DevDraft): RelatedMatch | null {
  return (
    (draft.related_issues ?? []).find(
      (m) => m.type === "issue" && m.nothing_new,
    ) ?? null
  );
}
