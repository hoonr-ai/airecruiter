import assert from "node:assert/strict";
import { test } from "node:test";

import { shouldShowQuestionsCompleted } from "./activityTimeline.ts";

test("hides questions completed on call parking and SIP start", () => {
  assert.equal(shouldShowQuestionsCompleted("call_dispatch_requested"), false);
  assert.equal(shouldShowQuestionsCompleted("call_initiated"), false);
});

test("hides questions completed on other call_* events", () => {
  assert.equal(shouldShowQuestionsCompleted("call_status_no_answer"), false);
  assert.equal(shouldShowQuestionsCompleted("call_amd_voicemail"), false);
});

test("hides questions completed on interview start", () => {
  assert.equal(shouldShowQuestionsCompleted("interview_started_web"), false);
  assert.equal(shouldShowQuestionsCompleted("interview_started_call"), false);
});

test("shows questions completed on real progress events", () => {
  assert.equal(shouldShowQuestionsCompleted("interview_partial_completed"), true);
  assert.equal(shouldShowQuestionsCompleted("questionnaire_submitted"), true);
  assert.equal(shouldShowQuestionsCompleted("interview_completed"), true);
});
