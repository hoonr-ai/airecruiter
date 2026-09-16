import assert from "node:assert/strict";
import { test } from "node:test";

import {
  isProvisionedTwinOf,
  jobDivaLinkage,
  mergeProvisionedTwins,
  stampedJobDivaId,
  type TwinRow,
} from "./candidateTwins.ts";

type Row = TwinRow & { email?: string; engage_status?: string };

const origin: Row = {
  candidate_id: "exa_linkedin.com/in/ada",
  source: "LinkedIn-Exa",
  email: "ada@work.example.com",
  data: { jobdiva_candidate_id: "462058065251", jobdiva_application_origin: "pair", match_score: 84 },
};
const twin: Row = {
  candidate_id: "462058065251",
  source: "JobDiva-Applicants",
  email: "ada@home.example.com",
  engage_status: "sent",
  data: { jobdiva_candidate_id: "462058065251", engage_status: "sent" },
};
const organic: Row = { candidate_id: "777", source: "JobDiva-Applicants", email: "bob@example.com", data: {} };

test("stamp is read from top level or data", () => {
  assert.equal(stampedJobDivaId(origin), "462058065251");
  assert.equal(stampedJobDivaId({ jobdiva_candidate_id: "1" }), "1");
  assert.equal(stampedJobDivaId({}), "");
});

test("a JobDiva-labelled row whose id is the origin's stamp is its twin", () => {
  assert.equal(isProvisionedTwinOf(twin, origin), true);
  // not the other way round, and never between two JobDiva rows
  assert.equal(isProvisionedTwinOf(origin, twin), false);
  assert.equal(isProvisionedTwinOf(organic, twin), false);
  // a different profile id is a different person
  assert.equal(isProvisionedTwinOf({ ...twin, candidate_id: "999" }, origin), false);
});

test("mergeProvisionedTwins folds the twin into the origin and keeps the origin label", () => {
  const folds: string[] = [];
  const out = mergeProvisionedTwins<Row>([twin, origin, organic], (o, t) => {
    folds.push(`${o.candidate_id}<-${t.candidate_id}`);
    return { ...o, engage_status: o.engage_status || t.engage_status };
  });
  assert.deepEqual(folds, ["exa_linkedin.com/in/ada<-462058065251"]);
  assert.equal(out.length, 2);
  assert.equal(out[0].candidate_id, "exa_linkedin.com/in/ada");
  assert.equal(out[0].source, "LinkedIn-Exa");
  assert.equal(out[0].engage_status, "sent");
  assert.equal(out[1].candidate_id, "777");
});

test("rows without a twin pass through unchanged, in order", () => {
  const rows: Row[] = [organic, { candidate_id: "exa_x", source: "LinkedIn-Exa", data: {} }];
  const out = mergeProvisionedTwins(rows, (o) => o);
  assert.deepEqual(out, rows);
});

test("linkage caption distinguishes PAIR-filed from organic applications", () => {
  assert.equal(jobDivaLinkage(origin).caption, "In JobDiva · via PAIR");
  assert.equal(
    jobDivaLinkage({ ...origin, data: { ...origin.data, jobdiva_application_origin: "organic" } }).caption,
    "In JobDiva · applied directly",
  );
  assert.equal(
    jobDivaLinkage({ ...origin, data: { jobdiva_candidate_id: "1" } }).caption,
    "In JobDiva",
  );
  // JobDiva-labelled rows say it with their label already; unlinked rows say nothing.
  assert.equal(jobDivaLinkage(organic).caption, "");
  assert.equal(jobDivaLinkage({ candidate_id: "exa_y", source: "LinkedIn-Exa" }).caption, "");
});
