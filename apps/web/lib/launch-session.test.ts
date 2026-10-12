/* eslint-disable @typescript-eslint/no-explicit-any -- session fixtures are untyped JSON */
// Unit tests for lib/launch-session.ts — the browser rules of the
// server-driven Step-5 Source & Launch (request bodies, session → UI, redirect,
// CSV rows, back-off, keyset merge, create / attach answers).
//   node --experimental-strip-types --test lib/launch-session.test.ts
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import * as ls from "./launch-session.ts";
import { clampLaunchCount, deduplicateCandidatesUI, getCandidateDisplayName } from "./launch-rules.ts";

const PAGE_SOURCE = new URL("../app/jobs/new/page.tsx", import.meta.url);

const view = (over: Partial<ls.LaunchSessionView> = {}): ls.LaunchSessionView => ({
  id: "s-1",
  job_id: "J1",
  mode: "full_search",
  status: "running",
  stage: "sourcing",
  terminal: false,
  counts: {},
  sourcing: { attempts_total: 3, current_attempt: 0, ui: {} },
  timestamps: {},
  cand_rev: 0,
  ...over,
});

const row = (id: string, over: Partial<ls.LaunchSessionRow> = {}): ls.LaunchSessionRow => ({
  candidate_id: id,
  state: "pooled",
  reason: null,
  ord: 0,
  rev: 1,
  row: { candidate_id: id, name: `Person ${id}`, source: "JobDiva-TalentSearch" },
  contact: null,
  ...over,
});

const cacheOf = (rows: ls.LaunchSessionRow[]): ls.RowCache =>
  ls.mergeKeysetPage(ls.emptyRowCache(), {
    rows,
    total: rows.length,
    cand_rev: Math.max(0, ...rows.map((r) => Number(r.rev ?? 0))),
    has_more: false,
    next: { after_rev: 9, after_id: "z" },
  });

// ── The relaxation chain ────────────────────────────────────────────────────

// handleRunSearch's loop (page.tsx), restated: relax the PREVIOUS query by
// tier, stop on no change, overrides from the tier only.
function legacyChain(initial: string, max: number, relax: (q: string, t: number) => { query: string; label: string }) {
  const attempts = [{ query: initial, label: "PAIR generated", tier: 0 }];
  while (attempts.length < max) {
    const tier = attempts.length;
    const relaxed = relax(attempts[attempts.length - 1].query, tier);
    if (relaxed.query === attempts[attempts.length - 1].query) break;
    attempts.push({ query: relaxed.query, label: relaxed.label, tier });
  }
  return attempts;
}

describe("buildSearchAttemptChain", () => {
  const dropLastAnd = (q: string, tier: number) => {
    const parts = q.split(" AND ");
    return { query: parts.length > 1 ? parts.slice(0, -1).join(" AND ") : q, label: `tier ${tier}` };
  };

  test("relaxes the previous attempt's query, tier = attempt index, until no change", () => {
    const seen: Array<[string, number]> = [];
    const chain = ls.buildSearchAttemptChain({
      initialQuery: "a AND b AND c AND d AND e",
      maxAttempts: 4,
      relax: (q, t) => {
        seen.push([q, t]);
        return dropLastAnd(q, t);
      },
      overridesFor: (tier) => ({ tier }),
      buildPayload: (query, overrides) => ({ boolean_string: query, overrides: overrides ?? null }),
    });
    assert.deepEqual(seen, [["a AND b AND c AND d AND e", 1], ["a AND b AND c AND d", 2], ["a AND b AND c", 3]]);
    assert.deepEqual(chain.map((a) => a.query), ["a AND b AND c AND d AND e", "a AND b AND c AND d", "a AND b AND c", "a AND b"]);
    assert.deepEqual(chain.map((a) => a.payload.overrides), [null, { tier: 1 }, { tier: 2 }, { tier: 3 }]);
    assert.equal(chain[0].label, "PAIR generated");
    assert.equal(chain[2].label, "tier 2");
  });

  test("stops when a tier changes nothing (no duplicate attempt)", () => {
    const chain = ls.buildSearchAttemptChain({
      initialQuery: "a AND b",
      maxAttempts: 4,
      relax: dropLastAnd,
      overridesFor: () => ({}),
      buildPayload: (q) => q,
    });
    assert.deepEqual(chain.map((a) => a.query), ["a AND b", "a"]);
  });

  test("matches handleRunSearch's loop for many inputs", () => {
    const relaxes = [
      dropLastAnd,
      (q: string, t: number) => ({ query: t === 2 ? q : `${q}~${t}`, label: `x${t}` }),
      (q: string) => ({ query: q.replace(/ OR [^ ]+$/, ""), label: "or" }),
    ];
    const inputs = ["", "solo", "a AND b AND c AND d AND e AND f", "x OR y OR z", "a AND b"];
    for (const relax of relaxes) {
      for (const input of inputs) {
        for (const max of [1, 2, 4]) {
          const ours = ls.buildSearchAttemptChain({
            initialQuery: input,
            maxAttempts: max,
            relax,
            overridesFor: (t) => t,
            buildPayload: (q, o) => ({ q, o }),
          });
          const theirs = legacyChain(input, max, relax);
          assert.deepEqual(
            ours.map((a) => [a.query, a.label, a.payload.o ?? 0]),
            theirs.map((a) => [a.query, a.label, a.tier]),
            `${input} / max ${max}`,
          );
        }
      }
    }
  });

  test("page.tsx: the server snapshot uses the same relaxation inputs as handleRunSearch", () => {
    const page = readFileSync(PAGE_SOURCE, "utf8");
    const loopStart = page.indexOf("const handleRunSearch = async ()");
    assert.ok(loopStart > 0, "handleRunSearch not found");
    const loop = page.slice(loopStart, page.indexOf("return accumulated;", loopStart));
    assert.match(loop, /relaxBooleanString\(currentAttempts\[currentAttempts\.length - 1\]\.query, tier\)/);
    assert.match(loop, /if \(relaxed\.query === currentAttempts\[currentAttempts\.length - 1\]\.query\) break;/);
    assert.match(loop, /relaxStructuralOverrides\(\s*tier,\s*resumeMatchFilters,\s*sourceTitles,\s*sourceSkills,\s*sourceCompanies\s*\)/);
    assert.match(loop, /while \(currentAttempts\.length < MAX_BOOLEAN_ATTEMPTS\)/);
    const srvStart = page.indexOf("const runServerSourceAndLaunch = async");
    assert.ok(srvStart > 0, "runServerSourceAndLaunch not found");
    const srv = page.slice(srvStart, page.indexOf("// QA review (awaiting_input)", srvStart));
    assert.match(srv, /maxAttempts: MAX_BOOLEAN_ATTEMPTS/);
    assert.match(srv, /relax: relaxBooleanString/);
    assert.match(srv, /relaxStructuralOverrides\(tier, resumeMatchFilters, sourceTitles, sourceSkills, sourceCompanies\)/);
    assert.match(srv, /buildPayload: buildSearchPayload/);
    assert.match(srv, /initialQuery: initial/);
    assert.match(srv, /const initial = resolvedGeneratedBoolean;/);
  });
});

// ── Create bodies ───────────────────────────────────────────────────────────

describe("create bodies", () => {
  test("launch options = runLaunchPair's flags", () => {
    assert.deepEqual(ls.buildLaunchOptions({ wizardMode: "edit", jobBoardIds: ["b1"], batchSize: 75 }), {
      is_initial_launch: true, notify_recruiters: true, send_job_posting_email: true, job_board_ids: ["b1"], batch_size: 75,
    });
    assert.deepEqual(ls.buildLaunchOptions({ wizardMode: "source", jobBoardIds: [], batchSize: 75 }), {
      is_initial_launch: false, notify_recruiters: true, send_job_posting_email: false, job_board_ids: [], batch_size: 75,
    });
    assert.deepEqual(ls.buildLaunchOptions({ wizardMode: "edit", isRetry: true, jobBoardIds: ["b1"], batchSize: 75 }), {
      is_initial_launch: false, notify_recruiters: false, send_job_posting_email: false, job_board_ids: ["b1"], batch_size: 75,
    });
  });

  test("clampCount agrees with lib/launch-rules clampLaunchCount", () => {
    for (const v of [-5, 0, 0.4, 1, 1.9, 2, 249, 250, 999.99, 1000, 1001, 5000, NaN]) {
      assert.equal(ls.clampCount(v), clampLaunchCount(Number.isNaN(v) ? 0 : v), String(v));
    }
  });

  test("full-search body", () => {
    const options = ls.buildLaunchOptions({ wizardMode: "edit", jobBoardIds: [], batchSize: 75 });
    const body = ls.buildFullSearchCreateBody({
      idempotencyKey: "k1",
      attempts: [{ label: "PAIR generated", query: "q", payload: { boolean_string: "q" } }],
      launchCount: 5000,
      minScore: 60,
      qualifiedTarget: 50,
      requireReview: true,
      options,
      appBaseUrl: "https://pairqa.example",
    });
    assert.deepEqual(body, {
      idempotency_key: "k1",
      mode: "full_search",
      trigger: "button",
      search_attempts: [{ label: "PAIR generated", payload: { boolean_string: "q" } }],
      min_score: 60,
      qualified_target: 50,
      launch_count: 1000,
      require_review: true,
      options,
      app_base_url: "https://pairqa.example",
    });
  });

  test("provided body: copy from a session, or upload with the first chunk", () => {
    const options = ls.buildLaunchOptions({ wizardMode: "source", jobBoardIds: [], batchSize: 75 });
    const common = { idempotencyKey: "k", trigger: "launch_remaining" as const, ids: ["a", "b"], requireReview: false, options, appBaseUrl: "" };
    const copy = ls.buildProvidedCreateBody({ ...common, fromSessionId: "s-9" });
    assert.equal(copy.mode, "provided");
    assert.equal(copy.from_session_id, "s-9");
    assert.deepEqual(copy.candidate_ids, ["a", "b"]);
    assert.equal(copy.candidates, undefined);
    assert.equal(copy.min_score, null);
    assert.equal(copy.launch_count, 2);
    const upload = ls.buildProvidedCreateBody({ ...common, trigger: "qa_quick", firstChunk: [{ candidate_id: "a" }], startOnCreate: true });
    assert.deepEqual(upload.candidates, [{ candidate_id: "a" }]);
    assert.equal(upload.start, true);
    assert.equal(upload.trigger, "qa_quick");
    assert.equal(upload.from_session_id, undefined);
  });

  test("upload plan and server-side copy rule", () => {
    const rows = Array.from({ length: 60 }, (_, i) => i);
    const plan = ls.planProvidedUpload(rows, 25);
    assert.equal(plan.first.length, 25);
    assert.deepEqual(plan.rest.map((p) => p.length), [25, 10]);
    assert.equal(plan.startOnCreate, false);
    assert.equal(ls.planProvidedUpload([1, 2], 25).startOnCreate, true);
    assert.equal(ls.planProvidedUpload([], 25).startOnCreate, true);
    assert.equal(ls.canCopyFromSession(["a", "b"], new Set(["a", "b", "c"])), true);
    assert.equal(ls.canCopyFromSession(["a", "x"], new Set(["a", "b"])), false);
    assert.equal(ls.canCopyFromSession([], new Set(["a"])), false);
    assert.equal(ls.canCopyFromSession(["a"], null), false);
  });

  test("idempotency key", () => {
    assert.equal(ls.newIdempotencyKey({ randomUUID: () => "uuid-1" }), "uuid-1");
    const a = ls.newIdempotencyKey(null);
    const b = ls.newIdempotencyKey({});
    assert.match(a, /^ls-/);
    assert.notEqual(a, b);
  });
});

// ── Reading answers ─────────────────────────────────────────────────────────

describe("classifyCreateResponse (create / attach / 409 handling)", () => {
  const s = view();
  test("201 created / 200 replayed", () => {
    assert.deepEqual(ls.classifyCreateResponse({ ok: true, status: 201, data: { session: s } }), { kind: "created", session: s });
    assert.deepEqual(ls.classifyCreateResponse({ ok: true, status: 200, data: { session: s } }), { kind: "replayed", session: s });
    assert.equal(ls.classifyCreateResponse({ ok: true, status: 200, data: { session: null, skipped: 2 } }).kind, "error");
  });
  test("409 another active session → attach", () => {
    assert.deepEqual(
      ls.classifyCreateResponse({ ok: false, status: 409, body: { detail: { code: "active_session", active_session_id: "s-2", status: "running" } } }),
      { kind: "active_session", activeSessionId: "s-2", status: "running" },
    );
  });
  test("409 outreach stopped / key in use; 400 email gate; 503 disabled", () => {
    const stopped = ls.classifyCreateResponse({ ok: false, status: 409, body: { detail: { code: "outreach_stopped", message: "Job activity has been stopped. Cannot launch new candidates." } } });
    assert.deepEqual(stopped, { kind: "outreach_stopped", message: "Job activity has been stopped. Cannot launch new candidates." });
    assert.equal(ls.classifyCreateResponse({ ok: false, status: 409, body: { detail: { code: "idempotency_key_in_use" } } }).kind, "error");
    assert.deepEqual(
      ls.classifyCreateResponse({ ok: false, status: 409, body: { detail: { code: "parent_active", status: "running" } } }),
      { kind: "error", status: 409, message: "that launch is still running — try again once it has finished" },
    );
    assert.deepEqual(
      ls.classifyCreateResponse({ ok: false, status: 422, body: { detail: { code: "job_mismatch", message: "search_attempts[0].payload.job_id is not this job" } } }),
      { kind: "error", status: 422, message: "search_attempts[0].payload.job_id is not this job" },
    );
    assert.deepEqual(
      ls.classifyCreateResponse({ ok: false, status: 400, body: { detail: "This job has no recruiter email assigned. Add one before launching." } }),
      { kind: "email_gate", message: "This job has no recruiter email assigned. Add one before launching." },
    );
    assert.deepEqual(ls.classifyCreateResponse({ ok: false, status: 503, body: { detail: { code: "disabled", message: "off" } } }), { kind: "disabled" });
  });
  test("retryable: 429 (Retry-After), 5xx (incl. the gate's 503), network", () => {
    assert.deepEqual(ls.classifyCreateResponse({ ok: false, status: 429, body: null, retryAfterMs: 2000 }), { kind: "transient", status: 429, retryAfterMs: 2000 });
    assert.equal(ls.classifyCreateResponse({ ok: false, status: 503, body: { detail: "Could not verify the job's recruiter assignment. Please try again." } }).kind, "transient");
    assert.equal(ls.classifyCreateResponse({ ok: false, status: 502, body: "<html>" }).kind, "transient");
    assert.equal(ls.classifyCreateResponse({ ok: false, status: 0, network: true }).kind, "transient");
  });
  test("other 4xx: an error with the server's message", () => {
    assert.deepEqual(ls.classifyCreateResponse({ ok: false, status: 404, body: { detail: "Job not found." } }), { kind: "error", status: 404, message: "Job not found." });
    assert.deepEqual(
      ls.classifyCreateResponse({ ok: false, status: 422, body: { detail: [{ msg: "field required", loc: ["body", "mode"] }] } }),
      { kind: "error", status: 422, message: "field required" },
    );
    assert.equal(ls.classifyCreateResponse({ ok: false, status: 403, body: {} }).kind, "error");
  });
  test("errorMessageOf", () => {
    assert.equal(ls.errorMessageOf({ detail: "x" }), "x");
    assert.equal(ls.errorMessageOf({ detail: { message: "m", code: "c" } }), "m");
    assert.equal(ls.errorMessageOf({ detail: { code: "not_collecting" } }), "not_collecting");
    assert.equal(ls.errorMessageOf({ message: "y" }), "y");
    assert.equal(ls.errorMessageOf(null, "fb"), "fb");
    assert.equal(ls.errorMessageOf("plain text"), "plain text");
  });
  test("transient statuses", () => {
    for (const st of [0, 408, 429, 500, 502, 503]) assert.equal(ls.isTransientStatus(st), true, String(st));
    for (const st of [400, 401, 403, 404, 409, 422]) assert.equal(ls.isTransientStatus(st), false, String(st));
  });
});

describe("back-off", () => {
  test("Retry-After: seconds, HTTP date, junk", () => {
    assert.equal(ls.retryAfterMs("3"), 3000);
    assert.equal(ls.retryAfterMs("0"), 0);
    assert.equal(ls.retryAfterMs(new Date(10_000).toUTCString(), 4_000), 6000);
    assert.equal(ls.retryAfterMs("soon"), null);
    assert.equal(ls.retryAfterMs(null), null);
    assert.equal(ls.retryAfterMs(""), null);
  });
  test("write retry delay doubles, Retry-After wins when longer", () => {
    assert.deepEqual([1, 2, 3, 4, 5, 6].map((a) => ls.writeRetryDelay(a, null)), [800, 1600, 3200, 6400, 12800, 15000]);
    assert.equal(ls.writeRetryDelay(1, 5000), 5000);
    assert.equal(ls.writeRetryDelay(4, 1000), 6400);
    assert.equal(ls.writeRetryDelay(1, 999_999), 60000);
  });
  test("poll delay: poll_ms, exponential to 15 s on errors, 15 s hidden", () => {
    const base = { baseMs: 3000, hiddenMs: 15000, hidden: false };
    assert.deepEqual([0, 1, 2, 3, 4, 10].map((errors) => ls.nextPollDelay({ ...base, errors })), [3000, 6000, 12000, 15000, 15000, 15000]);
    assert.equal(ls.nextPollDelay({ ...base, hidden: true, errors: 0 }), 15000);
    assert.equal(ls.nextPollDelay({ ...base, hidden: true, errors: 3 }), 15000);
    assert.equal(ls.nextPollDelay({ baseMs: 10, hiddenMs: 15000, hidden: false, errors: 0 }), 500);
    assert.equal(ls.nextPollDelay({ baseMs: NaN, hiddenMs: NaN, hidden: true, errors: 0 }), 15000);
  });
});

describe("config, reattach, hint", () => {
  test("normalizeConfig", () => {
    assert.equal(ls.normalizeConfig(null), null);
    assert.equal(ls.normalizeConfig({ poll_ms: 3000 }), null);
    assert.deepEqual(ls.normalizeConfig({ enabled: true, poll_ms: 2000, hidden_poll_ms: 15000, max_upload_chunk: 50, max_candidates: 1000, require_review_default: false }), {
      enabled: true, poll_ms: 2000, hidden_poll_ms: 15000, max_upload_chunk: 50, max_candidates: 1000, require_review_default: false,
    });
    assert.deepEqual(ls.normalizeConfig({ enabled: false, poll_ms: "junk" }), {
      enabled: false, poll_ms: 3000, hidden_poll_ms: 15000, max_upload_chunk: 50, max_candidates: 1000, require_review_default: false,
    });
  });
  test("config answers: blips are asked again, explicit answers are final", () => {
    const cfg = { enabled: true, poll_ms: 3000, hidden_poll_ms: 15000, max_upload_chunk: 50, max_candidates: 1000, require_review_default: false };
    assert.deepEqual(ls.classifyConfigResponse({ ok: true, status: 200, data: cfg }), { config: cfg, transient: false });
    assert.deepEqual(ls.classifyConfigResponse({ ok: true, status: 200, data: "<html>" }), { config: null, transient: false });
    for (const status of [0, 401, 408, 429, 500, 502, 503]) {
      assert.equal(ls.classifyConfigResponse({ ok: false, status }).transient, true, `HTTP ${status}`);
    }
    assert.equal(ls.classifyConfigResponse({ ok: false, status: 0, network: true }).transient, true);
    for (const status of [403, 404, 422]) {
      assert.equal(ls.classifyConfigResponse({ ok: false, status }).transient, false, `HTTP ${status}`);
    }
    assert.deepEqual([1, 2, 3, 4, 5, 6, 9].map((a) => ls.lookupRetryDelay(a)), [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
  });
  test("planReattach: only the user's own session is followed; a teammate's is offered", () => {
    const me = "Ann@Example.org";
    const mine = view({ id: "m", status: "running", requested_by: "ann@example.org" });
    const theirs = view({ id: "t", status: "running", requested_by: "bo@example.org" });
    const mineReview = view({ id: "r", status: "awaiting_input", requested_by: "ann@example.org" });
    const done = view({ id: "c", status: "completed", terminal: true, requested_by: "ann@example.org" });
    // Hint first, else the user's newest (list order = newest first).
    assert.equal(ls.planReattach([mine, mineReview], { hintId: "r", me }).follow?.id, "r");
    assert.equal(ls.planReattach([mine, mineReview], { hintId: "zzz", me }).follow?.id, "m");
    // A teammate's running launch is never taken over: banner only.
    const p = ls.planReattach([theirs], { me });
    assert.deepEqual([p.follow, p.other?.id], [null, "t"]);
    assert.equal(ls.planReattach([theirs, mine], { me }).follow?.id, "m");
    assert.equal(ls.planReattach([theirs, mine], { me }).other, null);
    // An unknown user owns nothing; the tab's own hint still wins.
    assert.equal(ls.planReattach([mine], { me: "" }).follow, null);
    assert.equal(ls.planReattach([theirs], { hintId: "t", me }).follow?.id, "t", "this tab was watching it before the reload");
    assert.deepEqual(ls.planReattach([done], { hintId: "c", me }), { follow: null, interrupted: null, other: null, parked: null });
    assert.deepEqual(ls.planReattach(undefined), { follow: null, interrupted: null, other: null, parked: null });
  });
  test("planReattach: launches that are over, interrupted uploads, parked rows", () => {
    const me = "ann@example.org";
    const provisioning = view({ id: "p", status: "running", stage: "provisioning", launch_complete: true, requested_by: me, needs_contact_count: 2 });
    // This tab's launch ended during the reload and its outcome never ran here: follow it once.
    assert.equal(ls.planReattach([provisioning], { hintId: "p", me, outcomeHandled: () => false }).follow?.id, "p");
    // Outcome already handled: not followed again; its parked rows are offered.
    const handled = ls.planReattach([provisioning], { hintId: "p", me, outcomeHandled: () => true });
    assert.deepEqual([handled.follow, handled.parked?.id], [null, "p"]);
    // No hint: a launch that is over is never followed, only offered when it is the user's.
    assert.deepEqual([ls.planReattach([provisioning], { me }).follow, ls.planReattach([provisioning], { me }).parked?.id], [null, "p"]);
    assert.equal(ls.planReattach([provisioning], { me: "bo@example.org" }).parked, null);
    assert.equal(ls.planReattach([{ ...provisioning, needs_contact_count: 0 }], { me }).parked, null);
    // The tab's own upload, cut off by the reload: cancelled, not followed.
    const collecting = view({ id: "u", status: "collecting", stage: "created", mode: "provided", requested_by: me });
    const cut = ls.planReattach([collecting], { hintId: "u", me });
    assert.deepEqual([cut.interrupted?.id, cut.follow], ["u", null]);
    // ...unless this page is uploading into it right now.
    assert.equal(ls.planReattach([collecting], { hintId: "u", me, uploadingIds: new Set(["u"]) }).follow?.id, "u");
    // Another tab's upload of the user's (no hint): followed, never cancelled.
    assert.deepEqual([ls.planReattach([collecting], { me }).follow?.id, ls.planReattach([collecting], { me }).interrupted], ["u", null]);
  });
  test("pickParkedOffer: the user's own COMPLETED launch, within 24 h", () => {
    const me = "ann@example.org";
    const now = Date.parse("2026-10-11T12:00:00Z");
    const s = (over: Partial<ls.LaunchSessionView>) =>
      view({ status: "completed", terminal: true, requested_by: me, needs_contact_count: 3, timestamps: { completed_at: "2026-10-11T09:00:00Z" }, ...over });
    assert.equal(ls.pickParkedOffer([s({ id: "a" })], me, now)?.id, "a");
    assert.equal(ls.pickParkedOffer([s({ id: "a", status: "cancelled" }), s({ id: "b" })], me, now)?.id, "b", "a cancelled launch never offers its parked rows");
    assert.equal(ls.pickParkedOffer([s({ id: "a", status: "failed" })], me, now), null);
    assert.equal(ls.pickParkedOffer([s({ id: "a", requested_by: "bo@example.org" })], me, now), null);
    assert.equal(ls.pickParkedOffer([s({ id: "a", timestamps: { completed_at: "2026-10-10T11:00:00Z" } })], me, now), null, "older than 24 h");
    assert.equal(ls.pickParkedOffer([s({ id: "a", needs_contact_count: 0 })], me, now), null);
    assert.equal(ls.pickParkedOffer([s({ id: "a", timestamps: {} })], me, now), null);
    assert.equal(ls.pickParkedOffer(null, me, now), null);
  });
  test("isRequestedBy", () => {
    assert.equal(ls.isRequestedBy({ requested_by: "ann@example.org" }, " ANN@example.org "), true);
    assert.equal(ls.isRequestedBy({ requested_by: "ann@example.org" }, "bo@example.org"), false);
    assert.equal(ls.isRequestedBy({ requested_by: "" }, ""), false);
    assert.equal(ls.isRequestedBy(null, "ann@example.org"), false);
  });
  test("hint round trip", () => {
    const h = { id: "s", attempts: [{ query: "q1", label: "PAIR generated" }, { query: "q2", label: "relaxed" }] };
    assert.deepEqual(ls.decodeSessionHint(ls.encodeSessionHint(h)), h);
    assert.equal(ls.decodeSessionHint("{bad"), null);
    assert.equal(ls.decodeSessionHint(JSON.stringify({ attempts: [] })), null);
    assert.deepEqual(ls.decodeSessionHint(JSON.stringify({ id: "x", attempts: [{ query: 1 }, { query: "ok" }] })), { id: "x", attempts: [{ query: "ok", label: "" }] });
    assert.equal(ls.sessionHintKey("26-1"), "pair:launch-session:26-1");
    assert.deepEqual(ls.decodeSessionHint(ls.encodeSessionHint({ id: "o", attempts: [], own: true })), { id: "o", attempts: [], own: true });
    assert.equal(ls.decodeSessionHint(JSON.stringify({ id: "o", own: "yes" }))?.own, undefined);
  });
});

// ── Keyset rows ─────────────────────────────────────────────────────────────

describe("mergeKeysetPage", () => {
  test("upserts by id, keeps the newer rev, moves the cursor", () => {
    let cache = ls.emptyRowCache();
    assert.equal(ls.rowsAreStale(cache, { cand_rev: 0 }), true);
    cache = ls.mergeKeysetPage(cache, { rows: [row("a", { rev: 1 }), row("b", { rev: 2 })], total: 2, cand_rev: 2, has_more: false, next: { after_rev: 2, after_id: "b" } });
    assert.deepEqual(Object.keys(cache.rows).sort(), ["a", "b"]);
    assert.deepEqual(cache.cursor, { after_rev: 2, after_id: "b" });
    assert.equal(cache.version, 1);
    assert.equal(cache.candRev, 2);
    assert.equal(ls.rowsAreStale(cache, { cand_rev: 2 }), false);
    assert.equal(ls.rowsAreStale(cache, { cand_rev: 3 }), true);

    const older = ls.mergeKeysetPage(cache, { rows: [row("a", { rev: 0, state: "dropped" })], total: 1, cand_rev: 2, has_more: false, next: { after_rev: 2, after_id: "b" } });
    assert.equal(older, cache, "an older copy changes nothing");

    const newer = ls.mergeKeysetPage(cache, { rows: [row("a", { rev: 3, state: "selected" })], total: 1, cand_rev: 3, has_more: false, next: { after_rev: 3, after_id: "a" } });
    assert.equal(newer.rows.a.state, "selected");
    assert.equal(newer.version, 2);
    assert.equal(cache.rows.a.state, "pooled", "the old cache is not mutated");
  });
  test("a partial read (has_more) does not mark the session's cand_rev as seen", () => {
    const partial = ls.mergeKeysetPage(ls.emptyRowCache(), {
      rows: [row("a", { rev: 2 })], total: 5, cand_rev: 9, has_more: true, next: { after_rev: 2, after_id: "a" },
    });
    assert.equal(partial.candRev, 2);
    assert.equal(ls.rowsAreStale(partial, { cand_rev: 9 }), true);
  });
  test("keyset positions: the cursor just before each row (one row's full record)", () => {
    const page = { rows: [row("b", { rev: 4 }), row("c", { rev: 4 }), row("a", { rev: 6 })] };
    assert.deepEqual(ls.keysetBeforeCursors({ after_rev: 3, after_id: "z" }, page), {
      b: { after_rev: 3, after_id: "z" },
      c: { after_rev: 4, after_id: "b" },
      a: { after_rev: 4, after_id: "c" },
    });
    assert.deepEqual(ls.keysetBeforeCursors({ after_rev: 0, after_id: "" }, null), {});
    assert.equal(ls.resumeTextOfRow({ resume_text: "R1" }), "R1");
    assert.equal(ls.resumeTextOfRow({ resumeText: "R2" }), "R2");
    assert.equal(ls.resumeTextOfRow({ data: { resume_text: "R3" } }), "R3");
    assert.equal(ls.resumeTextOfRow(null), "");
  });
  test("an empty page that only advances cand_rev keeps the version", () => {
    const cache = cacheOf([row("a", { rev: 1 })]);
    const next = ls.mergeKeysetPage(cache, { rows: [], total: 0, cand_rev: 7, has_more: false, next: { after_rev: 9, after_id: "z" } });
    assert.equal(next.version, cache.version);
    assert.equal(next.candRev, 7);
    assert.equal(ls.mergeKeysetPage(cache, null), cache);
  });
});

describe("tableCandidatesFromRows", () => {
  test("pool order, hidden rows dropped, contacts applied, browser dedupe injected", () => {
    const cache = cacheOf([
      row("c", { ord: 2 }),
      row("a", { ord: 0, contact: { override: { phone: "+1 555 000 1111" } } }),
      row("b", { ord: 1, state: "dropped", reason: "merged:a" }),
      row("d", { ord: 3, state: "skipped", reason: "identity_duplicate:a" }),
      row("e", { ord: 4, state: "handed_off" }),
      row("f", { ord: 5, state: "launched", reason: "sent", contact: { final: { phone: "555-222-3333", email: "" } } }),
      row("g", { ord: 6, contact: { override: { phone: "1", email: "o@x.com" }, provided: { phone: "999 888 7777", email: "" } } }),
    ]);
    const seen: any[][] = [];
    const table = ls.tableCandidatesFromRows(cache, (list) => {
      seen.push(list);
      return list;
    });
    assert.deepEqual(table.map((r) => r.candidate_id), ["a", "c", "f", "g"]);
    assert.equal(table[0].phone, "+1 555 000 1111");
    assert.equal(table[2].phone, "555-222-3333");
    assert.equal(table[2].email, "", "final contacts replace both channels");
    assert.equal(table[3].phone, "999 888 7777", "provided wins over the lookup");
    assert.equal(table[3].email, "o@x.com", "an empty provided field keeps the lookup's");
    assert.equal(seen.length, 0, "no shared identity key: the O(n²) browser de-dup is skipped");
    const dup = cacheOf([
      row("a", { ord: 0, row: { candidate_id: "a", email: "same@x.com" } }),
      row("b", { ord: 1, row: { candidate_id: "b", email: "SAME@x.com " } }),
    ]);
    ls.tableCandidatesFromRows(dup, (list) => {
      seen.push(list);
      return list;
    });
    assert.equal(seen.length, 1, "a shared key runs it");
  });
  test("dedupeIfNeeded == deduplicateCandidatesUI (seeded fuzz)", () => {
    let seed = 12345;
    const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
    const pick = <T,>(xs: T[]) => xs[Math.floor(rand() * xs.length)];
    const emails = ["", "a@x.com", "A@X.com ", "b@y.org", "auto_1@jobdiva.com", "c@z.net", "x@noemail.pair.ai"];
    const phones = ["", "555-123-4567", "(555) 123 4567", "+1 555 123 4567", "000-000-0000", "123", "555 987 6543"];
    const names = ["", "Ann Lee", "ann lee", "Bo", "Cy Do"];
    const lis = ["", "https://www.linkedin.com/in/ann/", "https://linkedin.com/in/ann?x=1", "https://jobdiva.com/p/1", "https://www.linkedin.com/in/bo"];
    const sources = ["JobDiva-TalentSearch", "LinkedIn", "Exa", "JobDiva-Applicants"];
    for (let run = 0; run < 400; run++) {
      const size = 1 + Math.floor(rand() * 12);
      const list = Array.from({ length: size }, (_, i) => {
        const c: Record<string, any> = { candidate_id: `r${run}-${i}`, source: pick(sources), match_score: Math.floor(rand() * 100) };
        const e = pick(emails);
        if (e && rand() < 0.7) c.email = e;
        else if (e) c.data = { email: e };
        const p = pick(phones);
        if (p && rand() < 0.7) c.phone = p;
        else if (p) c.enhanced_info = { phone: p };
        const nm = pick(names);
        if (nm) c.name = nm;
        const li = pick(lis);
        if (li && rand() < 0.5) c.linkedin_url = li;
        else if (li) c.profile_url = li;
        return c;
      });
      const expected = deduplicateCandidatesUI(list.map((c) => ({ ...c })));
      const actual = ls.dedupeIfNeeded(list.map((c) => ({ ...c })), deduplicateCandidatesUI);
      assert.deepEqual(actual, expected, `run ${run}`);
    }
  });
  test("placeholder emails / phones do not defeat the fast path", () => {
    const rows = Array.from({ length: 50 }, (_, i) => ({
      candidate_id: `r${i}`,
      email: i % 3 === 0 ? "Available upon request" : i % 3 === 1 ? `Auto_${i}@jobdiva.com` : "nobody@noemail.pair.ai",
      phone: i % 2 ? "000-000-0000" : "",
      name: "Same Name",
    }));
    assert.equal(ls.mayHaveIdentityDuplicates(rows), false);
    assert.equal(deduplicateCandidatesUI(rows.map((c) => ({ ...c }))).length, 50, "the browser de-dup agrees");
  });
  test("the real deduplicateCandidatesUI merges a duplicate the server let through", () => {
    const cache = cacheOf([
      row("jd1", { ord: 0, row: { candidate_id: "jd1", name: "Ann Lee", email: "ann@example.org", source: "JobDiva-TalentSearch" } }),
      row("li1", { ord: 1, row: { candidate_id: "li1", name: "Ann Lee", email: "ann@example.org", source: "LinkedIn" } }),
    ]);
    const table = ls.tableCandidatesFromRows(cache, deduplicateCandidatesUI);
    assert.equal(table.length, 1);
    assert.equal(table[0].candidate_id, "jd1");
  });
  test("keepResolvedOpenToWork: a resolved open-to-work survives a server re-send", () => {
    const prev = [
      { candidate_id: "a", profile_url: "https://linkedin.com/in/a", open_to_work: true },
      { candidate_id: "b", profile_url: "https://linkedin.com/in/b", open_to_work: "PENDING" },
      { candidate_id: "x", profile_url: "https://linkedin.com/in/c", open_to_work: false },
    ];
    const next = [
      { candidate_id: "a", profile_url: "https://linkedin.com/in/a", open_to_work: "PENDING" },
      { candidate_id: "b", profile_url: "https://linkedin.com/in/b" },
      { candidate_id: "c2", profile_url: "https://linkedin.com/in/c" },
      { candidate_id: "d", open_to_work: true },
    ];
    const out = ls.keepResolvedOpenToWork(prev, next);
    assert.deepEqual(out.map((c) => c.open_to_work), [true, undefined, false, true]);
    assert.equal(out[1], next[1], "untouched rows keep their identity");
    assert.equal(ls.keepResolvedOpenToWork([], next), next);
  });
  test("isTableRow", () => {
    assert.equal(ls.isTableRow(row("x")), true);
    assert.equal(ls.isTableRow(row("x", { state: "skipped", reason: "below_floor" })), true);
    assert.equal(ls.isTableRow(row("x", { state: "skipped", reason: "duplicate_id" })), false);
    assert.equal(ls.isTableRow(row("x", { state: "dropped", reason: "dropped" })), false);
  });
  test("sessionContactOverlay", () => {
    assert.deepEqual(ls.sessionContactOverlay(null), { final: false });
    assert.deepEqual(ls.sessionContactOverlay({ final: { phone: null, email: " q@x.com " } }), { phone: "", email: "q@x.com", final: true });
  });
});

// ── Session → UI ────────────────────────────────────────────────────────────

describe("sessionUiPhase", () => {
  const cases: Array<[Partial<ls.LaunchSessionView>, ls.SessionUiPhase]> = [
    [{ status: "queued", stage: "created", mode: "full_search" }, "sourcing"],
    [{ status: "queued", stage: "created", mode: "provided" }, "selecting"],
    [{ status: "collecting", stage: "created", mode: "provided" }, "collecting"],
    [{ status: "running", stage: "sourcing" }, "sourcing"],
    [{ status: "running", stage: "selecting" }, "selecting"],
    [{ status: "running", stage: "enriching" }, "enriching"],
    [{ status: "awaiting_input", stage: "review" }, "review"],
    [{ status: "queued", stage: "review" }, "launching"],
    [{ status: "running", stage: "splitting" }, "launching"],
    [{ status: "running", stage: "saving" }, "launching"],
    [{ status: "running", stage: "launching" }, "launching"],
    [{ status: "running", stage: "provisioning" }, "completed"],
    [{ status: "completed", stage: "done", terminal: true }, "completed"],
    [{ status: "failed", stage: "saving", terminal: true }, "failed"],
    [{ status: "cancelled", stage: "sourcing", terminal: true }, "cancelled"],
    [{ status: "running", stage: "provisioning", launch_complete: true }, "completed"],
    [{ status: "queued", stage: "done" }, "completed"],
  ];
  for (const [over, expected] of cases) {
    test(`${over.status}/${over.stage}/${over.mode ?? "full_search"} → ${expected}`, () => {
      assert.equal(ls.sessionUiPhase(view(over)), expected);
    });
  }
});

describe("sourcingUi (status line, pills, search phase — parity #6, #15)", () => {
  test("streaming", () => {
    const ui = ls.sourcingUi(view({
      sourcing: {
        attempts_total: 3, current_attempt: 1,
        ui: { search_status: "Scoring 40 profiles...", source_statuses: { Exa: { source: "Exa", status: "ok" } }, criteria_unconfigured: true, criteria_modal_requested: true, error_count: 1, detail_failed_count: 2 },
      },
    }));
    assert.equal(ui.searching, true);
    assert.equal(ui.phase, "full");
    assert.equal(ui.statusText, "Scoring 40 profiles... (attempt 2/3)");
    assert.equal(ui.attemptIndex, 1);
    assert.deepEqual(Object.keys(ui.sourceStatuses), ["Exa"]);
    assert.equal(ui.criteriaUnconfigured, true);
    assert.equal(ui.criteriaModalRequested, true);
    assert.equal(ui.errorCount, 1);
    assert.equal(ui.detailFailedCount, 2);
  });
  test("queued before the first claim reads as searching", () => {
    const ui = ls.sourcingUi(view({ status: "queued", stage: "created", sourcing: null }));
    assert.equal(ui.searching, true);
    assert.equal(ui.statusText, "Connecting to source portals...");
  });
  test("complete only when the search finished — a stopped / failed search never is (parity #15)", () => {
    const done = { sourcing_started_at: "2026-10-11T10:00:00Z", sourcing_done_at: "2026-10-11T10:05:00Z" };
    assert.equal(ls.sourcingUi(view({ stage: "launching", timestamps: done })).phase, "complete");
    assert.equal(ls.sourcingUi(view({ status: "completed", stage: "done", terminal: true, timestamps: done })).phase, "complete");
    assert.equal(ls.sourcingUi(view({ status: "failed", stage: "launching", terminal: true, timestamps: done })).phase, "complete");
    // A launch cancelled after its search finished (QA review closed, "Cancel
    // launch"): the full pool is on screen — the next click launches the rest.
    const cancelledLate = ls.sourcingUi(view({ status: "cancelled", stage: "launching", terminal: true, cancel_requested: true, timestamps: done }));
    assert.deepEqual([cancelledLate.phase, cancelledLate.stoppedEarly], ["complete", false]);
    assert.notEqual(cancelledLate.statusText, "Search stopped");
    // Stopped / failed while searching: back to "sampled", the next click searches again.
    const stopping = ls.sourcingUi(view({ stage: "sourcing", cancel_requested: true }));
    assert.deepEqual([stopping.phase, stopping.statusText, stopping.stoppedEarly, stopping.searching], ["sampled", "Search stopped", true, false]);
    const stopped = ls.sourcingUi(view({ status: "cancelled", stage: "sourcing", terminal: true }));
    assert.deepEqual([stopped.phase, stopped.stoppedEarly], ["sampled", true]);
    const failed = ls.sourcingUi(view({ status: "failed", stage: "sourcing", terminal: true }));
    assert.deepEqual([failed.phase, failed.statusText, failed.stoppedEarly, failed.searching], ["sampled", "Search failed", true, false]);
    assert.equal(ls.sourcingUi(view()).stoppedEarly, false);
    assert.equal(ls.sourcingUi(view({ mode: "provided", status: "cancelled", terminal: true })).stoppedEarly, false);
  });
  test("queued: the queue position; the server's plan for the attempts panel", () => {
    const queued = ls.sourcingUi(view({ status: "queued", stage: "created", queue_position: 2, sourcing: { plan: [{ label: "PAIR generated", boolean_string: "a AND b" }, { label: "relaxed", boolean_string: "a" }] } }));
    assert.equal(queued.statusText, "Waiting for a free search slot — 2 searches ahead…");
    assert.deepEqual(queued.plan, [{ query: "a AND b", label: "PAIR generated" }, { query: "a", label: "relaxed" }]);
    assert.equal(ls.sourcingUi(view({ status: "queued", stage: "created", queue_position: 0 })).statusText, "Starting the search…");
    assert.deepEqual(ls.sourcingUi(view()).plan, []);
  });
  test("a provided session never searches", () => {
    assert.equal(ls.sourcingUi(view({ mode: "provided", stage: "created", status: "queued" })).searching, false);
  });
});

describe("launch_complete", () => {
  test("isLaunchComplete / outcomeStatus", () => {
    assert.equal(ls.isLaunchComplete(view({ stage: "launching" })), false);
    assert.equal(ls.isLaunchComplete(view({ stage: "provisioning" })), true, "stage alone (older server without the flag)");
    assert.equal(ls.isLaunchComplete(view({ stage: "launching", launch_complete: true })), true);
    assert.equal(ls.isLaunchComplete(view({ status: "cancelled", terminal: true, stage: "sourcing" })), true);
    assert.equal(ls.isLaunchComplete(null), false);
    assert.equal(ls.outcomeStatus(view({ stage: "launching" })), null);
    assert.equal(ls.outcomeStatus(view({ stage: "provisioning", launch_complete: true })), "completed");
    assert.equal(ls.outcomeStatus(view({ status: "failed", terminal: true })), "failed");
    assert.equal(ls.outcomeStatus(view({ status: "cancelled", terminal: true })), "cancelled");
    assert.equal(ls.outcomeStatus(view({ status: "completed", terminal: true })), "completed");
  });
  test("a provisioning session redirects and shows its result without waiting for terminal", () => {
    const v = view({ mode: "provided", status: "running", stage: "provisioning", launch_complete: true, counts: { states: { launched: 2 } } });
    assert.equal(ls.shouldRedirectToRankings(v), true);
    const p = ls.sessionToProgress(v, [row("a", { state: "launched", reason: "sent", batch_index: 0 })], { saveBatchSize: 25 });
    assert.deepEqual([p.open, p.phase], [true, "completed"]);
  });
});

describe("outcome and the redirect rule (parity #10)", () => {
  const completed = (states: Record<string, number>, reasons: Record<string, Record<string, number>> = {}) =>
    view({ status: "completed", stage: "done", terminal: true, counts: { states, reasons, selected: 5 } });
  test("redirect iff failed == 0 && saved > 0 && needs_contact == 0", () => {
    assert.equal(ls.shouldRedirectToRankings(completed({ launched: 3 }, { launched: { sent: 3 } })), true);
    assert.equal(ls.shouldRedirectToRankings(completed({ launched: 1, unconfirmed: 1 })), true);
    assert.equal(ls.shouldRedirectToRankings(completed({ launched: 3, failed: 1 })), false);
    assert.equal(ls.shouldRedirectToRankings(completed({ launched: 3, needs_contact: 2 })), false);
    assert.equal(ls.shouldRedirectToRankings(completed({ skipped: 4 })), false);
    assert.equal(ls.shouldRedirectToRankings(view({ status: "failed", terminal: true, counts: { states: { launched: 2 } } })), false);
    assert.equal(ls.shouldRedirectToRankings(view({ status: "cancelled", terminal: true, counts: { states: { launched: 2 } } })), false);
  });
  test("live row tallies win over the session's counts", () => {
    const v = completed({ launched: 3 });
    const rows = [row("a", { state: "failed", failure_stage: "engage" }), row("b", { state: "launched", reason: "sent" })];
    assert.equal(ls.shouldRedirectToRankings(v, ls.tallyRows(rows)), false);
    const o = ls.sessionOutcome(v, ls.tallyRows(rows));
    assert.equal(o.failed, 1);
    assert.equal(o.sent, 1);
    assert.equal(o.saved, 1);
  });
  test("sessionOutcome from counts", () => {
    const o = ls.sessionOutcome(view({
      counts: {
        states: { launched: 4, failed: 1, needs_contact: 2, unconfirmed: 1, skipped: 6 },
        reasons: { launched: { sent: 2, reconciled: 1, already_launched: 1 }, skipped: { dnc: 2, hard_filter: 1, excluded: 3 } },
        sent: 3, already_launched: 1, selected: 14,
      },
    }));
    assert.deepEqual(o, {
      sent: 3, alreadyLaunched: 1, saved: 5, failed: 1, noInterview: 0, needsContact: 2, unconfirmed: 1, launched: 4, selected: 14,
      dncSkipped: 2, hardFilterSkipped: 1, excludedAtLaunch: 3,
    });
  });
  test("a person Pairbot created no interview for is not a failure (legacy counted batch failures only)", () => {
    const v = view({
      status: "completed", stage: "done", terminal: true,
      counts: { states: { launched: 2, failed: 1 }, reasons: { launched: { sent: 2 }, failed: { pairbot_rejected: 1 } }, no_interview: 1, saved_total: 3 },
    });
    const o = ls.sessionOutcome(v);
    assert.deepEqual([o.failed, o.noInterview, o.saved], [0, 1, 3]);
    assert.equal(ls.shouldRedirectToRankings(v), true);
    const rows = [
      row("a", { state: "launched", reason: "sent", batch_index: 0 }),
      row("r", { state: "failed", reason: "pairbot_rejected", failure_stage: "engage", batch_index: 0 }),
    ];
    assert.equal(ls.shouldRedirectToRankings(v, ls.tallyRows(rows)), true);
    assert.deepEqual([...ls.noInterviewIds(rows)], ["r"]);
    const p = ls.sessionToProgress(v, rows, { saveBatchSize: 25 });
    assert.deepEqual(p.batches.map((b) => b.status), ["completed"]);
    assert.equal(p.totalFailedBatches, 0);
    assert.match(ls.completionToasts(v, rows).map((t) => t.message).join(" | "), /1 candidate not launched — PAIR created no interview/);
  });
  test("saved_total (S6's total) counts as saved", () => {
    const v = view({ status: "completed", stage: "done", terminal: true, counts: { states: { unconfirmed: 0 }, saved_total: 4 } });
    assert.equal(ls.sessionOutcome(v).saved, 4);
  });
});

describe("sessionToProgress (the launch progress modal)", () => {
  const opts = { saveBatchSize: 25, jobRef: "26-1", displayName: getCandidateDisplayName };

  test("closed while sourcing, during the QA review and after a cancel", () => {
    assert.equal(ls.sessionToProgress(view(), [], opts).open, false);
    assert.equal(ls.sessionToProgress(view({ status: "awaiting_input", stage: "review" }), [], opts).open, false);
    assert.equal(ls.sessionToProgress(view({ status: "cancelled", terminal: true, stage: "launching" }), [], opts).open, false);
  });

  test("enriching: live counters from the rows + the announced lookups", () => {
    const v = view({
      stage: "enriching",
      events_tail: [{ seq: 4, payload: { type: "enrich_started", lookups: 3 } }],
    });
    const rows = [
      row("a", { state: "selected", contact: { done: true, outcome: "missing_linkedin", looked_up: false } }),
      row("b", { state: "selected", contact: { done: true, outcome: "succeeded", looked_up: true, phone_provider: "apollo" } }),
      row("c", { state: "selected", contact: { done: true, outcome: "not_eligible", looked_up: false } }),
      row("d", { state: "selected", contact: null }),
    ];
    const p = ls.sessionToProgress(v, rows, opts);
    assert.equal(p.open, true);
    assert.equal(p.phase, "enriching");
    assert.equal(p.enrichDone, 3);
    assert.equal(p.enrichTotal, 4, "3 announced lookups + 1 decided without one");
    assert.equal(p.enrichSucceeded, 1);
    assert.equal(p.enrichMissingLinkedIn, 1);
    assert.equal(p.enrichNoContact, 1);
  });

  test("after enrichment: the stage's summary", () => {
    const v = view({ stage: "saving", counts: { enrich: { total: 7, succeeded: 2, already_reachable: 1, missing_linkedin: 1, no_contact: 2, failed: 1 }, split: { ready: 30 } } });
    const p = ls.sessionToProgress(v, [], opts);
    assert.equal(p.phase, "launching");
    assert.deepEqual(
      [p.enrichTotal, p.enrichDone, p.enrichSucceeded, p.enrichAlreadyReachable, p.enrichMissingLinkedIn, p.enrichNoContact, p.enrichFailed],
      [7, 7, 2, 1, 1, 2, 1],
    );
    assert.equal(p.totalCandidates, 30);
    assert.equal(p.batchSize, 25);
    assert.equal(p.jobIdForRelaunch, "26-1");
  });

  test("save batches, saved / sent / failed", () => {
    const rows = [
      ...Array.from({ length: 25 }, (_, i) => row(`a${i}`, { ord: i, state: "launched", reason: "sent", batch_index: 0 })),
      row("b0", { ord: 30, state: "launched", reason: "already_launched", batch_index: 1 }),
      row("b1", { ord: 31, state: "failed", failure_stage: "engage", error: "Pairbot 500", batch_index: 1 }),
      row("b2", { ord: 32, state: "saved", batch_index: 1 }),
      row("c0", { ord: 40, state: "ready" }),
      row("c1", { ord: 41, state: "skipped", reason: "hard_filter", row: { name: "Bo Client" } }),
      row("c2", { ord: 42, state: "skipped", reason: "excluded:Offer Accepted", row: { name: "Cy Offer" } }),
    ];
    const p = ls.sessionToProgress(view({ stage: "launching" }), rows, opts);
    assert.deepEqual(p.batches.map((b) => [b.index, b.size, b.status]), [[0, 25, "completed"], [1, 3, "failed"], [2, 1, "pending"]]);
    assert.equal(p.batches[0].engageSent, 25);
    assert.equal(p.batches[1].alreadySent, 1);
    assert.equal(p.batches[1].savedCount, 3);
    assert.equal(p.batches[1].errorMessage, "Engage failed: Pairbot 500");
    assert.equal(p.totalSaved, 27);
    assert.equal(p.totalEngaged, 25);
    assert.equal(p.totalFailedBatches, 1);
    assert.equal(p.currentBatchIndex, 2);
    assert.equal(p.hardFilterSkipped, 2);
    assert.deepEqual(p.hardFilterSkippedNames, ["Bo Client", "Cy Offer (Offer Accepted)"]);
  });

  test("completed: clean → open (then redirect); failures → open with the error; parked → closed", () => {
    const clean = ls.sessionToProgress(view({ status: "completed", stage: "done", terminal: true }), [row("a", { state: "launched", reason: "sent", batch_index: 0 })], opts);
    assert.deepEqual([clean.open, clean.phase], [true, "completed"]);
    const partial = ls.sessionToProgress(
      view({ status: "completed", stage: "done", terminal: true, error: null }),
      [row("a", { state: "launched", reason: "sent", batch_index: 0 }), row("b", { state: "failed", failure_stage: "save", batch_index: 0 })],
      opts,
    );
    assert.deepEqual([partial.open, partial.phase], [true, "completed"]);
    const allFailed = ls.sessionToProgress(view({ status: "completed", stage: "done", terminal: true }), [row("b", { state: "failed", failure_stage: "save", batch_index: 0 })], opts);
    assert.deepEqual([allFailed.open, allFailed.phase], [true, "failed"]);
    const parked = ls.sessionToProgress(
      view({ status: "completed", stage: "done", terminal: true }),
      [row("a", { state: "launched", reason: "sent", batch_index: 0 }), row("n", { state: "needs_contact" })],
      opts,
    );
    assert.equal(parked.open, false);
    const nothing = ls.sessionToProgress(view({ status: "completed", stage: "done", terminal: true }), [row("s", { state: "skipped", reason: "below_floor" })], opts);
    assert.equal(nothing.open, false);
    const clientOnly = ls.sessionToProgress(view({ status: "completed", stage: "done", terminal: true }), [row("s", { state: "skipped", reason: "hard_filter" })], opts);
    assert.equal(clientOnly.open, true);
    assert.match(clientOnly.finalMessage ?? "", /client company/);
  });

  test("waiting / queued: the modal says why nothing moves", () => {
    const fmt = (ms: number) => `at:${new Date(ms).toISOString().slice(11, 16)}`;
    const waiting = ls.sessionToProgress(view({ stage: "launching", phase: "waiting", retry_at: "2026-10-11T10:42:00Z" }), [], { ...opts, formatTime: fmt });
    assert.equal(waiting.statusNote, "Waiting for PAIR — resumes automatically at at:10:42.");
    assert.equal(ls.sessionToProgress(view({ stage: "saving", phase: "waiting" }), [], opts).statusNote, "Waiting for PAIR — resumes automatically shortly.");
    assert.equal(ls.sessionToProgress(view({ status: "queued", stage: "saving", queue_position: 2 }), [], opts).statusNote, "Waiting for a free launch slot — 2 launches ahead…");
    assert.equal(ls.sessionToProgress(view({ status: "queued", stage: "enriching", queue_position: 0 }), [], opts).statusNote, "Waiting for a free launch slot…");
    assert.equal(ls.sessionToProgress(view({ stage: "launching", phase: "running" }), [], opts).statusNote, undefined);
    assert.match(ls.sessionToProgress(view({ stage: "saving", cancel_requested: true }), [], opts).statusNote ?? "", /^Stopping/);
    assert.equal(ls.sessionToProgress(view({ status: "completed", stage: "done", terminal: true, phase: "completed" }), [], opts).statusNote, undefined);
    assert.equal(ls.sessionToProgress(view({ stage: "sourcing", phase: "waiting" }), [], opts).statusNote, undefined, "closed while sourcing (the status line shows it)");
  });

  test("failed: modal with the error, except a failure during sourcing (a toast)", () => {
    const late = ls.sessionToProgress(view({ status: "failed", stage: "launching", terminal: true, error: "access revoked" }), [], opts);
    assert.deepEqual([late.open, late.phase, late.finalMessage], [true, "failed", "access revoked"]);
    const early = ls.sessionToProgress(view({ status: "failed", stage: "sourcing", terminal: true, error: "boom" }), [], opts);
    assert.equal(early.open, false);
  });
});

describe("failedRowsToCsv", () => {
  test("normalises the server's view=failed rows to LaunchFailedCandidate", () => {
    const out = ls.failedRowsToCsv([
      {
        candidate_id: 12, name: "", email: "", phone: "555", source: "LinkedIn", headline: null, location: "NYC",
        experience_years: "7", match_score: null, skills: ["Go", "", "SQL"], matched_skills: "Go", resume_id: 9,
        profile_url: "", batch_index: null, failure_stage: "save", error_message: "not saved",
      },
      { candidate_id: "", name: "ghost" },
      { candidate_id: "x", failure_stage: "weird", batch_index: 3 },
    ]);
    assert.equal(out.length, 2);
    assert.deepEqual(out[0], {
      candidate_id: "12", name: "12", email: null, phone: "555", source: "LinkedIn", headline: "", location: "NYC",
      experience_years: 7, match_score: 0, skills: "Go; SQL", matched_skills: "Go", resume_id: "9", profile_url: null,
      batch_index: 0, failure_stage: "save", error_message: "not saved",
    });
    assert.equal(out[1].failure_stage, "engage");
    assert.equal(out[1].batch_index, 3);
    assert.deepEqual(ls.failedRowsToCsv(null), []);
    assert.deepEqual(ls.failedRowsToCsv([{ candidate_id: "a" }, { candidate_id: "b" }], new Set(["a"])).map((r) => r.candidate_id), ["b"]);
  });
});

describe("contact modals", () => {
  test("review rows: every selected row, both fields, lookup marks", () => {
    const rows = [
      row("b", { ord: 2, state: "selected", current_phone: "555 111 2222", current_email: "", contact: { looked_up: true, override: { phone: "555 111 2222" }, phone_provider: "exa_deep" }, row: { name: "Bea", title: "Dev", location: "LA", source: "LinkedIn" } }),
      row("a", { ord: 1, state: "selected", current_phone: "", current_email: "a@x.com", row: { firstName: "Al", lastName: "Q", headline: "QA" } }),
      row("c", { ord: 0, state: "skipped", reason: "hard_filter" }),
    ];
    const out = ls.reviewCandidatesFromRows(rows, { jobdivaId: "26-1", displayName: getCandidateDisplayName, providerLabel: (p) => (p === "exa_deep" ? "Exa deep search" : String(p ?? "")) });
    assert.deepEqual(out.map((c) => c.candidate_id), ["a", "b"]);
    assert.deepEqual(out[1], {
      candidate_id: "b", name: "Bea", headline: "Dev", location: "LA", source: "LinkedIn", jobdiva_id: "26-1",
      needsPhone: true, needsEmail: true, currentPhone: "555 111 2222", currentEmail: "", lookedUp: true,
      foundPhone: "555 111 2222", foundPhoneBy: "Exa deep search", foundEmail: undefined, foundEmailBy: "",
    });
    assert.equal(out[0].name, "Al Q");
  });
  test("needs-contact rows come from S5's needs entry", () => {
    const rows = [
      row("n", { state: "needs_contact", current_phone: "x", contact: { needs: { candidate_id: "n", name: "Nia", headline: "PM", location: "SF", source: "Exa", jobdiva_id: "26-1", needsPhone: false, needsEmail: true, currentPhone: "555 999 0000", currentEmail: "" } } }),
      row("m", { state: "needs_contact", row: { name: "Mo" }, contact: {} }),
      row("z", { state: "launched" }),
    ];
    const out = ls.needsContactCandidatesFromRows(rows, { jobdivaId: "J", displayName: getCandidateDisplayName });
    assert.equal(out.length, 2);
    assert.deepEqual(out[0], {
      candidate_id: "n", name: "Nia", headline: "PM", location: "SF", source: "Exa", jobdiva_id: "26-1",
      needsPhone: false, needsEmail: true, currentPhone: "555 999 0000", currentEmail: "",
    });
    assert.equal(out[1].name, "Mo");
    assert.equal(out[1].needsPhone, true);
    assert.equal(out[1].jobdiva_id, "J");
  });
  test("confirm body: exactly what was confirmed (omitted = none, never a fallback)", () => {
    assert.deepEqual(ls.buildConfirmBody({ a: { phone: " 555 ", email: "" }, b: { email: "b@x.com" }, c: {} }), {
      contacts: { a: { phone: "555" }, b: { email: "b@x.com" }, c: {} },
      candidate_ids: ["a", "b", "c"],
    });
  });
  test("needs-contact body: launch the completed rows, skip the rest of the modal", () => {
    assert.deepEqual(ls.buildNeedsContactBody({ a: { phone: "555 111 2222" } }, ["a", "b", "c"]), {
      contacts: { a: { phone: "555 111 2222" } }, candidate_ids: ["a"], skip_ids: ["b", "c"],
    });
    assert.deepEqual(ls.buildNeedsContactBody({}, ["a", "b"], "k"), { contacts: {}, candidate_ids: [], skip_ids: ["a", "b"], idempotency_key: "k" });
  });
});

describe("milestones, toasts, telemetry", () => {
  test("reachedMilestones and the attach seed", () => {
    const v = view({
      status: "awaiting_input", stage: "review",
      counts: { selection: { selected: 3 }, enrich: { total: 2 } },
      timestamps: { sourcing_done_at: "2026-10-11T10:00:00Z" },
      sourcing: { ui: { criteria_modal_requested: true, error_count: 2 } },
    });
    assert.deepEqual(ls.reachedMilestones(v), ["search_finished", "selection", "enrich", "review"]);
    assert.deepEqual(ls.seedMarksForAttach(v), ["search_finished", "selection", "enrich", "criteria_modal", "search_error:2"]);
    assert.deepEqual(ls.reachedMilestones(view({ mode: "provided", status: "failed", terminal: true })), ["outcome"]);
    // As handleRunSearch: a failed search reports failed, then finished.
    assert.deepEqual(ls.reachedMilestones(view({ status: "failed", terminal: true })), ["search_failed", "search_finished", "outcome"]);
    assert.deepEqual(ls.seedMarksForAttach(view({ status: "failed", terminal: true })), ["search_failed", "search_finished"]);
    assert.deepEqual(ls.reachedMilestones(view({ status: "cancelled", terminal: true })), ["search_finished", "outcome"]);
    // The launch is over while JobDiva provisioning still runs: the outcome is due now.
    assert.deepEqual(ls.reachedMilestones(view({ mode: "provided", status: "running", stage: "provisioning", launch_complete: true })), ["outcome"]);
    assert.deepEqual(ls.seedMarksForAttach(view({ mode: "provided", status: "running", stage: "provisioning", launch_complete: true })), []);
  });
  test("selection toast", () => {
    const sel = (selected: number, eligible: number, extra = {}) => view({ counts: { selection: { selected, eligible_total: eligible, below_floor: 2, unscored: 1, ...extra } } });
    assert.equal(ls.selectionToast(sel(3, 3), 60)?.message, "Search complete — launching PAIR for 3 candidates at 60%+ (2 below 60%, 1 unscored held back)…");
    assert.equal(ls.selectionToast(sel(250, 400), 60)?.message, "Search complete — launching PAIR for the best 250 of 400 candidates at 60%+ (your launch count). Raise the number or click Launch PAIR again for the rest.");
    assert.equal(ls.selectionToast(sel(0, 0), 60)?.message, "Search complete — no launchable candidates at 60%+ (2 below 60%, 1 unscored held back), so nothing was launched.");
    assert.equal(ls.selectionToast(view({ mode: "provided", counts: { selection: { selected: 2 } } }), 60), null);
    assert.equal(ls.selectionToast(view(), 60), null);
  });
  test("completion toasts", () => {
    const done = (states: Record<string, number>, rows: ls.LaunchSessionRow[], over: Partial<ls.LaunchSessionView> = {}) =>
      ls.completionToasts(view({ status: "completed", stage: "done", terminal: true, counts: { states, selected: 3 }, ...over }), rows, { displayName: getCandidateDisplayName })
        .map((t) => `${t.type}:${t.message}`);
    assert.deepEqual(done({}, [row("a", { state: "launched", reason: "sent" }), row("b", { state: "launched", reason: "sent" })]), ["success:Launched PAIR for 2 candidates"]);
    assert.deepEqual(done({}, [row("a", { state: "launched", reason: "already_launched", row: { name: "Ann" } })]), [
      "info:Skipped Ann (Already Launched)", "success:All selected candidates were already launched",
    ]);
    assert.deepEqual(done({}, [row("a", { state: "launched", reason: "sent" }), row("b", { state: "failed" }), row("d", { state: "skipped", reason: "dnc" })], { error: null }), [
      "info:1 candidate skipped — Do Not Contact list match", "info:Launched 1 · 1 candidate failed",
    ]);
    assert.deepEqual(done({}, [row("s", { state: "skipped", reason: "below_floor" })]), ["info:No candidates were launched (all were skipped or blocked)"]);
    assert.deepEqual(done({}, [row("u", { state: "unconfirmed" })]).length, 1);
    assert.deepEqual(
      ls.completionToasts(view({ status: "failed", terminal: true, error: "access revoked" }), []).map((t) => t.message),
      ["Launch PAIR failed: access revoked"],
    );
    assert.deepEqual(
      ls.completionToasts(view({ status: "cancelled", terminal: true, cancel_reason: "cancelled by recruiter" }), []).map((t) => t.message),
      ["Launch stopped — nothing more will be sent."],
    );
    assert.deepEqual(
      ls.completionToasts(view({ status: "cancelled", terminal: true, cancel_reason: "review expired" }), [row("a", { state: "launched", reason: "sent" })]).map((t) => t.message),
      ["Launch stopped — 1 candidate already launched stay launched."],
    );
  });
  test("enrichment and split toasts", () => {
    const v = view({ counts: {
      enrich: { total: 9, succeeded: 2, missing_linkedin: 1, no_contact: 3, not_eligible: 1, failed: 1, phone_found_by: { apollo: 2 }, email_found_by: {}, kipplo_issues: { out_of_credits: 2, rate_limited: 1 } },
      split: { duplicate_email: 2, duplicate_phone: 0 },
    } });
    const enrich = ls.enrichToasts(v, { minScore: 60, describeFound: (p) => `${Object.values(p).reduce((a, b) => a + b, 0)} phones` }).map((t) => t.message);
    assert.deepEqual(enrich, [
      "Contact lookup found 2 phones for 2 candidates.",
      "Kipplo gave no result for 2 candidates (out_of_credits); the other providers were asked instead.",
      "Enrichment summary: 1 missing LinkedIn URL · 2 still missing phone & email · 1 not looked up (below 60% or an ineligible source) · 1 enrichment call failed",
    ]);
    assert.deepEqual(ls.splitToasts(v).map((t) => t.message), ["2 selected candidates share duplicate email addresses. PAIR needs a unique real email per candidate."]);
    assert.deepEqual(ls.enrichToasts(view(), { minScore: 60, describeFound: () => "" }), []);
  });
  test("milestone events keep today's names, carry job_ref + launch_session_id, no PII", () => {
    const ctx = { jobRef: "26-1", minScore: 60, launchCount: 250 };
    const v = view({
      status: "running", stage: "enriching", require_review: true,
      sourcing: {
        pool: 110, qualified: 55,
        attempts: [{ label: "PAIR generated", rows: 90, qualified: 40 }, { label: "relaxed", rows: 30, qualified: 15 }],
        plan: [{ label: "PAIR generated", boolean_string: "(java AND spring)" }, { label: "relaxed", boolean_string: "java" }],
      },
      timestamps: { sourcing_started_at: "2026-10-11T10:00:00Z", sourcing_done_at: "2026-10-11T10:00:30Z" },
      counts: {
        selection: { pool: 120, selected: 50, eligible_total: 70, below_floor: 30, unscored: 2 },
        enrich: { total: 8, looked_up: 6, succeeded: 3, not_eligible: 1, failed: 1, phone_found_by: { apollo: 2, exa: 1 }, email_found_by: { apollo: 1 }, kipplo_issues: {} },
        reasons: { skipped: { hard_filter: 2 } },
      },
      input: { launch_count: 50 },
    });
    const [search] = ls.milestoneEvents("search_finished", v, ctx);
    assert.equal(search.name, "job_wizard_step5_candidate_search_finished");
    assert.equal(search.props.launch_session_id, "s-1");
    assert.equal(search.props.job_ref, "26-1");
    assert.equal(search.props.runtime_seconds, 30);
    assert.equal(search.props.runs_executed, 2);
    assert.equal(search.props.candidates_found, 120, "handleRunSearch counted every attempt's rows");
    assert.equal(search.props.pool_size, 110, "the de-duplicated pool");
    assert.deepEqual(search.props.runs, [
      { attempt: 1, label: "PAIR generated", query: "(java AND spring)", results_count: 90, qualified: 40 },
      { attempt: 2, label: "relaxed", query: "java", results_count: 30, qualified: 15 },
    ]);
    assert.deepEqual(search.props.boolean_attempts, [
      { attempt: 1, label: "PAIR generated", query: "(java AND spring)" },
      { attempt: 2, label: "relaxed", query: "java" },
    ]);
    const long = ls.milestoneEvents("search_finished", view({ sourcing: { attempts: [{ rows: 1 }], plan: [{ boolean_string: "x".repeat(400) }] } }), ctx)[0];
    assert.equal(String((long.props.runs as any[])[0].query).length, 261, "queries are cut at 260");
    assert.equal(ls.milestoneEvents("search_finished", view({ sourcing: { pool: 7 } }), ctx)[0].props.candidates_found, 7);

    const selection = ls.milestoneEvents("selection", v, ctx);
    assert.deepEqual(selection.map((e) => e.name), ["job_wizard_step5_auto_launch_selection", "job_wizard_step5_launch_pair_clicked"]);
    assert.deepEqual(selection[0].props, {
      step: 5, job_ref: "26-1", launch_session_id: "s-1", mode: "full_search", trigger: "",
      pool_size: 120, selected_count: 50, eligible_total: 70, below_floor: 30, unscored: 2, min_score: 60, launch_count: 50,
    });
    assert.deepEqual(selection[1].props, {
      step: 5, job_ref: "26-1", launch_session_id: "s-1", mode: "full_search", trigger: "",
      selected_candidates_count: 50, hard_filter_skipped: 2,
    });
    // "Launch remaining" reports its selection at the click; launch_pair_clicked still fires.
    assert.deepEqual(
      ls.milestoneEvents("selection", view({ mode: "provided", trigger: "launch_remaining", counts: { selection: { selected: 2 } } }), ctx).map((e) => e.name),
      ["job_wizard_step5_launch_pair_clicked"],
    );
    // Nothing selected: nothing went into the Launch PAIR pipeline.
    assert.deepEqual(ls.milestoneEvents("selection", view({ mode: "provided", counts: { selection: { selected: 0 } } }), ctx), []);

    const [enrich] = ls.milestoneEvents("enrich", v, ctx);
    assert.equal(enrich.name, "job_wizard_step5_launch_contact_lookup");
    assert.deepEqual([enrich.props.phones_found, enrich.props.emails_found, enrich.props.qa_review], [3, 1, true]);
    assert.deepEqual(ls.milestoneEvents("enrich", view(), ctx), []);
    assert.deepEqual(ls.milestoneEvents("split", v, ctx), []);
    assert.deepEqual(ls.milestoneEvents("review", v, ctx), []);

    const [failed] = ls.milestoneEvents("search_failed", view({ status: "failed", terminal: true, error: "x".repeat(500) }), ctx);
    assert.equal(failed.name, "job_wizard_step5_candidate_search_failed");
    assert.equal(String(failed.props.error).length, 301);
    assert.equal(ls.milestoneEvents("search_failed", view({ status: "failed", terminal: true }), ctx)[0].props.error, "launch session failed while sourcing");

    const [done] = ls.milestoneEvents("outcome", view({ status: "running", stage: "provisioning", launch_complete: true, timestamps: { clicked_at: "2026-10-11T10:00:00Z", last_batch_done_at: "2026-10-11T10:03:00Z" } }), {
      ...ctx, rows: [row("a", { state: "launched", reason: "sent" }), row("b", { state: "needs_contact" })],
    });
    assert.equal(done.name, "job_wizard_step5_launch_session_finished");
    assert.deepEqual(
      [done.props.status, done.props.sent, done.props.needs_contact, done.props.click_to_done_ms],
      ["completed", 1, 1, 180000],
    );
    const json = JSON.stringify([search, selection, enrich, done]);
    assert.ok(!/Person|@|555/.test(json), "no names, emails or phones in event props");
  });
  test("click → done measure: skipped when the click happened before this page loaded", () => {
    const v = view({ timestamps: { clicked_at: "2026-10-11T10:00:00Z", completed_at: "2026-10-11T10:01:00Z" } });
    assert.deepEqual(ls.clickToDoneMeasure(v, 90000), { start: 30000, end: 90000 });
    assert.equal(ls.clickToDoneMeasure(v, 59000), null, "a reattached tab younger than the launch");
    assert.equal(ls.clickToDoneMeasure(view(), 90000), null);
  });
  test("browser-wide marks: once per session across tabs, seeded only for sessions this browser never tracked", () => {
    const now = Date.parse("2026-10-11T10:00:00Z");
    let m: ls.BrowserMarks = {};
    let r = ls.addBrowserMark(m, "s1", "selection", now);
    assert.equal(r.added, true);
    m = r.marks;
    r = ls.addBrowserMark(m, "s1", "selection", now);
    assert.deepEqual([r.added, r.marks], [false, m]);
    // Attaching to a session this browser already tracks keeps its record (the rest still goes out)...
    assert.equal(ls.seedBrowserMarks(m, "s1", ["selection", "enrich"], now), m);
    // ...one it never saw (a teammate's, another device): history is not re-sent.
    m = ls.seedBrowserMarks(m, "s2", ["search_finished", "selection"], now);
    assert.equal(ls.addBrowserMark(m, "s2", "selection", now).added, false);
    assert.equal(ls.addBrowserMark(m, "s2", "outcome", now).added, true);
    // Storage round trip, junk, pruning by age and count.
    assert.deepEqual(ls.parseBrowserMarks(JSON.stringify(m)), m);
    assert.deepEqual(ls.parseBrowserMarks("{bad"), {});
    assert.deepEqual(ls.parseBrowserMarks(JSON.stringify({ a: { k: ["x", 1], t: 5 }, b: "junk" })), { a: { k: ["x"], t: 5 } });
    const old = { old: { k: ["x"], t: now - 8 * 24 * 3600 * 1000 }, fresh: { k: ["y"], t: now - 1000 } };
    assert.deepEqual(Object.keys(ls.pruneBrowserMarks(old, now)), ["fresh"]);
    const many: ls.BrowserMarks = {};
    for (let i = 0; i < 130; i++) many[`s${i}`] = { k: [], t: now - i };
    const pruned = ls.pruneBrowserMarks(many, now);
    assert.equal(Object.keys(pruned).length, 100);
    assert.ok(pruned.s0 && !pruned.s120, "the newest are kept");
  });
  test("timings and event props (ids and counts only)", () => {
    const v = view({ trigger: "launch_remaining", mode: "provided", timestamps: { clicked_at: "2026-10-11T10:00:00Z", completed_at: "2026-10-11T10:02:30Z", sourcing_started_at: "2026-10-11T10:00:01Z", sourcing_done_at: "2026-10-11T10:01:01Z" } });
    assert.equal(ls.clickToDoneMs(v), 150000);
    assert.equal(ls.sourcingMs(v), 60000);
    assert.equal(ls.clickToDoneMs(view()), null);
    assert.equal(ls.clickToDoneMs(view({ timestamps: { clicked_at: "2026-10-11T10:00:00Z", last_batch_done_at: "2026-10-11T10:01:00Z" } })), 60000);
    assert.deepEqual(ls.sessionEventProps(v, "26-1"), { step: 5, job_ref: "26-1", launch_session_id: "s-1", mode: "launch_remaining", trigger: "launch_remaining" });
    assert.equal(ls.heldBackNote(undefined, 60), "");
  });
});

describe("final-review fixes (flags, row lookup by id, retries)", () => {
  const HOOK_SOURCE = new URL("../hooks/use-launch-session.ts", import.meta.url);

  test("sessions are looked for whatever config.enabled says (rollout #3)", () => {
    assert.equal(ls.shouldLookForSessions({ reattach: true, jobRef: "26-1", configState: "ready" }), true);
    assert.equal(ls.shouldLookForSessions({ reattach: true, jobRef: "26-1", configState: "loading" }), false);
    assert.equal(ls.shouldLookForSessions({ reattach: true, jobRef: "26-1", configState: "failed" }), false);
    assert.equal(ls.shouldLookForSessions({ reattach: false, jobRef: "26-1", configState: "ready" }), false);
    assert.equal(ls.shouldLookForSessions({ reattach: true, jobRef: "  ", configState: "ready" }), false);
    // An enabled:false answer is still a ready config: reattach / observe / parked offer run…
    const off = ls.classifyConfigResponse({ ok: true, status: 200, data: { enabled: false } });
    assert.equal(off.config?.enabled, false);
    // …while new clicks keep the legacy flow (serverFlowReady reads enabled).
    const hook = readFileSync(HOOK_SOURCE, "utf8");
    assert.match(hook, /if \(!shouldLookForSessions\(\{ reattach, jobRef: ref, configState \}\)\) return;/);
    assert.doesNotMatch(hook, /!config\?\.enabled\) return/);
    assert.match(hook, /return Boolean\(cfg\?\.enabled\) && !disabledRef\.current;/);
    // The résumé viewer of a server table is not gated on enabled either.
    const page = readFileSync(PAGE_SOURCE, "utf8");
    assert.doesNotMatch(page, /launchSession\.config\?\.enabled && tableSession/);
  });

  test("503 {code: disabled} is final for writes; other 5xx / 429 / network are retried", () => {
    const disabled = { ok: false, status: 503, body: { detail: { code: "disabled", message: "paused" } } };
    assert.equal(ls.shouldRetryWrite(disabled), false);
    assert.equal(ls.shouldRetryWrite({ ok: false, status: 503, body: { detail: "busy" } }), true);
    assert.equal(ls.shouldRetryWrite({ ok: false, status: 429, body: null }), true);
    assert.equal(ls.shouldRetryWrite({ ok: false, status: 0, body: null }), true);
    assert.equal(ls.shouldRetryWrite({ ok: false, status: 409, body: null }), false);
    assert.equal(ls.shouldRetryWrite({ ok: true, status: 201 }), false);
    assert.equal(ls.classifyCreateResponse(disabled).kind, "disabled");
  });

  test("a queue-expired session's toast says why in words", () => {
    assert.equal(ls.cancelReasonText("queue_expired"), "it waited too long for a free launch slot — please launch again");
    assert.equal(ls.cancelReasonText(" review expired "), "review expired");
    assert.equal(ls.cancelReasonText(null), "");
    const [t] = ls.completionToasts(view({ status: "cancelled", terminal: true, cancel_reason: "queue_expired" } as any), []);
    assert.equal(t.message, "Launch stopped (it waited too long for a free launch slot — please launch again) — nothing more will be sent.");
    const [u] = ls.completionToasts(view({ status: "cancelled", terminal: true, cancel_reason: "cancelled by user" } as any), []);
    assert.equal(u.message, "Launch stopped — nothing more will be sent.");
  });

  test("candidatesQueryString carries ids (deduplicated, at most 50) next to the other filters", () => {
    assert.equal(ls.candidatesQueryString(), "");
    assert.equal(ls.candidatesQueryString({ view: "full", ids: ["c1"], limit: 1 }), "?view=full&limit=1&ids=c1");
    assert.equal(ls.candidatesQueryString({ ids: [" a ", "b", "a", ""] }), "?ids=a%2Cb");
    const many = Array.from({ length: 60 }, (_, i) => `x${i}`);
    const sent = new URLSearchParams(ls.candidatesQueryString({ ids: many }).slice(1)).get("ids")!.split(",");
    assert.equal(sent.length, ls.MAX_ROW_IDS);
    assert.equal(
      ls.candidatesQueryString({ states: ["failed", "launched"], view: "table", limit: 500, afterRev: 0, afterId: "" }),
      "?state=failed%2Claunched&view=table&limit=500&after_rev=0&after_id=",
    );
    assert.equal(ls.candidatesQueryString({ offset: 200, limit: 200 }), "?limit=200&offset=200");
  });

  const fullRowsOf = (...ids: string[]) => ids.map((id, i) => row(id, { rev: i + 1, row: { candidate_id: id, resume_text: `cv ${id}` } }));
  const pageOf = (rows: ls.LaunchSessionRow[], total = rows.length, more = false): ls.LaunchSessionRowsPage => ({
    rows, total, cand_rev: 9, has_more: more, next: rows.length ? { after_rev: Number(rows[rows.length - 1].rev), after_id: rows[rows.length - 1].candidate_id } : undefined,
  });

  test("findFullRow reads the row by id first — a restored table needs no keyset position (contract #1)", async () => {
    const calls: ls.CandidatesQuery[] = [];
    const got = await ls.findFullRow({
      candidateId: "c77",
      read: async (q) => {
        calls.push(q);
        return q.ids ? pageOf(fullRowsOf("c77")) : pageOf([]);
      },
    });
    assert.deepEqual(got, { candidate_id: "c77", resume_text: "cv c77" });
    assert.deepEqual(calls, [{ view: "full", ids: ["c77"], limit: 1 }]);
    // total 0 from an API that knows ids: not in this session — no scan.
    calls.length = 0;
    assert.equal(await ls.findFullRow({ candidateId: "zz", read: async (q) => { calls.push(q); return pageOf([], 0); } }), null);
    assert.equal(calls.length, 1);
  });

  test("findFullRow falls back to the keyset position / scan on an API without ids, or a failed read", async () => {
    // An older API ignores `ids` and answers the first row of the pool.
    const pool = fullRowsOf("a", "b", "c", "d");
    const older = async (q: ls.CandidatesQuery) => {
      const after = typeof q.afterRev === "number" ? pool.filter((r) => Number(r.rev) > Number(q.afterRev)) : pool;
      const rows = after.slice(0, q.limit ?? 10);
      return pageOf(rows, after.length, rows.length < after.length);
    };
    assert.deepEqual(await ls.findFullRow({ candidateId: "c", read: older, known: { after_rev: 2, after_id: "b" } }),
      { candidate_id: "c", resume_text: "cv c" });
    assert.deepEqual(await ls.findFullRow({ candidateId: "d", read: older, scanLimit: 1 }), { candidate_id: "d", resume_text: "cv d" });
    assert.equal(await ls.findFullRow({ candidateId: "nope", read: older, scanLimit: 1, scanPages: 2 }), null);
    // The by-id read failed (null): the scan still runs.
    let n = 0;
    const flaky = async (q: ls.CandidatesQuery) => (n++ === 0 ? null : older(q));
    assert.deepEqual(await ls.findFullRow({ candidateId: "b", read: flaky }), { candidate_id: "b", resume_text: "cv b" });
    // The hook delegates to it.
    assert.match(readFileSync(HOOK_SOURCE, "utf8"), /return findFullRow\(\{/);
  });
});
