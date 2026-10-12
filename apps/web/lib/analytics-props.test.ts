import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { nrBridgeProps, shouldBridgeToNr } from "./analytics-props.ts";

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
