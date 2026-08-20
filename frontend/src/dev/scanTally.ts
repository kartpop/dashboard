/**
 * What a scan actually did, in one line.
 *
 * `run_scan` has always returned this tally and the view has always thrown it away,
 * which made two very different outcomes look identical from the outside: a source Doc
 * that was never read, and a Doc that was read whose entries the synthesiser
 * deliberately skipped because that work is already drafted or filed. The second is the
 * common case (the do-not-redraft contract doing its job) and it read as a dropped
 * source. So the counts are stated plainly, and the zero-draft cases say *why* they're
 * zero.
 */

export interface ScanTally {
  docs_read: number;
  new_entries: number;
  drafts_created: number;
  /** The LLM call errored or truncated: the entries are un-consumed, not un-actionable. */
  synthesis_failed: boolean;
  /** goal 12b: drafts newly linked to existing GitHub issues/PRs this scan. */
  linked?: number;
  /** goal 12b: drafts converted into comment drafts on an existing issue. */
  converted?: number;
  /** goal 12b: some repo's match phase was skipped (GitHub read / matcher failure) —
   * those drafts stay unmatched and retry next scan. Reported, never hidden. */
  matching_skipped?: boolean;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * The match phase runs over the whole unfiled backlog, so links/conversions can land
 * even on a scan with zero new entries — every terminal branch below carries this
 * suffix, not just the drafts-created one.
 */
function matchSuffix(tally: ScanTally): string {
  const bits: string[] = [];
  if (tally.linked) {
    bits.push(
      tally.linked === 1
        ? "1 linked to an existing issue or PR"
        : `${tally.linked} linked to existing issues or PRs`,
    );
  }
  if (tally.converted) {
    bits.push(
      `${plural(tally.converted, "converted to a comment", "converted to comments")}`,
    );
  }
  let out = bits.length ? ` (${bits.join(", ")})` : "";
  if (tally.matching_skipped) {
    out += " · match check skipped — it will retry on the next scan";
  }
  return out;
}

export function formatScanTally(tally: ScanTally): string {
  const docs = plural(tally.docs_read, "doc", "docs");

  if (tally.docs_read === 0) {
    return "No source Docs were read — check the source selection in Config.";
  }
  if (tally.synthesis_failed) {
    return `${docs} read · ${plural(tally.new_entries, "new entry", "new entries")} · synthesis failed — the entries are kept and will be re-scanned${matchSuffix(tally)}`;
  }
  if (tally.new_entries === 0) {
    return `${docs} read · no new entries since the last scan${matchSuffix(tally)}`;
  }

  const entries = plural(tally.new_entries, "new entry", "new entries");
  if (tally.drafts_created === 0) {
    return `${docs} read · ${entries} · no new drafts — that work is already drafted or filed${matchSuffix(tally)}`;
  }
  return `${docs} read · ${entries} · ${plural(tally.drafts_created, "draft", "drafts")}${matchSuffix(tally)}`;
}
