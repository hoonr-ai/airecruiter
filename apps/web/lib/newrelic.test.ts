import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { afterEach, beforeEach, describe, it } from "node:test";
import {
  NR_MAX_ATTRIBUTES,
  NR_MAX_VALUE_LENGTH,
  captureException,
  isPiiKey,
  logStep,
  nrMeasure,
  nrNoticeError,
  nrPageAction,
  nrUserIdFor,
  setNrAppVersion,
  setNrAttribute,
  setNrUser,
  toNrAttributes,
  toNrError,
  toNrValue,
} from "./newrelic.ts";

type Call = { method: string; args: unknown[] };

function fakeAgent(calls: Call[], opts: { throwOn?: string } = {}) {
  const api: Record<string, (...args: unknown[]) => void> = {};
  for (const method of [
    "addPageAction",
    "noticeError",
    "setUserId",
    "setCustomAttribute",
    "setApplicationVersion",
    "measure",
  ]) {
    api[method] = (...args: unknown[]) => {
      if (opts.throwOn === method) throw new Error(`${method} exploded`);
      calls.push({ method, args });
    };
  }
  return api;
}

const g = globalThis as unknown as { window?: unknown };

describe("toNrAttributes (PII filter)", () => {
  it("drops PII keys, including inside arrays and nested objects", () => {
    const attrs = toNrAttributes({
      job_id: "26-23319",
      username: "pat@example.com",
      candidate_name: "Pat Doe",
      Email: "x@y.com",
      phone_number: "5551234567",
      resume_text: "…",
      home_address: "1 Main St",
      linkedin_url: "https://linkedin.com/in/pat",
      skipped_candidates: [
        { candidate_id: "c1", name: "Pat Doe", phone: "555", needsPhone: true },
        { candidate_id: "c2", first_name: "Sam", contact: { email: "s@x.io", source: "kipplo" } },
      ],
    });
    assert.equal(attrs.job_id, "26-23319");
    for (const k of ["username", "candidate_name", "Email", "phone_number", "resume_text", "home_address", "linkedin_url"]) {
      assert.ok(!(k in attrs), `${k} must be dropped`);
    }
    assert.deepEqual(JSON.parse(attrs.skipped_candidates as string), [
      { candidate_id: "c1", needsPhone: true },
      { candidate_id: "c2", contact: { source: "kipplo" } },
    ]);
  });

  it("keeps flags and small counts under PII-looking keys, never phone-sized numbers", () => {
    const attrs = toNrAttributes({
      needsPhone: true,
      phones_found: 3,
      emails_found: 0,
      phone: 5551234567,
      phone_found_by: { kipplo: 2 },
      name: false,
    });
    assert.deepEqual(attrs, { needsPhone: true, phones_found: 3, emails_found: 0, name: false });
  });

  it("redacts email addresses inside string values", () => {
    const attrs = toNrAttributes({
      msg: "lookup failed for pat.doe+x@example.co.uk, retrying",
      changes: [{ field: "recruiterEmails", after: ["a@b.com", "c@d.org"] }],
    });
    assert.equal(attrs.msg, "lookup failed for [email], retrying");
    assert.ok(!(attrs.changes as string).includes("@"));
    assert.ok((attrs.changes as string).includes("recruiterEmails"), "field names are not values");
  });

  it("filters JSON held in a string by the same key rule", () => {
    // logger.warn("engagement.launch.parse_error", { jsonStr }) when a handler throws.
    const attrs = toNrAttributes({
      jsonStr: ' {"type":"done","excluded_candidates":[{"candidate_id":"c1","name":"Pat Doe","reason":"client"}],"emails_found":2} ',
      nested: { raw: '[{"candidate_id":"c2","phone":"555-123-4567","email":"p@x.io"}]' },
    });
    assert.deepEqual(JSON.parse(attrs.jsonStr as string), {
      type: "done",
      excluded_candidates: [{ candidate_id: "c1", reason: "client" }],
      emails_found: 2,
    });
    assert.deepEqual(JSON.parse(JSON.parse(attrs.nested as string).raw), [{ candidate_id: "c2" }]);
  });

  it("redacts PII-keyed values in JSON that does not parse", () => {
    // The SSE parse_error case proper: a truncated / malformed event.
    const attrs = toNrAttributes({
      jsonStr: '{"type":"done","excluded_candidates":[{"candidate_id":"c1","name":"Pat \\"P\\" Doe","phone":5551234567,"phones_found":3},{"candidate_id":"c2","full_name":"Sam Ro',
    });
    const text = attrs.jsonStr as string;
    for (const leak of ["Pat", "Doe", "5551234567", "Sam", "Ro"]) {
      assert.ok(!text.includes(leak), `${leak} leaked: ${text}`);
    }
    assert.ok(text.includes('"candidate_id":"c1"'), text);
    assert.ok(text.includes('"name":"[redacted]"'), text);
    assert.ok(text.includes('"phones_found":3'), "small counts survive");
  });

  it("redacts formatted phone numbers but keeps id-like digit runs", () => {
    const attrs = toNrAttributes({
      cell: "+1 (555) 123-4567",
      note: "call (555) 123-4567 or 555.123.4567 / +44 20 7946 0958",
      candidate_id: "21562841721070",
      jobdiva_id: "462058065251",
      ts: "2026-10-11T12:34:56.789Z",
      job_ref: "26-23319",
      launch_id: "123e4567-e89b-12d3-a456-426614174000",
    });
    assert.equal(attrs.cell, "[phone]");
    assert.equal(attrs.note, "call [phone] or [phone] / [phone]");
    assert.equal(attrs.candidate_id, "21562841721070");
    assert.equal(attrs.jobdiva_id, "462058065251");
    assert.equal(attrs.ts, "2026-10-11T12:34:56.789Z");
    assert.equal(attrs.job_ref, "26-23319");
    assert.equal(attrs.launch_id, "123e4567-e89b-12d3-a456-426614174000");
  });

  it("stays fast on huge strings and objects (redaction scans a bounded prefix)", () => {
    // An unbounded email regex took ~30 s on a 200 KB run without whitespace.
    const blob = `data:application/pdf;base64,${"QUJD".repeat(250_000)}`;
    const huge = `contact pat@example.com ${"x".repeat(1_000_000)}@y`;
    const many = Array.from({ length: 2000 }, (_, i) => ({ candidate_id: `c${i}`, note: `${"a".repeat(1990)}@b` }));
    const started = performance.now();
    const attrs = toNrAttributes({ blob, huge, many });
    assert.ok(performance.now() - started < 1000, `took ${performance.now() - started} ms`);
    assert.ok((attrs.huge as string).startsWith("contact [email] xxx"));
    for (const k of ["blob", "huge", "many"]) {
      assert.equal((attrs[k] as string).length, NR_MAX_VALUE_LENGTH, k);
    }
    assert.ok(!(attrs.many as string).includes("@"));
  });

  it("never lets a cut-off email through where the scan stops", () => {
    // Redactions shrink the text, so what was past the scan limit can land
    // inside the 1000-char value: the cut must not split an email.
    const prefix = `${"a".repeat(40)}@example.com `.repeat(37) + `${"z".repeat(25)} `;
    const email = "pat.doe@example.com";
    // The scan limit (2000) falls at "pat.doe@examp|le.com": no TLD, so the
    // email regex alone would not catch the cut-off half.
    assert.equal(prefix.length + "pat.doe@examp".length, 2000);
    const out = toNrValue(`${prefix}${email} ${"tail ".repeat(400)}`) as string;
    assert.ok(!out.includes("pat.doe"), out);
    assert.ok(out.endsWith(`${"z".repeat(25)}…`), out);
  });

  it("JSON-stringifies objects instead of [object Object], capped at 1000 chars", () => {
    const attrs = toNrAttributes({
      quality: { scored: 3, dropped: 1 },
      ids: ["a", "b"],
      big: { blob: "x".repeat(5000) },
      long: "y".repeat(5000),
    });
    assert.equal(attrs.quality, '{"scored":3,"dropped":1}');
    assert.equal(attrs.ids, '["a","b"]');
    assert.equal((attrs.big as string).length, NR_MAX_VALUE_LENGTH);
    assert.equal((attrs.long as string).length, NR_MAX_VALUE_LENGTH);
  });

  it("keeps primitives, drops null/undefined/NaN/functions", () => {
    const attrs = toNrAttributes({
      n: 3,
      ok: false,
      s: "x",
      nul: null,
      undef: undefined,
      nan: Number.NaN,
      fn: () => 1,
      big: BigInt(7),
    });
    assert.deepEqual(attrs, { n: 3, ok: false, s: "x", big: "7" });
  });

  it("flattens Errors and Dates, survives cycles", () => {
    const cyclic: Record<string, unknown> = { a: 1 };
    cyclic.self = cyclic;
    assert.equal(toNrValue(new TypeError("Failed to fetch")), "TypeError: Failed to fetch");
    assert.equal(toNrValue(new Date("2026-10-11T00:00:00Z")), "2026-10-11T00:00:00.000Z");
    assert.equal(toNrValue(cyclic), '{"a":1,"self":"[circular]"}');
  });

  it("caps the attribute count", () => {
    const many: Record<string, number> = {};
    for (let i = 0; i < 100; i++) many[`k${i}`] = i;
    assert.equal(Object.keys(toNrAttributes(many)).length, NR_MAX_ATTRIBUTES);
  });

  it("flags the PII key families", () => {
    for (const k of ["name", "userName", "email", "phone", "mobile_phone", "resume", "resume_text", "address", "linkedin", "LinkedInUrl"]) {
      assert.ok(isPiiKey(k), k);
    }
    for (const k of ["job_id", "candidate_id", "status", "category", "match_score", "launch_count"]) {
      assert.ok(!isPiiKey(k), k);
    }
  });
});

describe("nrUserIdFor", () => {
  it("is the first 12 hex of sha256(trimmed lowercased email)", async () => {
    const expected = createHash("sha256").update("pat@example.com").digest("hex").slice(0, 12);
    assert.equal(await nrUserIdFor("  Pat@Example.COM "), expected);
    assert.match(expected, /^[0-9a-f]{12}$/);
  });

  it("returns null without an email", async () => {
    assert.equal(await nrUserIdFor(""), null);
    assert.equal(await nrUserIdFor(undefined), null);
    assert.equal(await nrUserIdFor("   "), null);
  });
});

describe("window.newrelic helpers", () => {
  let calls: Call[];
  beforeEach(() => {
    calls = [];
    g.window = { newrelic: fakeAgent(calls) };
  });
  afterEach(() => {
    delete g.window;
  });

  it("are silent no-ops without the agent", async () => {
    g.window = {};
    nrPageAction("x", { a: 1 });
    nrNoticeError(new Error("x"));
    nrMeasure("m");
    setNrAttribute("job_id", "1");
    setNrAppVersion("abc");
    await setNrUser("pat@example.com");
    logStep("s", "failed", { a: 1 });
    captureException(new Error("x"));
    delete g.window;
    nrPageAction("x", { a: 1 });
    await setNrUser("pat@example.com");
    assert.deepEqual(calls, []);
  });

  it("never throw when the agent throws", () => {
    g.window = { newrelic: fakeAgent(calls, { throwOn: "addPageAction" }) };
    assert.doesNotThrow(() => nrPageAction("x", { a: 1 }));
    assert.doesNotThrow(() => logStep("x", "info"));
  });

  it("setNrUser sends only the hash, never the email", async () => {
    await setNrUser("Pat@Example.com");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "setUserId");
    const id = calls[0].args[0] as string;
    assert.equal(id, createHash("sha256").update("pat@example.com").digest("hex").slice(0, 12));
    assert.ok(!JSON.stringify(calls).includes("@"));
  });

  it("setNrUser without an email clears the id (sign-out)", async () => {
    await setNrUser("pat@example.com");
    await setNrUser(null);
    await setNrUser("");
    assert.deepEqual(
      calls.map((c) => c.args[0]),
      [createHash("sha256").update("pat@example.com").digest("hex").slice(0, 12), null, null],
    );
    assert.ok(calls.every((c) => c.method === "setUserId"));
  });

  it("setNrAttribute refuses PII keys and redacts emails", () => {
    setNrAttribute("app_env", "qa");
    setNrAttribute("recruiter_email", "pat@example.com");
    setNrAttribute("note", "ping pat@example.com");
    setNrAttribute("job_id", null);
    assert.deepEqual(calls, [
      { method: "setCustomAttribute", args: ["app_env", "qa"] },
      { method: "setCustomAttribute", args: ["note", "ping [email]"] },
      { method: "setCustomAttribute", args: ["job_id", null] },
    ]);
  });

  it("setNrAppVersion passes the version through", () => {
    setNrAppVersion("9f5a71de");
    setNrAppVersion(undefined);
    assert.deepEqual(calls, [{ method: "setApplicationVersion", args: ["9f5a71de"] }]);
  });

  it("nrPageAction filters attributes", () => {
    nrPageAction("launch_session_stage", { job_id: "j1", stage: "sourcing", name: "Pat" });
    assert.deepEqual(calls, [
      { method: "addPageAction", args: ["launch_session_stage", { job_id: "j1", stage: "sourcing" }] },
    ]);
  });

  it("nrNoticeError wraps non-Errors and filters attributes", () => {
    nrNoticeError("boom", { job_id: "j1", email: "pat@example.com" });
    assert.equal(calls[0].method, "noticeError");
    assert.ok(calls[0].args[0] instanceof Error);
    assert.equal((calls[0].args[0] as Error).message, "boom");
    assert.deepEqual(calls[0].args[1], { job_id: "j1" });
  });

  it("nrNoticeError scrubs the message and the same text in the stack", () => {
    const err = new TypeError("Contact lookup failed for pat@example.com (555) 123-4567");
    nrNoticeError(err, { job_id: "j1" });
    const sent = calls[0].args[0] as Error;
    assert.notEqual(sent, err);
    assert.equal(sent.name, "TypeError");
    assert.equal(sent.message, "Contact lookup failed for [email] [phone]");
    assert.ok(sent.stack?.startsWith("TypeError: Contact lookup failed for [email] [phone]"), sent.stack);
    assert.ok(!sent.stack?.includes("pat@example.com"));
    assert.equal(
      sent.stack?.split("\n").slice(1).join("\n"),
      err.stack?.split("\n").slice(1).join("\n"),
      "frames untouched",
    );
  });

  it("nrNoticeError passes a clean Error through as thrown", () => {
    const err = new Error("Failed to fetch");
    nrNoticeError(err);
    captureException(err);
    assert.equal(calls[0].args[0], err);
    assert.equal(calls[1].args[0], err);
  });

  it("toNrError turns non-Errors into scrubbed messages", () => {
    assert.equal(toNrError({ detail: "no slot", email: "p@x.io" }).message, '{"detail":"no slot"}');
    assert.equal(toNrError("ping pat@example.com").message, "ping [email]");
    assert.equal(toNrError(null).message, "null");
    assert.equal(toNrError(new Error("x".repeat(5000))).message.length, NR_MAX_VALUE_LENGTH);
  });

  it("nrMeasure forwards start/end and filtered attributes", () => {
    nrMeasure("pair_launch_stream", { start: 10, end: 250, attrs: { job_id: "j1", phone: "555" } });
    assert.deepEqual(calls, [
      { method: "measure", args: ["pair_launch_stream", { start: 10, end: 250, customAttributes: { job_id: "j1" } }] },
    ]);
  });

  it("logStep keeps its shape and reports failures as errors", () => {
    logStep("pair_launch_skipped_missing_contacts", "info", {
      username: "pat@example.com",
      job_id: "j1",
      skipped_candidates: [{ candidate_id: "c1", name: "Pat" }],
    });
    logStep("save_batch", "failed", { job_id: "j1" }, "launch");
    assert.deepEqual(calls[0], {
      method: "addPageAction",
      args: [
        "pair_launch_skipped_missing_contacts",
        { status: "info", category: "frontend_step", job_id: "j1", skipped_candidates: '[{"candidate_id":"c1"}]' },
      ],
    });
    assert.deepEqual(calls[1], {
      method: "addPageAction",
      args: ["save_batch", { status: "failed", category: "launch", job_id: "j1" }],
    });
    assert.equal(calls[2].method, "noticeError");
    assert.equal((calls[2].args[0] as Error).message, "Step 'save_batch' failed");
  });

  it("logStep keeps its own status and category over the details'", () => {
    logStep("FrontendLog", "error", { status: 500, category: "x", http_status: 500, msg: "send_bulk.failed" }, "frontend_logger");
    assert.deepEqual(calls[0].args, [
      "FrontendLog",
      { status: "error", category: "frontend_logger", http_status: 500, msg: "send_bulk.failed" },
    ]);
  });
});
