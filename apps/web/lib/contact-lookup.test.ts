import assert from "node:assert/strict";
import { test } from "node:test";

import {
  contactProviderLabel,
  describeContactsFound,
  inGroups,
  isKipploIssue,
  mostCommon,
  readNdjson,
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

test("deep-search finds are named apart from the normal Exa run", () => {
  assert.equal(
    describeContactsFound({ exa: 2, exa_deep: 1, apollo: 1 }, {}),
    "4 phones (Exa 2, Apollo 1, Exa deep search 1)",
  );
  assert.equal(contactProviderLabel("exa_deep"), "Exa deep search");
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

// Hands out one chunk per read, then closes (or fails, like a dropped connection).
function streamOf(chunks: string[], failAfter = false): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  const queue = [...chunks];
  return new ReadableStream({
    pull(controller) {
      const next = queue.shift();
      if (next !== undefined) controller.enqueue(encoder.encode(next));
      else if (failAfter) controller.error(new TypeError("network error"));
      else controller.close();
    },
  });
}

test("streamed lookup lines are handed over one by one, split anywhere", async () => {
  const seen: unknown[] = [];
  await readNdjson(
    streamOf(['{"index": 1, "res', 'ult": {"phone": "+1"}}\n{"ping": true}\n\n', 'not json\n{"done": true}']),
    (v) => seen.push(v),
  );
  assert.deepEqual(seen, [{ index: 1, result: { phone: "+1" } }, { ping: true }, { done: true }]);
});

test("a broken stream rejects after delivering what came before", async () => {
  const seen: unknown[] = [];
  await assert.rejects(readNdjson(streamOf(['{"index": 0, "result": {}}\n'], true), (v) => seen.push(v)));
  assert.deepEqual(seen, [{ index: 0, result: {} }]);
});

