import assert from "node:assert/strict";
import { test } from "node:test";

import { escapeCSV, toCsv } from "./csv.ts";

// --- formula injection (CWE-1236) -------------------------------------------
// Job titles, customer names and recruiter emails are attacker-influenced:
// they come from JobDiva records and recruiter free-text.

test("neutralizes a formula-leading value", () => {
  assert.equal(escapeCSV('=HYPERLINK("http://evil","click")'), `"'=HYPERLINK(""http://evil"",""click"")"`);
});

test("neutralizes every formula-trigger prefix", () => {
  for (const prefix of ["=", "+", "-", "@", "\t", "\r"]) {
    const escaped = escapeCSV(`${prefix}cmd`);
    assert.ok(
      escaped.startsWith("'") || escaped.startsWith(`"'`),
      `${JSON.stringify(prefix)} was not neutralized: ${JSON.stringify(escaped)}`,
    );
  }
});

test("leaves ordinary values untouched", () => {
  assert.equal(escapeCSV("Data Engineer"), "Data Engineer");
  assert.equal(escapeCSV("—"), "—");
  assert.equal(escapeCSV(""), "");
});

test("does not treat a mid-string = as a formula", () => {
  assert.equal(escapeCSV("Level=3"), "Level=3");
});

test("a negative duration is quoted, not silently altered", () => {
  // "-5m" trips the formula guard; the apostrophe must be the only change.
  assert.equal(escapeCSV("-5m"), "'-5m");
});

// --- field integrity ---------------------------------------------------------

test("quotes and doubles embedded quotes", () => {
  assert.equal(escapeCSV('Acme "Holdings"'), '"Acme ""Holdings"""');
});

test("quotes values containing a comma", () => {
  assert.equal(escapeCSV("Smith, John"), '"Smith, John"');
});

test("quotes a bare CR so it cannot split the row", () => {
  assert.equal(escapeCSV("a\rb"), '"a\rb"');
  assert.equal(escapeCSV("a\nb"), '"a\nb"');
});

// --- toCsv -------------------------------------------------------------------

test("builds a header row plus one line per row", () => {
  const csv = toCsv(["Job", "Launched"], [["Data Engineer", "12"], ["QA Lead", "3"]]);
  assert.equal(csv, "Job,Launched\nData Engineer,12\nQA Lead,3");
});

test("escapes cells in both the header and the body", () => {
  const csv = toCsv(["A,B"], [["=evil"]]);
  assert.equal(csv, `"A,B"\n'=evil`);
});

test("every row has the same field count as the header", () => {
  const headers = ["a", "b", "c"];
  const csv = toCsv(headers, [["1", "2", "3"], ["x,y", '"q"', "-z"]]);
  for (const line of csv.split("\n")) {
    // Count only commas outside quoted sections.
    let fields = 1;
    let inQuotes = false;
    for (let i = 0; i < line.length; i++) {
      if (line[i] === '"') inQuotes = !inQuotes;
      else if (line[i] === "," && !inQuotes) fields++;
    }
    assert.equal(fields, headers.length, `field count drifted on: ${line}`);
  }
});
