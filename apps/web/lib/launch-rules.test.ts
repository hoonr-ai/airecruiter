/* eslint-disable @typescript-eslint/no-explicit-any -- fixture inputs are untyped JSON */
// Golden-fixture parity for lib/launch-rules.ts.
//
// The fixtures in apps/api/tests/fixtures/launch_rules/*.json hold
// {function, cases: [{name, input, expected}]}; this file replays every case
// against the TS rules, and apps/api/tests/test_launch_rules_parity.py replays
// the same cases against the Python port (services/launch_sessions/rules.py),
// so the browser and the server launch runner cannot drift apart.
//
// The TS is the source of truth. After ANY edit to launch-rules.ts (even a
// comment), regenerate the expected outputs from the TS and then port the
// change to rules.py:
//   LAUNCH_RULES_WRITE_FIXTURES=1 node --experimental-strip-types --test lib/launch-rules.test.ts
// The write also stamps launch-rules.ts's sha256 into _source.json; pytest
// fails while that stamp is stale, so CI catches a skipped regeneration even
// where this suite does not run. New cases: add {name, input} to a fixture
// file (expected: null) and rerun with the variable set.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { test } from "node:test";

import * as rules from "./launch-rules.ts";
import { isOutreachEligibleScore, OUTREACH_MIN_SCORE } from "./match-score.ts";
import { normalizePhone } from "./phone.ts";

const FIXTURE_DIR = new URL("../../api/tests/fixtures/launch_rules/", import.meta.url);
const SOURCE_STAMP = new URL("_source.json", FIXTURE_DIR);
const RULES_SOURCE = new URL("./launch-rules.ts", import.meta.url);
const PAGE_SOURCE = new URL("../app/jobs/new/page.tsx", import.meta.url);
const WRITE = process.env.LAUNCH_RULES_WRITE_FIXTURES === "1";
let writeFailed = false;

// sha256 of launch-rules.ts with CRLF folded to LF (pytest computes the same).
const rulesSha256 = () =>
  createHash("sha256").update(readFileSync(RULES_SOURCE, "utf8").replace(/\r\n/g, "\n"), "utf8").digest("hex");

type Input = Record<string, any>;

// Sets → arrays (iteration order), then a JSON round trip so `undefined`
// fields disappear exactly as they do on the wire.
function plain(value: unknown): unknown {
  const walk = (v: any): any => {
    if (v instanceof Set) return Array.from(v, walk);
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x)]));
    }
    return v;
  };
  const json = JSON.stringify(walk(value));
  return json === undefined ? null : JSON.parse(json);
}

const set = (v: unknown) => new Set<any>(Array.isArray(v) ? v : []);

// Search-stream steps: {begin: "replace" | "append"} starts an attempt,
// {event: {...}} is one NDJSON event. `accumulated` is every attempt's
// runList, as handleRunSearch concatenates them.
function runStream(steps: any[]) {
  let state = rules.initialStreamState();
  let accumulated: any[] = [];
  let started = false;
  for (const step of steps) {
    if (step.begin) {
      if (started) accumulated = [...accumulated, ...state.runList];
      state = rules.beginStreamAttempt(state, step.begin);
      started = true;
    } else {
      state = rules.applyStreamEvent(state, step.event);
    }
  }
  accumulated = [...accumulated, ...state.runList];
  return { state, accumulated };
}

const ADAPTERS: Record<string, (input: Input) => unknown> = {
  isValidLaunchEmail: (i) => rules.isValidLaunchEmail(i.value),
  launchPhoneDigits: (i) => rules.launchPhoneDigits(i.value),
  isValidLaunchPhone: (i) => rules.isValidLaunchPhone(i.value),
  normalizeDncPhone: (i) => rules.normalizeDncPhone(i.value),
  betterPhoneUI: (i) => rules.betterPhoneUI(i.current, i.incoming),
  getCandidateLaunchEmail: (i) => rules.getCandidateLaunchEmail(i.candidate),
  getCandidateLaunchPhone: (i) => rules.getCandidateLaunchPhone(i.candidate),
  isLaunchableNow: (i) => rules.isLaunchableNow(i.candidate),
  getCandidateDisplayName: (i) => rules.getCandidateDisplayName(i.candidate),
  isPlaceholderEmailUI: (i) => rules.isPlaceholderEmailUI(i.value),
  candIsJobDivaUI: (i) => rules.candIsJobDivaUI(i.candidate),
  deduplicateCandidatesUI: (i) => rules.deduplicateCandidatesUI(i.candidates),
  normalizeCompanyName: (i) => rules.normalizeCompanyName(i.value),
  extractCompanyFromHeadline: (i) => rules.extractCompanyFromHeadline(i.headline),
  getCandidateCurrentCompanies: (i) => rules.getCandidateCurrentCompanies(i.candidate),
  isEmployedByClient: (i) => rules.isEmployedByClient(i.candidate, i.job),
  getCandidateExclusionReason: (i) => rules.getCandidateExclusionReason(i.candidate),
  withClientConflictFlag: (i) => rules.withClientConflictFlag(i.candidate, i.job),
  isLaunchEligibleScore: (i) => rules.isLaunchEligibleScore(i.score),
  clampLaunchCount: (i) => rules.clampLaunchCount(i.n),
  countQualified: (i) => rules.countQualified(i.candidates),
  candidateRowId: (i) => rules.candidateRowId(i.candidate),
  chooseSelectionPool: (i) => rules.chooseSelectionPool(i.table, i.accumulated).map(rules.candidateRowId),
  computeAutoLaunchSelection: (i) =>
    rules.computeAutoLaunchSelection(i.pool, {
      launchedKeys: set(i.launchedKeys),
      launchedIds: set(i.launchedIds),
      limit: i.limit,
      ...("minScore" in i ? { minScore: i.minScore } : {}),
      ...(i.blocked ? { blocked: new Map(Object.entries(i.blocked)) } : {}),
      ...(i.withReasons ? { withReasons: true } : {}),
    }),
  launchedSetsFromResponse: (i) => {
    const { keys, ids } = rules.launchedSetsFromResponse(i.items);
    return { keys: [...keys].sort(), ids: [...ids].sort() };
  },
  computeHardFilterSkips: (i) => rules.computeHardFilterSkips(i.candidates, set(i.selectedIds)),
  selectCandidatesMissingContact: (i) =>
    rules.selectCandidatesMissingContact(i.candidates, set(i.selectedIds), set(i.hardFilterSkipIds)),
  extractLinkedInFromText: (i) => rules.extractLinkedInFromText(i.text),
  looksLikeLinkedInProfile: (i) => rules.looksLikeLinkedInProfile(i.url),
  resolveLinkedInUrl: (i) => rules.resolveLinkedInUrl(i.candidate),
  getCandidateSeedEmail: (i) => rules.getCandidateSeedEmail(i.candidate),
  getCandidateLookupName: (i) => rules.getCandidateLookupName(i.candidate),
  buildEnrichRequest: (i) => rules.buildEnrichRequest(i.candidate, i.candidateId, i.linkedinUrl, i.jobdivaId),
  interpretEnrichAnswer: (i) => rules.interpretEnrichAnswer(i.answer, i.launchableNow, i.heldEmail),
  dncDroppedIds: (i) =>
    rules.dncDroppedIds(i.candidates, set(i.selectedIds), i.overrides || {}, set(i.dncPhones)),
  splitReadyNeedsContact: (i) =>
    rules.splitReadyNeedsContact(i.candidates, {
      selectedIds: set(i.selectedIds),
      skipIds: set(i.skipIds),
      contactOverrides: i.overrides || {},
      jobdivaId: i.jobdivaId,
    }),
  mergeContactOverrides: (i) => rules.mergeContactOverrides(i.pending, i.provided),
  applyContactOverrides: (i) => rules.applyContactOverrides(i.candidates, i.overrides),
  selectSaveCandidates: (i) =>
    rules.selectSaveCandidates(i.candidates, {
      launchIds: set(i.launchIds),
      job: i.job,
      dncPhones: set(i.dncPhones),
    }),
  buildSaveCandidatePayload: (i) => rules.buildSaveCandidatePayload(i.candidate),
  buildLaunchSavePayload: (i) =>
    rules.buildLaunchSavePayload(i.candidates, {
      launchIds: set(i.launchIds),
      contactOverrides: i.overrides,
      job: i.job,
      dncPhones: set(i.dncPhones),
    }),
  mapStageToStatus: (i) => rules.mapStageToStatus(i.stage),
  mergeSourceStatus: (i) => rules.mergeSourceStatus(i.prev, i.status),
  applyStreamPatch: (i) => rules.applyStreamPatch(i.row, i.patch),
  applyStreamEvents: (i) => {
    const { state, accumulated } = runStream(i.steps);
    return { state, qualified: rules.countQualified(accumulated) };
  },
  // The server runner's S1 → S2 path: stream events into the pool, then
  // select — from the table, and from the pool handleSourceAndLaunch uses.
  streamThenSelect: (i) => {
    const { state, accumulated } = runStream(i.steps);
    const opts = { launchedKeys: set(i.launchedKeys), launchedIds: set(i.launchedIds), limit: i.limit };
    const pool = rules.chooseSelectionPool(state.candidates, accumulated);
    return {
      table: state.candidates.map(rules.candidateRowId),
      selection: rules.computeAutoLaunchSelection(state.candidates, opts),
      exclusions: state.candidates.map(rules.getCandidateExclusionReason),
      pool: pool.map(rules.candidateRowId),
      poolSelection: rules.computeAutoLaunchSelection(pool, opts),
      qualified: rules.countQualified(accumulated),
    };
  },
};

// Fixture text is kept ASCII-only (non-ASCII as \uXXXX escapes) so the
// whitespace / unicode trap cases stay visible in diffs.
const fixtureText = (fixture: unknown) =>
  JSON.stringify(fixture, null, 2).replace(
    /[^\x20-\x7e\n]/g,
    (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"),
  ) + "\n";

// "_"-prefixed files are metadata (_source.json), not fixtures.
const fixtureFiles = readdirSync(FIXTURE_DIR).filter((f) => f.endsWith(".json") && !f.startsWith("_")).sort();

test("launch-rules fixtures exist and every fixture names a known rule", () => {
  assert.ok(fixtureFiles.length > 0, "no fixtures found");
  for (const file of fixtureFiles) {
    const fixture = JSON.parse(readFileSync(new URL(file, FIXTURE_DIR), "utf8"));
    assert.ok(ADAPTERS[fixture.function], `${file}: unknown function ${fixture.function}`);
    assert.ok(Array.isArray(fixture.cases) && fixture.cases.length > 0, `${file}: no cases`);
  }
});

for (const file of fixtureFiles) {
  const url = new URL(file, FIXTURE_DIR);
  const fixture = JSON.parse(readFileSync(url, "utf8"));
  const adapter = ADAPTERS[fixture.function];
  if (!adapter) continue;
  test(`launch-rules fixture ${file}`, () => {
    for (const c of fixture.cases) {
      let actual: unknown;
      try {
        actual = plain(adapter(structuredClone(c.input)));
      } catch (e) {
        writeFailed = true;
        throw e;
      }
      if (WRITE) {
        c.expected = actual;
      } else {
        assert.deepStrictEqual(actual, c.expected, `${file} :: ${c.name}`);
      }
    }
    if (WRITE) {
      const text = fixtureText(fixture);
      if (text !== readFileSync(url, "utf8")) writeFileSync(url, text);
    }
  });
}

test("rules never mutate their inputs", () => {
  for (const file of fixtureFiles) {
    const fixture = JSON.parse(readFileSync(new URL(file, FIXTURE_DIR), "utf8"));
    const adapter = ADAPTERS[fixture.function];
    for (const c of fixture.cases) {
      const input = structuredClone(c.input);
      adapter(input);
      assert.deepStrictEqual(input, c.input, `${file} :: ${c.name} mutated its input`);
    }
  }
});

function assertInlinedCopiesAgree() {
  for (const v of [null, undefined, "", "408-555-1234", "1 (408) 555-1234", "+44 20 7946 0958", "5551234", "abc"]) {
    assert.equal(rules.normalizeDncPhone(v), normalizePhone(v));
  }
  assert.equal(rules.LAUNCH_MIN_SCORE, OUTREACH_MIN_SCORE);
  for (const v of [59.99, 60, 100, "75", true, null, Number.NaN, Number.POSITIVE_INFINITY, -1]) {
    assert.equal(rules.isLaunchEligibleScore(v), isOutreachEligibleScore(v));
  }
}

test("inlined copies agree with lib/phone and lib/match-score", assertInlinedCopiesAgree);

// The page applies search-stream events inline (React setters); the server
// runner applies them through applyStreamEvent. Both must handle the same
// event types with the same primitives, or the two paths drift.
test("page.tsx runSearchStream and applyStreamEvent handle the same events", () => {
  const page = readFileSync(PAGE_SOURCE, "utf8");
  const start = page.indexOf("const runSearchStream = async (");
  assert.ok(start >= 0, "runSearchStream not found in app/jobs/new/page.tsx");
  const end = page.indexOf("return runList;", start);
  assert.ok(end > start, "runSearchStream's `return runList;` not found");
  const pageBody = page.slice(start, end);
  const reducer = rules.applyStreamEvent.toString();
  const eventTypes = (src: string) =>
    [...new Set([...src.matchAll(/event\.type === "([a-z_]+)"/g)].map((m) => m[1]))].sort();
  assert.deepStrictEqual(eventTypes(pageBody), eventTypes(reducer));
  const primitives = Object.keys(rules).filter(
    (name) => name !== "applyStreamEvent" && typeof (rules as any)[name] === "function"
      && new RegExp(`\\b${name}\\(`).test(reducer),
  );
  assert.ok(primitives.length >= 8, `expected the reducer's primitives, found ${primitives}`);
  for (const name of primitives) {
    assert.match(pageBody, new RegExp(`\\b${name}\\(`), `runSearchStream no longer calls ${name}`);
  }
});

// A JobDiva row with an Auto_* (or no) email takes a LinkedIn row's extracted
// email on a merge: Launch still looks the email up, and a found one replaces it.
test("a merged JobDiva row never seeds another source's email", () => {
  const li = "https://www.linkedin.com/in/jane-doe";
  const [jd] = rules.deduplicateCandidatesUI([
    { candidate_id: "jd", source: "JobDiva-TalentSearch", email: "Auto_77@jobdiva.com", profile_url: li },
    { candidate_id: "li", source: "LinkedIn-Exa", email: "jane.extracted@acme.com", profile_url: li },
  ]);
  assert.equal(jd.email, "jane.extracted@acme.com");
  assert.equal(rules.getCandidateSeedEmail(jd), "");
  assert.equal(rules.buildEnrichRequest(jd, "jd", li, "26-1").email, undefined);
  const found = { status: "success", email: "jane@found.test", phone: "+15125550100" };
  assert.deepStrictEqual(rules.interpretEnrichAnswer(found, true, rules.getCandidateSeedEmail(jd)).override, {
    phone: "+15125550100", email: "jane@found.test",
  });
});

// Last: the fixtures were generated from THIS launch-rules.ts.
test("fixtures are stamped with this launch-rules.ts", () => {
  const sha256 = rulesSha256();
  if (WRITE) {
    assertInlinedCopiesAgree();
    assert.ok(!writeFailed, "a fixture failed to regenerate; _source.json left unchanged");
    const stamp = {
      source: "apps/web/lib/launch-rules.ts",
      sha256,
      regenerate: "cd apps/web && LAUNCH_RULES_WRITE_FIXTURES=1 node --experimental-strip-types --test lib/launch-rules.test.ts",
    };
    writeFileSync(SOURCE_STAMP, JSON.stringify(stamp, null, 2) + "\n");
    return;
  }
  const stamp = JSON.parse(readFileSync(SOURCE_STAMP, "utf8"));
  assert.equal(stamp.sha256, sha256, "launch-rules.ts changed since the fixtures were generated: regenerate them");
});
