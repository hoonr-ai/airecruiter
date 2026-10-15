// The recruiter-action queue (lib/activity-queue.ts) and the New Relic user id
// it is attributed to (lib/newrelic.ts nrUserIdFor), which must equal the
// server's services/recruiter_activity.recruiter_hash.
import assert from "node:assert/strict";
import { test } from "node:test";

import { CLIENT_ACTIONS, MAX_BATCH, createActivityQueue, type ActivityItem } from "./activity-queue.ts";
import { nrUserIdFor } from "./newrelic.ts";

const tick = () => new Promise((r) => setTimeout(r, 0));

test("nr user id is the server's recruiter_hash (shared vector)", async () => {
  assert.equal(await nrUserIdFor("  Recruiter@Example.com "), "7a3d6dab7de3");
});

test("only client actions queue; a full batch flushes itself", async () => {
  const batches: ActivityItem[][] = [];
  const q = createActivityQueue(async (items) => { batches.push(items); return true; });
  assert.equal(q.push({ action: "feedback_submit" as never }), false);   // server-side only
  for (let i = 0; i < MAX_BATCH - 1; i++) q.push({ action: "candidate_opened", candidate_id: `c${i}` });
  assert.equal(batches.length, 0);
  q.push({ action: "recording_played", attrs: { seconds: 12 } });
  await tick();
  assert.equal(batches.length, 1);
  assert.equal(batches[0].length, MAX_BATCH);
  assert.equal(q.size(), 0);
});

test("a failed periodic flush keeps its batch; a keepalive flush does not wait", async () => {
  let ok = false;
  const sent: number[] = [];
  const q = createActivityQueue(async (items) => { sent.push(items.length); return ok; });
  q.push({ action: "report_exported", attrs: { report: "launch_report" } });
  q.flush(false);
  await tick();
  assert.equal(q.size(), 1);             // back in the queue
  ok = true;
  q.flush(true);
  await tick();
  assert.deepEqual(sent, [1, 1]);
  assert.equal(q.size(), 0);
});

test("CLIENT_ACTIONS mirror the server list", () => {
  assert.ok(CLIENT_ACTIONS.includes("recording_played"));
  assert.ok(!(CLIENT_ACTIONS as readonly string[]).includes("search_run"));
});
