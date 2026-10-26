import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { scoreMatch, smartFilter } from "./rejection-search.ts";

const REASONS = [
  "Skills do not meet requirements",
  "Communication skills",
  "Domain experience mismatch",
  "Overqualified for the role",
  "Not comfortable with background check / drug test",
  "Not aligned with employment type (W2 / C2C / 1099)",
  "Work authorization / visa constraints",
  "Already submitted to same client / hiring manager by another vendor",
  "Fake candidate — Multiple profiles/resumes; misrepresentation of past experience",
];

describe("scoreMatch", () => {
  it("matches a multi-word query against a single run-together word in the reason", () => {
    // The actual requirement this feature was built for: "over qualified"
    // (two words) must find "Overqualified for the role" (one word, no
    // space) — not just literal substring/exact matches.
    assert.ok(scoreMatch("Overqualified for the role", "over qualified") > 0);
  });

  it("does not score a short query as a full match via a coincidental mid-word substring", () => {
    // Regression for PR #840 review: "ab" used to score 70 (treated as
    // every query word found) against this reason purely because "ab" sits
    // inside "comfortable" — nothing to do with what was typed.
    assert.equal(scoreMatch("Not comfortable with background check / drug test", "ab"), 0);
  });

  it("still matches a short query that is a real word or acronym", () => {
    assert.ok(scoreMatch("Not aligned with employment type (W2 / C2C / 1099)", "w2") > 0);
  });

  it("still matches an acronym whose letters appear in order across the reason's words", () => {
    // "wac" -> Work / Authorization / Constraints (skipping "visa"), in order.
    assert.ok(scoreMatch("Work authorization / visa constraints", "wac") > 0);
  });

  it("does not score a query whose letters appear scattered out of order in the acronym", () => {
    // Regression: "com" used to score 15 against this reason because its
    // first-letters string ("fcmprmope") happens to contain a c, an o, and
    // an m — just not in that order, and "com" doesn't appear in the text.
    assert.equal(
      scoreMatch(
        "Fake candidate — Multiple profiles/resumes; misrepresentation of past experience",
        "com"
      ),
      0
    );
  });

  it("ranks an exact match above a prefix match above a partial-word match", () => {
    const exact = scoreMatch("Communication skills", "Communication skills");
    const prefix = scoreMatch("Communication skills", "comm");
    const partial = scoreMatch("Domain experience mismatch", "experience");
    assert.ok(exact > prefix);
    assert.ok(prefix > partial);
    assert.ok(partial > 0);
  });

  it("returns 0 for an empty or whitespace-only query", () => {
    assert.equal(scoreMatch("Communication skills", ""), 0);
    assert.equal(scoreMatch("Communication skills", "   "), 0);
  });

  it("does not match a query longer than the reason's own acronym", () => {
    // "Open to remote only" -> acronym "otro" (4 letters); a 5-letter query
    // can never be a subsequence of a shorter string, so this must fall
    // through to 0 rather than throwing or matching by accident.
    assert.equal(scoreMatch("Open to remote only", "otros"), 0);
  });

  it("does not match via the acronym branch when the reason has no acronym at all", () => {
    // Punctuation-only text strips to zero words in wordsOf, so the acronym
    // is "" — isSubsequence must handle an empty text without matching any
    // non-empty query (and without throwing).
    assert.equal(scoreMatch("---", "abc"), 0);
  });
});

describe("smartFilter", () => {
  it("finds the target reason by a keyword, not the exact reason text", () => {
    const results = smartFilter(REASONS, "over qualified");
    assert.equal(results[0], "Overqualified for the role");
  });

  it("excludes reasons that don't match at all", () => {
    const results = smartFilter(REASONS, "visa");
    assert.deepEqual(results, ["Work authorization / visa constraints"]);
  });

  it("excludes everything for a short, unrelated query (the 'ab' regression)", () => {
    assert.deepEqual(smartFilter(REASONS, "ab"), []);
  });

  it("orders multiple matches with the best match first", () => {
    const results = smartFilter(REASONS, "submitted");
    assert.equal(results[0], "Already submitted to same client / hiring manager by another vendor");
  });

  it("does not include an unrelated reason for 'com' just because its acronym has scattered c/o/m letters", () => {
    const results = smartFilter(REASONS, "com");
    assert.ok(!results.includes("Fake candidate — Multiple profiles/resumes; misrepresentation of past experience"));
    assert.ok(results.includes("Communication skills"));
  });
});
