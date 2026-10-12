import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  normalizePermissionState,
  shouldShowBrowserPopup,
  notificationClickPath,
} from "./browser-notifications.ts";

describe("normalizePermissionState", () => {
  it("maps the Permissions API's prompt to Notification.permission's default", () => {
    assert.equal(normalizePermissionState("prompt"), "default");
  });

  it("passes granted and denied through unchanged", () => {
    assert.equal(normalizePermissionState("granted"), "granted");
    assert.equal(normalizePermissionState("denied"), "denied");
  });
});

describe("shouldShowBrowserPopup", () => {
  it("never pops without granted permission, regardless of visibility/focus", () => {
    assert.equal(shouldShowBrowserPopup("default", false, false), false);
    assert.equal(shouldShowBrowserPopup("denied", false, false), false);
    assert.equal(shouldShowBrowserPopup("unsupported", false, false), false);
  });

  it("pops when the tab is fully hidden", () => {
    assert.equal(shouldShowBrowserPopup("granted", false, false), true);
  });

  it("pops when the tab is visible but another window/app has OS focus", () => {
    assert.equal(shouldShowBrowserPopup("granted", true, false), true);
  });

  it("does not pop when the tab is visible and focused — the in-app feed already shows it", () => {
    assert.equal(shouldShowBrowserPopup("granted", true, true), false);
  });
});

describe("notificationClickPath", () => {
  it("links straight to the candidate's report when both ids are present", () => {
    assert.equal(
      notificationClickPath({ jobdiva_id: "26-14907", candidate_id: "123" }),
      "/jobs/26-14907/report?candidateId=123"
    );
  });

  it("URL-encodes ids that need it", () => {
    assert.equal(
      notificationClickPath({ jobdiva_id: "26/14907", candidate_id: "a b" }),
      "/jobs/26%2F14907/report?candidateId=a%20b"
    );
  });

  it("falls back to the notifications page when either id is missing", () => {
    assert.equal(notificationClickPath({ jobdiva_id: null, candidate_id: "123" }), "/notifications");
    assert.equal(notificationClickPath({ jobdiva_id: "26-14907", candidate_id: null }), "/notifications");
    assert.equal(notificationClickPath({ jobdiva_id: null, candidate_id: null }), "/notifications");
  });
});
