// Who is "the same person" for the launch gate — the browser half of
// apps/api/services/launch_person_keys.py (golden cases shared in
// apps/api/tests/fixtures/launch_person_keys.json; lib/launch-person-keys.test.ts).
//
// candidate_id is source-scoped, not a person: one human can be a JobDiva id,
// exa_<linkedin-url>, a Unipile id and a manual upload at once. The server's
// launch claims refuse another id of a person already launched / in flight for
// the job (same email, phone, LinkedIn or JobDiva id), so Step 5 treats those
// rows as already launched too — hidden from the table and never spending the
// Source & Launch cap on a row the claim would refuse.
//
// Keys: email:<lowercase, placeholders excluded>, phone:<last 10 digits;
// <10 digits or <4 distinct excluded>, linkedin:<the /in/ or /pub/ slug;
// recruiter /talent/ links excluded>, jobdiva:<data.jobdiva_candidate_id, or a
// JobDiva-sourced row's own id>.

/* eslint-disable @typescript-eslint/no-explicit-any */

export const LAUNCHED_PERSON = "launched_person";

const PLACEHOLDER_EMAILS = new Set([
  "your-email@example.com",
  "email@example.com",
  "example@example.com",
  "test@example.com",
  "candidate@example.com",
  "noreply@example.com",
]);
const PLACEHOLDER_DOMAINS = new Set([
  "example.com", "example.org", "example.net", "test.com", "invalid", "localhost", "local",
]);
const PLACEHOLDER_LOCAL_PARTS = new Set(["your-email", "your_email", "email", "test", "example", "candidate"]);
const LINKEDIN_SLUG_RE = /linkedin\.com\/(?:in|pub)\/([^/?#\s]+)/i;
const LINKEDIN_URL_FIELDS = ["profile_url", "linkedin_url", "public_profile_url", "linkedin"] as const;

const text = (v: unknown): string =>
  v === null || v === undefined || typeof v === "boolean" ? "" : String(v).trim();

const isObject = (v: unknown): v is Record<string, any> =>
  Boolean(v) && typeof v === "object" && !Array.isArray(v);

// utils.email_utils.is_placeholder_email (the backend's one rule).
export function isPlaceholderEmail(email: unknown): boolean {
  const e = text(email).toLowerCase();
  if (!e) return true;
  if (PLACEHOLDER_EMAILS.has(e)) return true;
  if (e.endsWith("@noemail.pair.ai")) return true;
  if (!e.includes("@")) return true;
  const at = e.lastIndexOf("@");
  const local = e.slice(0, at);
  const domain = e.slice(at + 1);
  if (PLACEHOLDER_DOMAINS.has(domain)) return true;
  if (domain.endsWith(".local") || domain === "local") return true;
  if (PLACEHOLDER_LOCAL_PARTS.has(local)) return true;
  if (domain === "jobdiva.com") return true;
  return false;
}

export function emailKey(value: unknown): string {
  const e = text(value).toLowerCase();
  return isPlaceholderEmail(e) ? "" : e;
}

export function phoneKey(value: unknown): string {
  const digits = text(value).replace(/\D/g, "");
  if (digits.length < 10 || new Set(digits).size < 4) return "";
  return digits.slice(-10);
}

export function linkedinKey(value: unknown): string {
  const m = LINKEDIN_SLUG_RE.exec(text(value));
  if (!m) return "";
  let slug = m[1];
  try {
    slug = decodeURIComponent(slug);
  } catch {
    // keep the raw slug (Python's unquote never throws)
  }
  return slug.trim().toLowerCase();
}

const jobdivaKey = (value: unknown): string => {
  const v = text(value);
  return /^\d+$/.test(v) ? v : "";
};

export function personKeys(row: any): string[] {
  const data = isObject(row?.data) ? row.data : {};
  const keys: string[] = [];
  const add = (kind: string, value: string) => {
    if (!value) return;
    const key = `${kind}:${value}`;
    if (!keys.includes(key)) keys.push(key);
  };
  add("email", emailKey(row?.email) || emailKey(data.email));
  add("phone", phoneKey(row?.phone) || phoneKey(data.phone));
  const urls = isObject(data.urls) ? data.urls : {};
  for (const raw of [
    ...LINKEDIN_URL_FIELDS.map((f) => row?.[f]),
    ...LINKEDIN_URL_FIELDS.map((f) => data[f]),
    urls.linkedin,
  ]) {
    add("linkedin", linkedinKey(raw));
  }
  add("jobdiva", jobdivaKey(data.jobdiva_candidate_id || row?.jobdiva_candidate_id));
  if (text(row?.source).toLowerCase().startsWith("jobdiva")) add("jobdiva", jobdivaKey(row?.candidate_id));
  return keys;
}

// The id selection keys a pool row by (launch-rules candidate row id).
const rowId = (row: any): string =>
  text(row?.candidate_id) || text(row?.jobdiva_candidate_id) || text(row?.id);

// /jobs/{id}/launched-candidate-keys `person_keys` → Map(person key → holder id).
export function personOwnersFromResponse(raw: unknown): Map<string, string> {
  const out = new Map<string, string>();
  if (!isObject(raw)) return out;
  for (const [key, cid] of Object.entries(raw)) {
    const id = text(cid);
    if (key && id) out.set(key, id);
  }
  return out;
}

// Pool row id → "launched_person" for rows whose person is already held for
// the job by ANOTHER candidate_id. Pass as computeAutoLaunchSelection's
// `blocked` (the server's S2 does the same).
export function launchedPersonBlocks(
  pool: readonly any[],
  owners: ReadonlyMap<string, string>,
): Map<string, string> {
  const out = new Map<string, string>();
  if (owners.size === 0) return out;
  for (const row of pool) {
    const id = rowId(row);
    if (!id || out.has(id)) continue;
    for (const key of personKeys(row)) {
      const other = owners.get(key);
      if (other && other !== id) {
        out.set(id, LAUNCHED_PERSON);
        break;
      }
    }
  }
  return out;
}

// One row: is it another id of a person already held for the job?
export function isLaunchedPerson(row: any, owners: ReadonlyMap<string, string>): boolean {
  if (owners.size === 0) return false;
  const id = rowId(row);
  return personKeys(row).some((key) => {
    const other = owners.get(key);
    return Boolean(other) && other !== id;
  });
}
