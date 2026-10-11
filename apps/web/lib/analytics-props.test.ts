import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  MAX_EVENT_JSON_LENGTH,
  nrBridgeProps,
  sanitizeEventProps,
  sanitizeEventValue,
  shouldBridgeToNr,
} from "./analytics-props.ts";

describe("sanitizeEventProps (Amplitude)", () => {
  it("JSON-stringifies objects instead of [object Object]", () => {
    const out = sanitizeEventProps({
      quality: { scored: 3, dropped: 1 },
      phone_found_by: { kipplo: 2, exa: 1 },
      runs: [{ source: "jobdiva", found: 4 }],
    });
    assert.equal(out.quality, '{"scored":3,"dropped":1}');
    assert.equal(out.phone_found_by, '{"kipplo":2,"exa":1}');
    assert.equal(out.runs, '[{"source":"jobdiva","found":4}]');
  });

  it("keeps primitives and the comma-joined form of primitive arrays", () => {
    assert.deepEqual(
      sanitizeEventProps({ step: 5, ok: true, mode: "full_search", changed_fields: ["a", "b"], none: null, nope: undefined }),
      { step: 5, ok: true, mode: "full_search", changed_fields: "a,b" },
    );
  });

  it("caps JSON at 1000 chars and survives cycles", () => {
    const big = sanitizeEventValue({ blob: "x".repeat(5000) }) as string;
    assert.equal(big.length, MAX_EVENT_JSON_LENGTH);
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    assert.equal(sanitizeEventValue(cyclic), "[object Object]");
  });

  it("formats Dates and Errors readably", () => {
    assert.equal(sanitizeEventValue(new Date("2026-10-11T00:00:00Z")), "2026-10-11T00:00:00.000Z");
    assert.equal(sanitizeEventValue(new Error("boom")), "Error: boom");
    assert.equal(sanitizeEventValue(() => 1), undefined);
  });
});

describe("shouldBridgeToNr", () => {
  it("bridges the Step-5, launch and wizard-completion events", () => {
    for (const e of [
      "job_wizard_step5_source_and_launch_started",
      "job_wizard_step5_launch_pair_clicked",
      "job_wizard_step5_candidate_search_finished",
      "job_wizard_step5_boolean_edit_saved",
      "launch_session_stage",
      "pair_launch_finished",
      "job_wizard_step_completed",
    ]) {
      assert.ok(shouldBridgeToNr(e), e);
    }
  });

  it("never bridges the high-volume events", () => {
    for (const e of [
      "ui_click",
      "page_view",
      "frontend_log",
      "frontend_error_log",
      "frontend_error",
      "api_request_success",
      "api_request_error",
      "api_request_exception",
      "user_authenticated",
      "job_wizard_step_started",
      "job_wizard_step2_title_enhance_failed",
      "job_wizard_step5_boolean_edited",
      "",
    ]) {
      assert.ok(!shouldBridgeToNr(e), e);
    }
  });
});

describe("nrBridgeProps", () => {
  it("drops the wizard form snapshots", () => {
    assert.deepEqual(nrBridgeProps({ changes: [{ field: "jobTitle" }], changes_count: 1, job_ref: "26-1" }), {
      changes_count: 1,
      job_ref: "26-1",
    });
    assert.deepEqual(nrBridgeProps(undefined), {});
  });
});
