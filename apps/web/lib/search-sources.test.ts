import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_SEARCH_SOURCES,
  SEARCH_SOURCES_VERSION,
  restoreSavedSearchSources,
} from "./search-sources.ts";

// --- defaults ---------------------------------------------------------------

test("defaults: both JobDiva pools, LinkedIn and Exa on; Dice off", () => {
  assert.deepEqual(DEFAULT_SEARCH_SOURCES, {
    jobdiva_agent: true,
    jobdiva_talent: true,
    linkedin: true,
    dice: false,
    exa: true,
  });
});

test("restore with nothing saved returns the defaults", () => {
  assert.deepEqual(restoreSavedSearchSources(null, undefined), DEFAULT_SEARCH_SOURCES);
  assert.deepEqual(restoreSavedSearchSources(undefined, undefined), DEFAULT_SEARCH_SOURCES);
  assert.deepEqual(restoreSavedSearchSources("junk", undefined), DEFAULT_SEARCH_SOURCES);
  assert.deepEqual(restoreSavedSearchSources([true], undefined), DEFAULT_SEARCH_SOURCES);
});

// --- Exa re-enable migration ----------------------------------------------------

test("legacy draft (no version) with exa:false gets the new default (on)", () => {
  const out = restoreSavedSearchSources(
    { jobdiva_agent: true, jobdiva_talent: false, linkedin: true, dice: false, exa: false },
    undefined,
  );
  assert.equal(out.exa, true);
  // Every other stored flag is still honoured.
  assert.equal(out.jobdiva_talent, false);
});

test("legacy draft with a missing exa key gets the new default", () => {
  const out = restoreSavedSearchSources({ linkedin: false }, undefined);
  assert.equal(out.exa, true);
  assert.equal(out.linkedin, false);
});

test("legacy draft with an explicit exa:true opt-in stays on", () => {
  const out = restoreSavedSearchSources({ exa: true }, undefined);
  assert.equal(out.exa, true);
});

test("legacy draft keeps the caller's current exa when it is off", () => {
  // "No stored preference" means "leave the current value alone", whatever it is.
  const out = restoreSavedSearchSources(
    { exa: false },
    undefined,
    { ...DEFAULT_SEARCH_SOURCES, exa: false },
  );
  assert.equal(out.exa, false);
});

test("v2 draft with exa:false is a deliberate untick and is honoured", () => {
  const out = restoreSavedSearchSources({ exa: false }, SEARCH_SOURCES_VERSION);
  assert.equal(out.exa, false);
});

test("a future version is treated like v2 (flags honoured)", () => {
  const out = restoreSavedSearchSources({ exa: false }, SEARCH_SOURCES_VERSION + 1);
  assert.equal(out.exa, false);
});

test("a non-numeric version is treated as legacy", () => {
  const out = restoreSavedSearchSources({ exa: false }, "2");
  assert.equal(out.exa, true);
});

// --- legacy JobDiva flag + retired keys -------------------------------------

test("legacy single jobdiva flag drives both pools", () => {
  const out = restoreSavedSearchSources({ jobdiva: false }, undefined);
  assert.equal(out.jobdiva_agent, false);
  assert.equal(out.jobdiva_talent, false);
});

test("explicit per-pool flags win over the legacy jobdiva flag", () => {
  const out = restoreSavedSearchSources({ jobdiva: false, jobdiva_talent: true }, undefined);
  assert.equal(out.jobdiva_agent, false);
  assert.equal(out.jobdiva_talent, true);
});

test("retired and unknown keys are dropped", () => {
  const out = restoreSavedSearchSources(
    { jobdiva_hotlist: true, jobdiva_applicants: true, exa: true },
    SEARCH_SOURCES_VERSION,
  );
  assert.deepEqual(Object.keys(out).sort(), Object.keys(DEFAULT_SEARCH_SOURCES).sort());
});

test("non-boolean values are ignored", () => {
  const out = restoreSavedSearchSources(
    { linkedin: "no", dice: 1, exa: null },
    SEARCH_SOURCES_VERSION,
  );
  assert.equal(out.linkedin, true);
  assert.equal(out.dice, false);
  assert.equal(out.exa, true);
});

test("never mutates its inputs", () => {
  const saved = { exa: false, jobdiva: true };
  const current = { ...DEFAULT_SEARCH_SOURCES, dice: true };
  const savedSnapshot = JSON.stringify(saved);
  const currentSnapshot = JSON.stringify(current);
  restoreSavedSearchSources(saved, undefined, current);
  assert.equal(JSON.stringify(saved), savedSnapshot);
  assert.equal(JSON.stringify(current), currentSnapshot);
});
