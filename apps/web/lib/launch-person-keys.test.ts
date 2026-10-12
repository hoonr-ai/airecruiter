// The browser's person keys must match the server's
// (apps/api/services/launch_person_keys.py) on every golden case, or Step 5
// and the launch claim would disagree on who is already launched.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  isLaunchedPerson,
  launchedPersonBlocks,
  personKeys,
  personOwnersFromResponse,
} from "./launch-person-keys.ts";

const FIXTURE = JSON.parse(
  readFileSync(new URL("../../api/tests/fixtures/launch_person_keys.json", import.meta.url), "utf8"),
);

test("golden person keys match the server", () => {
  for (const c of FIXTURE.keys) {
    assert.deepEqual(personKeys(c.row), c.keys, JSON.stringify(c.row));
  }
});

test("golden launched-person blocks match the server", () => {
  for (const c of FIXTURE.blocks) {
    const owners = personOwnersFromResponse(c.owners);
    assert.deepEqual(Object.fromEntries(launchedPersonBlocks(c.pool, owners)), c.blocked);
  }
});

test("isLaunchedPerson: another id of a held person, never the holder itself", () => {
  const owners = personOwnersFromResponse({ "email:jane@x.com": "exa_jane" });
  assert.equal(isLaunchedPerson({ candidate_id: "4471", email: "Jane@X.com" }, owners), true);
  assert.equal(isLaunchedPerson({ candidate_id: "exa_jane", email: "jane@x.com" }, owners), false);
  assert.equal(isLaunchedPerson({ candidate_id: "bob", email: "bob@x.com" }, owners), false);
  assert.equal(isLaunchedPerson({ candidate_id: "x", email: "jane@x.com" }, new Map()), false);
});

test("personOwnersFromResponse ignores junk", () => {
  assert.equal(personOwnersFromResponse(undefined).size, 0);
  assert.equal(personOwnersFromResponse([]).size, 0);
  assert.deepEqual([...personOwnersFromResponse({ "email:a@b.co": "a", "": "x", "phone:1": "" })], [["email:a@b.co", "a"]]);
});
