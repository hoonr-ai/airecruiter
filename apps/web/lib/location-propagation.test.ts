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
  isGeneratedCommuteQuestion,
  mergeArrangementQuestionLocations,
  mergeSourceLocations,
  normalizeLocationKey,
} = helperModule.exports;

test("normalizes ZIPs and full state names for location deduplication", () => {
  assert.equal(normalizeLocationKey("Seattle, Washington 98101"), "seattle, wa");
  assert.equal(normalizeLocationKey("Seattle, WA"), "seattle, wa");
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

  assert.deepEqual(merged.map(({ value, radius }) => ({ value, radius })), [
    { value: "Seattle, Washington", radius: "within 40 mi" },
    { value: "New York, NY", radius: "within 25 mi" },
  ]);
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
