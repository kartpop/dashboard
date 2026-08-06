import { describe, expect, it } from "vitest";
import { type ScanTally, formatScanTally } from "./scanTally";

function tally(over: Partial<ScanTally> = {}): ScanTally {
  return {
    docs_read: 2,
    new_entries: 2,
    drafts_created: 1,
    synthesis_failed: false,
    ...over,
  };
}

describe("formatScanTally", () => {
  it("states the counts of a normal scan", () => {
    expect(formatScanTally(tally())).toBe(
      "2 docs read · 2 new entries · 1 draft",
    );
  });

  it("says WHY a read entry produced no draft — the case that read as a dropped doc", () => {
    // The real 2026-08-06 scan: both Docs read, both entries considered, one draft —
    // the other entry restated already-filed work, which looked like an unread Doc.
    const line = formatScanTally(tally({ drafts_created: 0 }));
    expect(line).toContain("2 docs read");
    expect(line).toContain("2 new entries");
    expect(line).toContain("already drafted or filed");
  });

  it("does not blame the do-not-redraft list when synthesis actually failed", () => {
    const line = formatScanTally(
      tally({ drafts_created: 0, synthesis_failed: true }),
    );
    expect(line).toContain("synthesis failed");
    expect(line).toContain("re-scanned");
    expect(line).not.toContain("already drafted or filed");
  });

  it("distinguishes nothing-new from nothing-drafted", () => {
    expect(formatScanTally(tally({ new_entries: 0, drafts_created: 0 }))).toBe(
      "2 docs read · no new entries since the last scan",
    );
  });

  it("points at Config when no Doc was read at all", () => {
    const line = formatScanTally(
      tally({ docs_read: 0, new_entries: 0, drafts_created: 0 }),
    );
    expect(line).toContain("No source Docs were read");
    expect(line).toContain("Config");
  });

  it("pluralises each count", () => {
    expect(
      formatScanTally(
        tally({ docs_read: 1, new_entries: 1, drafts_created: 1 }),
      ),
    ).toBe("1 doc read · 1 new entry · 1 draft");
    expect(formatScanTally(tally({ drafts_created: 3 }))).toContain("3 drafts");
  });
});
