import assert from "node:assert/strict";
import { test } from "node:test";

import {
  contactProviderLabel,
  describeContactsFound,
  inGroups,
  isKipploIssue,
  mostCommon,
} from "./contact-lookup.ts";

test("groups candidates for one call each, the last group shorter", () => {
  assert.deepEqual(inGroups([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);
  assert.deepEqual(inGroups([1, 2, 3], 10), [[1, 2, 3]]);
  assert.deepEqual(inGroups([], 10), []);
  assert.deepEqual(inGroups([1, 2], 0), [[1], [2]]);
});

test("a Kipplo result is not an issue; anything else Kipplo said is", () => {
  for (const outcome of ["-", "miss", "phone", "email", "email,phone", "", undefined, null]) {
    assert.equal(isKipploIssue(outcome), false, String(outcome));
  }
  for (const outcome of [
    "Kipplo not configured",
    "Kipplo out of credits",
    "Kipplo unavailable: out of credits",
    "Kipplo rate limited",
    "Kipplo API key rejected",
  ]) {
    assert.equal(isKipploIssue(outcome), true, outcome);
  }
});

test("names who found the phones and emails, most first", () => {
  assert.equal(
    describeContactsFound({ exa: 1, kipplo: 2 }, { exa: 1 }),
    "3 phones (Kipplo 2, Exa 1) and 1 email (Exa 1)",
  );
  assert.equal(describeContactsFound({ kipplo: 1 }, {}), "1 phone (Kipplo 1)");
  assert.equal(describeContactsFound({}, { cache: 2, zoominfo: 0 }), "2 emails (saved 2)");
  assert.equal(describeContactsFound({}, {}), "");
});

test("unknown providers keep their own name", () => {
  assert.equal(contactProviderLabel("other"), "other");
  assert.equal(contactProviderLabel("zoominfo"), "ZoomInfo");
  assert.equal(contactProviderLabel(undefined), "");
});

test("most common issue, ties alphabetical", () => {
  assert.deepEqual(mostCommon({ "Kipplo rate limited": 2, "Kipplo out of credits": 5 }), {
    key: "Kipplo out of credits",
    count: 5,
  });
  assert.deepEqual(mostCommon({ b: 1, a: 1 }), { key: "a", count: 1 });
  assert.equal(mostCommon({}), null);
});
