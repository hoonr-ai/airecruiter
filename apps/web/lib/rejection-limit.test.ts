// Node 20's built-in test runner loads `.test.ts` as CommonJS but does not
// transpile TypeScript imports. Compile the small helper in-memory so these
// regression tests run with the repository's existing `node --test` script
// (same pattern as location-propagation.test.ts).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");

const helperPath = path.join(__dirname, "rejection-limit.ts");
const helperSource = fs.readFileSync(helperPath, "utf8");
const helperJs = ts.transpileModule(helperSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const helperModule = new Module(helperPath, module);
helperModule.filename = helperPath;
helperModule.paths = Module._nodeModulePaths(path.dirname(helperPath));
helperModule._compile(helperJs, helperPath);
const { MAX_REJECT_REASON_CHANGES, getRemainingRejectChanges, safeRejectReasonChangeCount } = helperModule.exports;

// This repo's test runner (package.json's "test" script) only globs
// lib/*.test.ts and hooks/*.test.ts — there is no component/DOM test
// infrastructure (no jsdom, no @testing-library) to render
// ChangeRejectionReasonButton or RejectChangeLimitNotice directly against.
// Both components only ever derive their count/remaining/limitReached
// display from this module, so exhaustively covering the 0, 1, 2 (and
// beyond/invalid) states here is this repo's equivalent of a component test
// for those states (PR #876 review).

test("MAX_REJECT_REASON_CHANGES matches the backend's own limit", () => {
  assert.equal(MAX_REJECT_REASON_CHANGES, 2);
});

test("getRemainingRejectChanges at count 0: 2 remaining, not at the limit", () => {
  const result = getRemainingRejectChanges(0);
  assert.deepEqual(result, { changeCount: 0, maxChanges: 2, remaining: 2, limitReached: false });
});

test("getRemainingRejectChanges at count 1: 1 remaining, not at the limit", () => {
  const result = getRemainingRejectChanges(1);
  assert.deepEqual(result, { changeCount: 1, maxChanges: 2, remaining: 1, limitReached: false });
});

test("getRemainingRejectChanges at count 2: 0 remaining, at the limit", () => {
  const result = getRemainingRejectChanges(2);
  assert.deepEqual(result, { changeCount: 2, maxChanges: 2, remaining: 0, limitReached: true });
});

test("getRemainingRejectChanges never reports negative remaining past the limit", () => {
  // Defensive: the backend should never let the count exceed the limit, but
  // a stale/corrupt value must not make the UI show "-1 remaining".
  const result = getRemainingRejectChanges(5);
  assert.equal(result.remaining, 0);
  assert.equal(result.limitReached, true);
});

test("safeRejectReasonChangeCount guards against NaN and other invalid values", () => {
  // PR #876 review (low): Number(candidate?.reject_reason_change_count) on a
  // non-numeric API value yields NaN, and Math.max(0, NaN - x) is NaN —
  // every caller must be guarded against this, not just the ones that
  // happened to use Number(...) directly.
  assert.equal(safeRejectReasonChangeCount(undefined), 0);
  assert.equal(safeRejectReasonChangeCount(null), 0);
  assert.equal(safeRejectReasonChangeCount(NaN), 0);
  assert.equal(safeRejectReasonChangeCount("not a number"), 0);
  assert.equal(safeRejectReasonChangeCount(-1), 0);
  assert.equal(safeRejectReasonChangeCount(1.9), 1);
});

test("getRemainingRejectChanges never produces NaN from an invalid changeCount", () => {
  const result = getRemainingRejectChanges(undefined);
  assert.equal(Number.isNaN(result.changeCount), false);
  assert.equal(Number.isNaN(result.remaining), false);
  assert.deepEqual(result, { changeCount: 0, maxChanges: 2, remaining: 2, limitReached: false });
});
