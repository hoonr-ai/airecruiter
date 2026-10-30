// Node 20's built-in test runner loads `.test.ts` as CommonJS but does not
// transpile TypeScript imports. Compile the small helper in-memory so these
// regression tests run with the repository's existing `node --test` script.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");

const helperPath = path.join(__dirname, "location-propagation.ts");
const helperSource = fs.readFileSync(helperPath, "utf8");
const helperJs = ts.transpileModule(helperSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const helperModule = new Module(helperPath, module);
helperModule.filename = helperPath;
helperModule.paths = Module._nodeModulePaths(path.dirname(helperPath));
helperModule._compile(helperJs, helperPath);
const {
  extractZipOnlyValues,
  isGeneratedCommuteQuestion,
  mergeArrangementQuestionLocations,
  mergeSourceLocations,
  normalizeLocationKey,
} = helperModule.exports;

test("normalizes ZIPs and full state names for location deduplication", () => {
  assert.equal(normalizeLocationKey("Seattle, Washington 98101"), "seattle, wa");
  assert.equal(normalizeLocationKey("Seattle, WA"), "seattle, wa");
});

test("normalizeLocationKey gives bare zips distinct, non-empty keys", () => {
  // Regression: stripping the zip from a value that's JUST a zip left an
  // empty key, so mergeSourceLocations' own add() (which treats an empty
  // key as "no location") silently dropped bare-zip entries outright —
  // and worse, every bare zip collapsed onto the SAME empty key.
  assert.notEqual(normalizeLocationKey("10001"), "");
  assert.notEqual(normalizeLocationKey("94102"), "");
  assert.notEqual(normalizeLocationKey("10001"), normalizeLocationKey("94102"));
});

test("extractZipOnlyValues picks out bare 5-digit and ZIP+4 values only", () => {
  // Regression: a zip typed into Step 3's "Other Requirements" only ever
  // became a Step 4 resume-match "Requirement" filter — this is what now
  // lets it also become a Step 5 search location.
  assert.deepEqual(
    extractZipOnlyValues(["10001", "94102", "94102-1234", "  75023  "]),
    ["10001", "94102", "94102-1234", "75023"],
  );
});

test("extractZipOnlyValues excludes text that merely contains a zip", () => {
  assert.deepEqual(
    extractZipOnlyValues(["Must be within 10001", "Night shift only", "", undefined, null]),
    [],
  );
});

test("merges the job location and note locations while preserving saved radii", () => {
  const merged = mergeSourceLocations({
    jobCity: "Seattle",
    jobState: "WA",
    jobZip: "98101",
    existing: [{ id: 7, value: "Seattle, Washington", radius: "within 40 mi" }],
    noteLocations: ["New York, NY", "Seattle, WA"],
    defaultRadius: "within 25 mi",
  });

  // The saved primary entry had no zip, but one is now known — backfilled
  // in, while the saved radius is still preserved untouched.
  assert.deepEqual(merged.map(({ value, radius }) => ({ value, radius })), [
    { value: "Seattle, Washington 98101", radius: "within 40 mi" },
    { value: "New York, NY", radius: "within 25 mi" },
  ]);
});

test("backfills the job zip onto a saved zip-less primary entry", () => {
  // Regression: a zip known via jobZip used to be dropped outright whenever
  // ANY saved entry already existed for that city/state, even one saved
  // before zip-appending existed — the fix must append it, not ignore it.
  const merged = mergeSourceLocations({
    jobCity: "Austin",
    jobState: "TX",
    jobZip: "78701",
    existing: [{ id: 1, value: "Austin, TX", radius: "within 25 mi" }],
    noteLocations: [],
    defaultRadius: "within 25 mi",
  });

  assert.deepEqual(merged.map(({ value }) => value), ["Austin, TX 78701"]);
});

test("never overwrites a saved entry that already carries its own zip", () => {
  // A recruiter's own deliberate zip (or one that differs from jobZip) must
  // never be clobbered by backfill or upgrade logic.
  const merged = mergeSourceLocations({
    jobCity: "Austin",
    jobState: "TX",
    jobZip: "78701",
    existing: [{ id: 1, value: "Austin, TX 78702", radius: "within 25 mi" }],
    noteLocations: ["Austin, TX 78701"],
    defaultRadius: "within 25 mi",
  });

  assert.deepEqual(merged.map(({ value }) => value), ["Austin, TX 78702"]);
});

test("end to end: Other Requirements zips merge alongside the job's own location", () => {
  // The exact reported scenario: two zips typed into Step 3's "Other
  // Requirements" (10001, 94102) must show up in Step 5's Locations panel
  // together with the job's own auto-seeded city/state/zip, not replace it.
  const otherRequirementZips = extractZipOnlyValues(["10001", "94102"]);
  const merged = mergeSourceLocations({
    jobCity: "Plano",
    jobState: "TX",
    jobZip: "75023",
    existing: [],
    noteLocations: otherRequirementZips,
    defaultRadius: "within 25 mi",
  });

  assert.deepEqual(merged.map(({ value }) => value), ["Plano, TX 75023", "10001", "94102"]);
});

test("upgrades an already-added zip-less entry when a later note location supplies the zip", () => {
  // Regression: an AI-extracted JD location or recruiter note with a zip
  // used to be silently discarded once a zip-less entry for the same
  // city/state was already added first (e.g. the primary job-location
  // entry) — normalizeLocationKey strips zips before matching, so both
  // collide on the same key. The zip-bearing one must win instead of being
  // dropped.
  const merged = mergeSourceLocations({
    jobCity: "Austin",
    jobState: "TX",
    existing: [],
    noteLocations: ["Austin, TX 78701"],
    defaultRadius: "within 25 mi",
  });

  assert.deepEqual(merged.map(({ value }) => value), ["Austin, TX 78701"]);
});

test("does not restore a manually removed location but still adds other note locations", () => {
  const merged = mergeSourceLocations({
    jobCity: "Seattle",
    jobState: "WA",
    existing: [],
    noteLocations: ["Seattle, WA", "New York, NY"],
    defaultRadius: "within 25 mi",
    excludedLocationKeys: ["Seattle, Washington 98101"],
  });
  assert.deepEqual(merged.map(location => location.value), ["New York, NY"]);
});

test("updates one arrangement question with OR locations and keeps stable metadata", () => {
  const question = {
    id: 3,
    category: "default",
    pass_criteria: "Must be open to an onsite work arrangement",
    question_text: "This role follows an onsite work arrangement based in Seattle, WA. Are you open to working in this setup?",
  };
  const first = mergeArrangementQuestionLocations(question, ["Seattle, WA", "New York, NY"]);
  const second = mergeArrangementQuestionLocations(first, ["Seattle, WA", "New York, NY"]);
  assert.match(second.question_text, /based in Seattle, WA or New York, NY\./);
  assert.equal(second.question_text.match(/New York, NY/g)?.length, 1);
  assert.equal(second.generated_source, "work-arrangement-location");
});

test("only removes marked or exact legacy generated commute questions", () => {
  assert.equal(isGeneratedCommuteQuestion({
    id: 1,
    category: "Location",
    question_text: "Are you located in or able to commute to New York, NY?",
    pass_criteria: "Yes",
    is_hard_filter: true,
  }), true);
  assert.equal(isGeneratedCommuteQuestion({
    id: 2,
    category: "custom",
    question_text: "Are you located in or able to commute to New York, NY?",
    pass_criteria: "Yes",
    is_hard_filter: true,
  }), false);
});
