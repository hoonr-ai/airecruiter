import test from "node:test";
import assert from "node:assert/strict";
import { resolveRejectReason, isRejectReasonValid } from "./rejection.ts";

test("resolveRejectReason", async (t) => {
  await t.test("returns standard reason", () => {
    assert.equal(resolveRejectReason("Skills do not meet requirements", ""), "Skills do not meet requirements");
  });

  await t.test("trims standard reason", () => {
    assert.equal(resolveRejectReason("  Too expensive  ", ""), "Too expensive");
  });

  await t.test("returns undefined for empty standard reason", () => {
    assert.equal(resolveRejectReason("   ", ""), undefined);
    assert.equal(resolveRejectReason("", ""), undefined);
  });

  await t.test("returns other text when reason is __other__", () => {
    assert.equal(resolveRejectReason("__other__", "Needs more experience"), "Needs more experience");
  });

  await t.test("trims other text", () => {
    assert.equal(resolveRejectReason("__other__", "  Too junior  "), "Too junior");
  });

  await t.test("returns undefined when __other__ has empty text", () => {
    assert.equal(resolveRejectReason("__other__", ""), undefined);
    assert.equal(resolveRejectReason("__other__", "   "), undefined);
  });
});

test("isRejectReasonValid", async (t) => {
  await t.test("returns true for standard reason", () => {
    assert.equal(isRejectReasonValid("Skills do not meet requirements", ""), true);
  });

  await t.test("returns false for empty standard reason", () => {
    assert.equal(isRejectReasonValid("  ", ""), false);
  });

  await t.test("returns true for __other__ with text", () => {
    assert.equal(isRejectReasonValid("__other__", "Needs more experience"), true);
  });

  await t.test("returns false for __other__ with empty text", () => {
    assert.equal(isRejectReasonValid("__other__", "  "), false);
  });
});
