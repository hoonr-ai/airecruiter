import assert from "node:assert/strict";
import { test } from "node:test";

import { formatTimecode } from "./timecode.ts";

test("formats minutes and seconds", () => {
  assert.equal(formatTimecode(0), "0:00");
  assert.equal(formatTimecode(125.9), "2:05");
});

test("switches to hours from one hour", () => {
  assert.equal(formatTimecode(3599), "59:59");
  assert.equal(formatTimecode(4500), "1:15:00");
});

test("clamps negative and non-finite input", () => {
  assert.equal(formatTimecode(-5), "0:00");
  assert.equal(formatTimecode(Number.NaN), "0:00");
});
