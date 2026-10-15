/* eslint-disable @typescript-eslint/no-explicit-any -- candidate rows are untyped JSON from many sources */
// Step-5 Source & Launch rules, extracted from app/jobs/new/page.tsx so the
// browser and the server-side launch runner apply the SAME rules: contact
// validity, exclusion reasons, the hiring-client conflict, cross-source
// de-dup, the auto-launch selection, who gets a contact lookup, the
// ready / needs-contact split, the /candidates/save payload, and the search
// stream parser.
//
// Pure functions only: every input is an argument (no React state). The
// Python port is apps/api/services/launch_sessions/rules.py. Golden fixtures
// shared by both live in apps/api/tests/fixtures/launch_rules/ and are checked
// by lib/launch-rules.test.ts (node --test) and
// apps/api/tests/test_launch_rules_parity.py (pytest). Changing this file
// means regenerating the fixtures (see launch-rules.test.ts) and porting the
// change to rules.py. The regeneration also records this file's sha256 in
// the fixtures' _source.json, and pytest (the CI job that gates deploys)
// fails when that fingerprint is stale — so an edit here that skipped the
// regeneration, or a regenerated rule that was not ported, fails test-api
// even where CI does not run the node suite.
//
// No value imports from sibling modules: node --test loads this file directly
// and the Next tsconfig does not allow the explicit ".ts" specifiers that
// would need. normalizeDncPhone / isLaunchEligibleScore are copies of
// lib/phone.ts normalizePhone / lib/match-score.ts isOutreachEligibleScore;
// the test asserts they agree.

// ── Constants ───────────────────────────────────────────────────────────────

// "No outreach for less than 60%" (lib/match-score OUTREACH_MIN_SCORE).
export const LAUNCH_MIN_SCORE = 60;
// Source & Launch footer count: default and hard ceiling.
export const AUTO_LAUNCH_DEFAULT_COUNT = 250;
export const AUTO_LAUNCH_MAX_COUNT = 1000;
// "Qualified" for the boolean-relaxation loop = clears the outreach floor.
export const QUALIFIED_SCORE_THRESHOLD = LAUNCH_MIN_SCORE;
export const QUALIFIED_TARGET_COUNT = 50;
// Launch contact lookups: groups of this size per /candidates/enrich-contacts
// call, this many calls at a time (20 candidates in flight).
export const LAUNCH_ENRICH_GROUP_SIZE = 5;
export const LAUNCH_ENRICH_GROUP_CONCURRENCY = 4;

export type ContactOverride = { phone?: string; email?: string };
export type ContactOverrides = Record<string, ContactOverride>;
// The job fields the hiring-client rules read (monitored job / jobData).
export type ClientJob = { customer_name?: unknown; customer?: unknown } | null | undefined;

// ── Contact validity (page.tsx 134-209) ─────────────────────────────────────

export const LAUNCH_EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
export const PLACEHOLDER_LAUNCH_EMAILS = new Set([
  "your-email@example.com",
  "email@example.com",
  "example@example.com",
  "test@example.com",
  "candidate@example.com",
  "noreply@example.com",
]);

export function isValidLaunchEmail(value: string | null | undefined): boolean {
  const email = String(value || "").trim().toLowerCase();
  if (!email || !LAUNCH_EMAIL_RE.test(email)) return false;
  if (PLACEHOLDER_LAUNCH_EMAILS.has(email)) return false;
  if (email.endsWith("@example.com")) return false;
  if (email.endsWith("@noemail.pair.ai")) return false;
  // JobDiva auto-generates Auto_<id>@jobdiva.com for candidates with no real
  // email — dead addresses the backend blanks (engagement.py _is_placeholder_email).
  // Treat them as no-email here too so the launch gate matches the backend: a
  // synthetic-email-only candidate then routes to enrichment / manual entry
  // instead of slipping into the payload and failing the whole batch with a 400.
  if (email.endsWith("@jobdiva.com")) return false;
  return true;
}

export function launchPhoneDigits(value: string | null | undefined): string {
  return String(value || "").replace(/\D/g, "");
}

export function isValidLaunchPhone(value: string | null | undefined): boolean {
  return launchPhoneDigits(value).length >= 7;
}

// Copy of lib/phone.ts normalizePhone (DNC matching; mirrors apps/api/utils/phone.py).
export function normalizeDncPhone(raw: string | null | undefined): string | null {
  if (raw === null || raw === undefined) return null;
  const digits = String(raw).replace(/\D/g, "");
  if (!digits) return null;
  if (digits.length === 10) return "1" + digits;
  if (digits.length === 11 && digits.startsWith("1")) return digits;
  return null;
}

// Defensive phone guard for streamed patches: the backend already makes
// CandidatesDetail/hydration phone patches upgrade-only (mobile-preferred), so
// here we only block the obvious downgrade — replacing a valid number with an
// empty/invalid one. Mobile-vs-home is decided server-side (slot/type known).
export function betterPhoneUI(current: string | null | undefined, incoming: string | null | undefined): string {
  const cur = String(current || "").trim();
  const inc = String(incoming || "").trim();
  if (!inc) return cur;
  if (normalizeDncPhone(inc) === null && normalizeDncPhone(cur) !== null) return cur;
  return inc;
}

export function getCandidateLaunchEmail(candidate: any): string {
  return String(
    candidate?.email ||
    candidate?.workEmail ||
    candidate?.personalEmail ||
    candidate?.enhanced_info?.email ||
    candidate?.enhanced_info?.workEmail ||
    candidate?.enhanced_info?.personalEmail ||
    candidate?.data?.email ||
    candidate?.data?.workEmail ||
    candidate?.data?.personalEmail ||
    candidate?.data?.enhanced_info?.email ||
    candidate?.data?.enhanced_info?.workEmail ||
    candidate?.data?.enhanced_info?.personalEmail ||
    candidate?.data?.zoominfo_contact_enrichment?.workEmail ||
    candidate?.data?.zoominfo_contact_enrichment?.personalEmail ||
    ""
  ).trim().toLowerCase();
}

// The phone PAIR calls: the candidate's personal number only. Work lines
// (workPhone, or a phone an earlier lookup saved from one) are never used, so
// such a candidate is enriched at launch to find their own number.
export function getCandidateLaunchPhone(candidate: any): string {
  const enrich = candidate?.data?.zoominfo_contact_enrichment || {};
  const digits = (value: any) => String(value || "").replace(/\D/g, "");
  const savedWork = digits(enrich.workPhone);
  const savedMobile = digits(enrich.mobilePhone);
  const isSavedWorkNumber = (value: any) =>
    !!savedWork && digits(value) === savedWork && digits(value) !== savedMobile;
  const options = [
    candidate?.phone,
    candidate?.mobilePhone,
    candidate?.enhanced_info?.phone,
    candidate?.enhanced_info?.mobilePhone,
    candidate?.data?.phone,
    candidate?.data?.mobilePhone,
    candidate?.data?.enhanced_info?.phone,
    candidate?.data?.enhanced_info?.mobilePhone,
    enrich.mobilePhone,
  ];
  return String(options.find(value => String(value || "").trim() && !isSavedWorkNumber(value)) || "").trim();
}

// PAIR launches on phone OR email: reachable on the candidate's own contact
// (page.tsx 8776-8778).
export function isLaunchableNow(candidate: any): boolean {
  return (
    isValidLaunchPhone(getCandidateLaunchPhone(candidate)) ||
    isValidLaunchEmail(getCandidateLaunchEmail(candidate))
  );
}

// ── Display name (page.tsx 566-591) ─────────────────────────────────────────

// Placeholder names sources give a row with no name.
const ANONYMOUS_CANDIDATE_NAMES = ["linkedin candidate", "professional candidate", "unknown candidate", "unknown"];

export const getCandidateDisplayName = (candidate: {
  name?: string;
  firstName?: string;
  lastName?: string;
  title?: string;
  source?: string;
}) => {
  const normalize = (value?: string) => {
    const cleaned = (value || "").replace(/\s+/g, " ").trim();
    if (!cleaned) return "";
    const lowered = cleaned.toLowerCase();
    if (ANONYMOUS_CANDIDATE_NAMES.includes(lowered)) {
      return "";
    }
    return cleaned;
  };

  const fullName = normalize(candidate.name);
  if (fullName) return fullName;

  const composed = normalize([candidate.firstName, candidate.lastName].filter(Boolean).join(" "));
  if (composed) return composed;

  const title = normalize(candidate.title);
  if (title) return title;

  return candidate.source === "LinkedIn" ? "LinkedIn profile" : "Unnamed candidate";
};

// ── Cross-source de-dup (page.tsx 593-756) ──────────────────────────────────

// Extracted UI dedupe logic using email OR phone
// Client-side cross-source de-dup. POLICY: a JobDiva row is never the dropped
// side of a collision — when the same person appears from JobDiva and another
// source we MERGE the best info into one surviving row and keep JobDiva's
// identity (so it stays Launch-PAIR-actionable). Matches only on STRONG
// identity (real email / phone+name / LinkedIn URL) so two different people
// sharing a common name+city are never falsely merged.
export const isPlaceholderEmailUI = (e?: string) => {
  const n = String(e || "").trim().toLowerCase();
  if (!n || !n.includes("@")) return true;
  const domain = n.split("@").pop() || "";
  if (domain === "jobdiva.com") return true;
  if (n.endsWith("@noemail.pair.ai")) return true;
  return false;
};
export const candIsJobDivaUI = (c: any) => {
  const s = String(c?.source || "").toLowerCase();
  if (s.startsWith("jobdiva")) return true;
  const srcs = Array.isArray(c?.sources) ? c.sources : [];
  if (srcs.some((x: any) => String(x || "").toLowerCase().startsWith("jobdiva"))) return true;
  return Boolean(String(c?.jobdiva_candidate_id || c?.jobdiva_id || c?.data?.jobdiva_candidate_id || "").trim());
};
export const deduplicateCandidatesUI = (candidatesList: any[]) => {
  const normalizeEmail = (e?: string) => String(e || "").trim().toLowerCase();
  const normalizePhoneValue = (p?: string) => {
    const digits = String(p || "").replace(/\D/g, "");
    if (digits.length < 7) return "";
    // A shared/placeholder line (e.g. 000-000-0000, 555-555-5555) must not
    // collapse a whole agency's candidates into one row.
    if (new Set(digits.split("")).size < 4) return "";
    return digits.slice(-10);
  };
  const getName = (c: any) => {
    const f = String(c.firstName || "").toLowerCase().trim();
    const l = String(c.lastName || "").toLowerCase().trim();
    const full = `${f} ${l}`.trim();
    return full || String(c.name || "").toLowerCase().trim();
  };
  const getEmail = (c: any) => {
    const e = normalizeEmail(c.email || c.data?.email || c.enhanced_info?.email);
    return e && !isPlaceholderEmailUI(e) ? e : "";
  };
  const getPhone = (c: any) => normalizePhoneValue(c.phone || c.data?.phone || c.enhanced_info?.phone);
  const getLinkedin = (c: any) => {
    const u = String(c.profile_url || c.data?.urls?.linkedin || c.linkedin_url || "").trim().toLowerCase();
    return u.includes("linkedin.com") ? u.split("?")[0].replace(/\/+$/, "") : "";
  };

  const keysOf = (c: any): string[] => {
    const keys: string[] = [];
    const email = getEmail(c);
    if (email) keys.push(`email:${email}`);
    const phone = getPhone(c);
    const name = getName(c);
    if (phone && name && name.includes(" ")) keys.push(`phone-name:${phone}|${name}`);
    const li = getLinkedin(c);
    if (li) keys.push(`linkedin:${li}`);
    return keys;
  };

  const mergeBestOf = (dst: any, src: any) => {
    const srcList = (c: any) => {
      const out: string[] = [];
      if (Array.isArray(c.sources)) out.push(...c.sources.filter(Boolean).map(String));
      if (c.source) out.push(String(c.source));
      return out;
    };
    const merged = Array.from(new Set([...srcList(dst), ...srcList(src)]));
    if (merged.length) dst.sources = merged;
    for (const f of ["phone", "location", "city", "state", "title", "headline",
      "resume_id", "profile_url", "linkedin_url", "image_url", "experience_years",
      "jobdiva_candidate_id", "jobdiva_id"]) {
      if (!dst[f] && src[f]) dst[f] = src[f];
    }
    // No-contact flag is sticky-true across merges: if EITHER row of the same
    // person is flagged, the survivor stays flagged (dropping the flag here
    // would silently re-enable actions on a blocked candidate).
    if (src.no_contact === true && dst.no_contact !== true) {
      dst.no_contact = true;
      dst.no_contact_reason = src.no_contact_reason;
      dst.no_contact_company = src.no_contact_company;
    }
    // Hiring-client conflict is sticky-true for the same reason: one source
    // may carry the employment history that another lacks, and the merged row
    // must keep the block.
    if (src.client_conflict === true && dst.client_conflict !== true) {
      dst.client_conflict = true;
      dst.client_conflict_reason = src.client_conflict_reason;
      dst.client_conflict_company = src.client_conflict_company;
      dst.client_conflict_relation = src.client_conflict_relation;
    }
    const dEmail = String(dst.email || "");
    const sEmail = String(src.email || "");
    if (sEmail && sEmail !== dEmail && (!dEmail || (isPlaceholderEmailUI(dEmail) && !isPlaceholderEmailUI(sEmail)))) {
      dst.email = sEmail;
    }
    const badResume = (r: any) => { const s = String(r || ""); return !s.trim() || s.includes("Resume content unavailable"); };
    const dR = String(dst.resume_text || "");
    const sR = String(src.resume_text || "");
    if (sR && !badResume(sR) && (badResume(dR) || sR.length > dR.length)) dst.resume_text = src.resume_text;
    return dst;
  };

  const getPrio = (s?: string) => {
    const l = String(s || "").toLowerCase();
    if (l.includes("applicant")) return 1;
    if (l.includes("talentsearch")) return 2;
    return 3;
  };

  let uniqueResults: any[] = [];

  for (const cand of candidatesList) {
    const cKeys = keysOf(cand);
    if (cKeys.length === 0) {
      uniqueResults.push(cand);
      continue;
    }

    // Find ALL existing rows that share a strong identity key with cand.
    const matchIndices: number[] = [];
    for (let i = 0; i < uniqueResults.length; i++) {
      const eKeys = keysOf(uniqueResults[i]);
      if (cKeys.some(k => eKeys.includes(k))) matchIndices.push(i);
    }

    if (matchIndices.length === 0) {
      uniqueResults.push(cand);
      continue;
    }

    // Same person across rows: pick ONE survivor, then fold everyone else's
    // best info into it. JobDiva-bearing record always wins the survivor slot
    // (never dropped); otherwise prefer has-both-contacts, then source
    // priority, then match_score.
    const competitors = [cand, ...matchIndices.map(i => uniqueResults[i])];
    let winner = competitors[0];
    for (let i = 1; i < competitors.length; i++) {
      const comp = competitors[i];
      const wJd = candIsJobDivaUI(winner);
      const cJd = candIsJobDivaUI(comp);
      if (cJd !== wJd) { if (cJd) winner = comp; continue; }
      const wBoth = Boolean(getEmail(winner)) && Boolean(getPhone(winner));
      const cBoth = Boolean(getEmail(comp)) && Boolean(getPhone(comp));
      if (cBoth !== wBoth) { if (cBoth) winner = comp; continue; }
      const wPrio = getPrio(winner.source);
      const cPrio = getPrio(comp.source);
      if (cPrio !== wPrio) { if (cPrio < wPrio) winner = comp; continue; }
      if (Number(comp.match_score || 0) > Number(winner.match_score || 0)) winner = comp;
    }

    // Clone the survivor so we never mutate an object still held in React state,
    // then absorb best-of from every other competitor.
    const survivor = { ...winner };
    for (const comp of competitors) {
      if (comp !== winner) mergeBestOf(survivor, comp);
    }

    uniqueResults = uniqueResults.filter((_, idx) => !matchIndices.includes(idx));
    uniqueResults.push(survivor);
  }
  return uniqueResults;
};

// ── Current employer / exclusion / hiring-client conflict (page.tsx 1553-1761) ─

// Launch-PAIR skip policy: the ONLY reason to skip a selected candidate is
// that they CURRENTLY work at the hiring client company — we don't poach from
// the client. Everyone else launches, regardless of match score, location, or
// how little we could extract about them. ("Not enough info" is no longer a
// failing criterion — a thin/empty profile is launchable, not skipped.)
//
// Normalize a company name for comparison: lowercase, strip punctuation and
// common legal/filler suffixes so "Bank of America, N.A." ~ "Bank of America".
export const normalizeCompanyName = (s: any): string =>
  String(s || "")
    .toLowerCase()
    .replace(/[.,]/g, " ")
    .replace(/\b(inc|llc|ltd|corp|corporation|co|company|plc|gmbh|na|pvt|private|limited|technologies|technology|solutions|consulting|services|group|holdings)\b/g, " ")
    .replace(/[^a-z0-9 ]/g, " ")
    .replace(/\s+/g, " ")
    .trim();

// Best-effort CURRENT company from a LinkedIn-style headline ("Engineer at
// Meta | ex-Amazon" → "Meta"). Unipile/Exa search rows carry the employer
// only in headline text. Uses the LAST "at X"/"@ X" occurrence (current
// employer is named last in "Ex-Google | Engineer at Stripe").
export const extractCompanyFromHeadline = (headline: any): string => {
  const text = String(headline || "").trim();
  if (!text || text.length > 300) return "";
  const matches = [...text.matchAll(/(?:\s+at\s+|\s+@\s*)([^|,;•·(–—]+)/gi)];
  if (!matches.length) return "";
  let company = String(matches[matches.length - 1][1] || "").trim();
  company = company.replace(/^the\s+/i, "").replace(/[\s.!\-–—]+$/g, "");
  if (!company || company.split(/\s+/).length > 6) return "";
  return company;
};

// Every signal of the candidate's CURRENT employer (present role only).
// Past employers must NOT count — we only skip people who literally work
// at the client today. Mirrors the backend collector
// (apps/api/services/company_match.py) across all source shapes: flat
// fields, current company_experience entries, Exa deep-search
// exa_recent_companies, and the headline parse.
export const getCandidateCurrentCompanies = (c: any): string[] => {
  const ei = c?.enhanced_info || c?.data?.enhanced_info || {};
  const data = c?.data || {};
  const out: string[] = [];
  for (const direct of [
    c?.current_company, ei?.current_company, data?.current_company,
    c?.company, c?.company_name, ei?.company,
  ]) {
    if (direct && String(direct).trim()) out.push(String(direct).trim());
  }
  const expLists = [
    (Array.isArray(c?.company_experience) && c.company_experience) ||
      (Array.isArray(ei?.company_experience) && ei.company_experience) ||
      (Array.isArray(data?.company_experience) && data.company_experience) || [],
    (Array.isArray(c?.exa_recent_companies) && c.exa_recent_companies) ||
      (Array.isArray(data?.exa_recent_companies) && data.exa_recent_companies) || [],
  ];
  for (const exp of expLists) {
    for (const e of exp) {
      if (!e || typeof e !== "object") continue;
      const endRaw = String(e.end_date ?? e.endDate ?? e.to ?? e.end ?? "").trim();
      const isCurrent = e.is_current === true || e.current === true || !endRaw || /present|current/i.test(endRaw);
      const comp = String(e.company || e.company_name || e.employer || e.name || "").trim();
      if (isCurrent && comp) out.push(comp);
    }
  }
  for (const headline of [c?.headline, c?.title, data?.headline, data?.title]) {
    const parsed = extractCompanyFromHeadline(headline);
    if (parsed) out.push(parsed);
  }
  return [...new Set(out)];
};

// `job` is the monitored job (jobData): its customer_name / customer is the
// hiring client.
export const isEmployedByClient = (c: any, job: ClientJob): boolean => {
  const client = normalizeCompanyName(job?.customer_name || job?.customer || "");
  // "external" is the placeholder for non-JobDiva reqs — never a real client.
  if (!client || client === "external") return false;
  for (const raw of getCandidateCurrentCompanies(c)) {
    const cand = normalizeCompanyName(raw);
    if (!cand) continue;
    if (cand === client) return true;
    // Substring match only when the shorter name is specific enough (>= 4 chars)
    // to avoid false hits on generic tokens left after normalization.
    const shorter = cand.length <= client.length ? cand : client;
    if (shorter.length >= 4 && (cand.includes(client) || client.includes(cand))) return true;
  }
  return false;
};

export const getCandidateExclusionReason = (c: any): string => {
  if (!c) return "";
  const ei = c?.enhanced_info || c?.data?.enhanced_info || {};
  const data = c?.data || c;

  const offerStatus = String(c?.offer_status || data?.offer_status || "");
  if (offerStatus.toLowerCase().includes("offer accepted")) return "Offer Accepted";
  if (offerStatus.toLowerCase().includes("offer extended")) return "Offer Extended";

  const actionTypes = c?.action_types || data?.action_types || [];
  if (Array.isArray(actionTypes)) {
    for (const at of actionTypes) {
      const atLow = String(at).toLowerCase();
      if (atLow.includes("offer accepted") || ["placed", "hired"].includes(atLow)) return "Offer Accepted";
      if (atLow.includes("offer extended")) return "Offer Extended";
    }
  }

  if (
    c?.available === false ||
    data?.available === false ||
    String(c?.available).toLowerCase() === "false" ||
    String(data?.available).toLowerCase() === "false"
  ) {
    return "Current Employee (Pyramid / Unavailable)";
  }

  const quals = data?.qualifications || c?.qualifications || ei?.qualifications || c?.data?.qualifications || [];
  if (Array.isArray(quals)) {
    for (const q of quals) {
      if (!q || typeof q !== "object") continue;
      // Check both camelCase (from some sources) and UPPERCASE (from BI endpoint)
      const qVal = `${q.qualificationValue || ''} ${q.QUALVALUE || ''} ${q.value || ''} ${q.name || ''} ${q.QUALNAME || ''}`.toLowerCase();
      if (qVal.includes("current employee")) return "Current Employee (Pyramid)";
      if (qVal.includes("offer extended") || qVal.includes("offer - extended")) return "Offer Extended";
      if (qVal.includes("offer accepted") || qVal.includes("offer - accepted") || ["placed", "hired"].includes(qVal)) {
        return "Offer Accepted";
      }
    }
  }

  // JobDiva API: status=1 means Current Employee, status=2 means Past Employee
  const numStatus = c?.status ?? data?.status;
  if (numStatus === 1 || numStatus === "1") return "Current Employee (Pyramid)";

  const statusStrs = [
    String(c?.employee_status || ""),
    String(data?.employee_status || ""),
    String(c?.available || ""),
    String(data?.available || ""),
    String(c?.availability_status || ""),
    String(data?.availability_status || ""),
  ];
  for (const st of statusStrs) {
    if (!st) continue;
    const stLower = st.toLowerCase();
    if (stLower.includes("offer extended") || stLower.includes("offer - extended")) return "Offer Extended";
    if (
      stLower.includes("offer accepted") ||
      stLower.includes("offer - accepted") ||
      ["placed", "hired"].includes(stLower)
    ) {
      return "Offer Accepted";
    }
    if (stLower.includes("current employee")) return "Current Employee (Pyramid)";
  }

  for (const raw of getCandidateCurrentCompanies(c)) {
    const currComp = normalizeCompanyName(raw);
    if (currComp && currComp.includes("pyramid")) {
      return "Current Employee (Pyramid)";
    }
  }

  // NOTE: "employed by the hiring client" is deliberately NOT returned here.
  // This helper also drives row VISIBILITY (see the sortedCandidates filter),
  // and hiding those rows meant a recruiter never saw that the person works
  // at the client — they simply vanished from the list. They now stay
  // visible, greyed out behind the backend-stamped `client_conflict` flag,
  // and are filtered out of the save/launch paths separately, exactly as
  // `no_contact` already is.

  return "";
};

// Client-side fallback for the backend `client_conflict` flag, so rows
// sourced before the flag existed still render greyed out. Narrower than the
// backend rule (current employer only — it does not do the "no current
// employer on file, fall back to the most recent one" step), so it can only
// under-flag, never over-flag.
export const withClientConflictFlag = <T extends Record<string, unknown>>(c: T, job: ClientJob): T => {
  if (!c || c.client_conflict !== undefined) return c;
  if (!isEmployedByClient(c, job)) return c;
  return {
    ...c,
    client_conflict: true,
    client_conflict_relation: "current",
    client_conflict_reason: `Current employer is the hiring client (${
      job?.customer_name || job?.customer || "client"
    })`,
  };
};

// ── Auto-launch selection (page.tsx 1221, 6315, 7071-7121) ──────────────────

// Copy of lib/match-score isOutreachEligibleScore. Unscored (N/A) rows never
// clear the floor — "no outreach below 60%" needs a number to compare against.
export function isLaunchEligibleScore(score: unknown): boolean {
  return typeof score === "number" && Number.isFinite(score) && score >= LAUNCH_MIN_SCORE;
}

export const clampLaunchCount = (n: number) => Math.min(AUTO_LAUNCH_MAX_COUNT, Math.max(1, Math.floor(n)));

// The boolean-relaxation loop's "qualified" count (page.tsx countQualified).
// handleRunSearch runs it on `accumulated`: every attempt's runList
// concatenated, i.e. each attempt's rows with THAT attempt's patches. It
// counts excluded / no-contact / already-launched rows, rows a later attempt
// dropped, and string scores ("75" counts: `>=` coerces).
export const countQualified = (list: any[]) =>
  list.filter(c => (c.match_score || 0) >= QUALIFIED_SCORE_THRESHOLD).length;

// The id a selection / launch / split keys a row by.
export const candidateRowId = (c: any): string =>
  String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();

// Which rows handleSourceAndLaunch selects from after a full search
// (page.tsx: `candidatesRef.current.length >= results.length ? table : results`).
// `table` = the de-duplicated Step-5 rows (last patch wins, sticky
// no_contact / client_conflict); `accumulated` = every attempt's runList.
// The table is shorter whenever a cross-source de-dup merge or a
// later-attempt `dropped` happened, and the selection then reads the
// per-attempt rows: each row's score from the attempt that streamed it
// (not a later relaxed `scored` patch), and both copies of a merged person.
// Only ids still in the table go on to the contact lookup, the split and the
// save (runLaunchPair filters the table by the selected ids), so a selected
// id that was merged away launches nobody.
export function chooseSelectionPool<T>(table: T[], accumulated: T[]): T[] {
  return table.length >= accumulated.length ? table : accumulated;
}

export type AutoLaunchSkip = { id: string; reason: string };
export type AutoLaunchSelection = {
  ids: string[];
  eligibleTotal: number;
  belowFloor: number;
  unscored: number;
  // Only with `withReasons`: every row with an id that was not selected, in
  // pool order, then the over-the-cap rows in score order. Reasons:
  // "duplicate_id", "exclusion:<getCandidateExclusionReason>", "no_contact",
  // "launched", the caller's `blocked` reason, "unscored", "below_floor",
  // "over_limit". Rows with no id are skipped without an entry.
  skipped?: AutoLaunchSkip[];
};

// Auto-launch selection for Source & Launch: every candidate that is
// actually launchable — not excluded (client employee / offer status), not
// a no-contact company, not already launched — AND scoring at or above
// the floor. "No outreach for less than 60%": sub-floor rows and unscored
// rows (N/A — nothing to compare against the floor) stay in the table for
// review and are never auto-launched. DNC and missing-contact handling stay
// inside the shared Launch PAIR flow.
//
// Count: the recruiter's footer number (`limit`, default 250, max 1000) —
// the best N by score among the eligible (stable sort: ties keep pool order).
// `limit` always goes through clampLaunchCount, so null/0 select ONE row.
//
// Options the browser's Source & Launch leaves unset:
// - minScore: undefined = LAUNCH_MIN_SCORE. null = no floor (the QA quick
//   launch): every row that clears the skips is eligible, unscored rows
//   sorted last (score -1); belowFloor / unscored stay 0.
// - blocked: row id → skip reason, checked right after `launched` (the
//   server runner's "in_flight" / "applicant_backlog").
// - withReasons: also return `skipped` (see AutoLaunchSelection).
export function computeAutoLaunchSelection(
  pool: readonly any[],
  opts: {
    launchedKeys: ReadonlySet<string>;
    launchedIds: ReadonlySet<string>;
    limit: number;
    minScore?: number | null;
    blocked?: ReadonlyMap<string, string>;
    withReasons?: boolean;
  },
): AutoLaunchSelection {
  const { launchedKeys, launchedIds, limit, blocked, withReasons } = opts;
  const floor = opts.minScore === undefined ? LAUNCH_MIN_SCORE : opts.minScore;
  const idOf = (c: any) =>
    String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
  const eligible: { id: string; score: number }[] = [];
  const seen = new Set<string>();
  const skipped: AutoLaunchSkip[] = [];
  const skip = (id: string, reason: string) => {
    if (withReasons) skipped.push({ id, reason });
  };
  let belowFloor = 0;
  let unscored = 0;
  for (const c of pool) {
    const id = idOf(c);
    if (!id) continue;
    if (seen.has(id)) { skip(id, "duplicate_id"); continue; }
    const exclusion = getCandidateExclusionReason(c);
    if (exclusion) { skip(id, `exclusion:${exclusion}`); continue; }
    if (c?.no_contact === true) { skip(id, "no_contact"); continue; }
    const launchedKey = `${c?.source ?? ""}:${id}`;
    if (launchedKeys.has(launchedKey) || launchedIds.has(id)) { skip(id, "launched"); continue; }
    const blockedReason = blocked?.get(id);
    if (blockedReason) { skip(id, String(blockedReason)); continue; }
    seen.add(id);
    const score = typeof c?.match_score === "number" ? c.match_score : null;
    if (floor === null) {
      eligible.push({ id, score: score === null ? -1 : score });
      continue;
    }
    if (score === null) {
      unscored += 1;
      skip(id, "unscored");
      continue;
    }
    // == isLaunchEligibleScore(score) at the default floor.
    if (!(Number.isFinite(score) && score >= floor)) {
      belowFloor += 1;
      skip(id, "below_floor");
      continue;
    }
    eligible.push({ id, score });
  }
  const ranked = [...eligible].sort((a, b) => b.score - a.score);
  const cap = clampLaunchCount(limit);
  const capped = ranked.slice(0, cap);
  if (withReasons) {
    for (const e of ranked.slice(cap)) skipped.push({ id: e.id, reason: "over_limit" });
  }
  return {
    ids: capped.map((e) => e.id),
    eligibleTotal: eligible.length,
    belowFloor,
    unscored,
    ...(withReasons ? { skipped } : {}),
  };
}

// /jobs/{id}/launched-candidate-keys answer → the composite `${source}:${id}`
// keys and bare ids Step 5 treats as already launched (page.tsx 2017-2041).
// A launched Exa/LinkedIn person now has a JobDiva profile, and the JobDiva
// pools return them under that profile id with a JobDiva-* label, so the
// stamped jobdiva_candidate_id counts as launched too.
export function launchedSetsFromResponse(items: readonly any[] | null | undefined): {
  keys: Set<string>;
  ids: Set<string>;
} {
  const keys = new Set<string>();
  const ids = new Set<string>();
  for (const item of items ?? []) {
    if (item?.candidate_id) {
      keys.add(`${item.source ?? ''}:${item.candidate_id}`);
      ids.add(String(item.candidate_id));
    }
    if (item?.jobdiva_candidate_id) {
      ids.add(String(item.jobdiva_candidate_id));
    }
  }
  return { keys, ids };
}

// ── Launch: hard-filter skips, who gets a contact lookup (page.tsx 8650-8930) ─

// Selected rows withheld from the launch by getCandidateExclusionReason, and
// their display names for the completion screen.
export function computeHardFilterSkips(
  candidates: readonly any[],
  selectedIds: ReadonlySet<string>,
): { ids: Set<string>; names: string[] } {
  const ids = new Set<string>();
  const names: string[] = [];
  for (const c of candidates) {
    const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
    if (!id || !selectedIds.has(id)) continue;
    if (getCandidateExclusionReason(c)) {
      ids.add(id);
      names.push(getCandidateDisplayName(c) || c.name || "Unnamed");
    }
  }
  return { ids, names };
}

// Selected, not hard-filter-skipped rows missing a valid phone OR a valid
// email: the launch looks their contact up. NOTE the selection test uses the
// row's RAW id (not String()-ed), exactly as the page always has.
export function selectCandidatesMissingContact(
  candidates: readonly any[],
  selectedIds: ReadonlySet<any>,
  hardFilterSkipIds: ReadonlySet<string>,
): any[] {
  return candidates.filter(c => {
    const id = c.candidate_id || c.jobdiva_candidate_id || c.id;
    if (!selectedIds.has(id)) return false;
    if (hardFilterSkipIds.has(String(id || "").trim())) return false;
    const phone = getCandidateLaunchPhone(c);
    const email = getCandidateLaunchEmail(c);
    return !isValidLaunchPhone(phone) || !isValidLaunchEmail(email);
  });
}

export const extractLinkedInFromText = (text?: string | null): string => {
  const raw = String(text || "");
  if (!raw) return "";
  const m = raw.match(/https?:\/\/(?:www\.)?linkedin\.com\/in\/[A-Za-z0-9\-_%]+/i);
  return m ? m[0] : "";
};

export const looksLikeLinkedInProfile = (url?: string | null): boolean => {
  const u = String(url || "").trim().toLowerCase();
  return u.includes("linkedin.com/in/");
};

// The LinkedIn profile URL a contact lookup runs on: the first of seven
// fields (or a linkedin.com/in/ link in the résumé) that looks like a profile.
export function resolveLinkedInUrl(c: any): string {
  const linkedinUrlCandidates = [
    c.profile_url,
    c.linkedin_url,
    c.urls?.linkedin,
    c.urls?.linkedin_url,
    c.data?.urls?.linkedin,
    c.data?.urls?.linkedin_url,
    extractLinkedInFromText(c.resume_text || c.resumeText || c.data?.resume_text),
  ].map((v: any) => String(v || "").trim()).filter(Boolean);

  return linkedinUrlCandidates.find((u: string) => looksLikeLinkedInProfile(u)) || "";
}

// The email a Launch lookup is told the candidate already has: the TOP-LEVEL
// email of a JobDiva row, when it is a real launch email ("" otherwise). The
// lookup then buys only the phone, and the candidate keeps their own address.
// Emails on LinkedIn / Dice / Unipile rows are regex- or LLM-extracted
// (unreliable: apps/api/services/contact_cache.py), and ones a provider found
// are reused through the contact cache anyway.
export function getCandidateSeedEmail(c: any): string {
  if (!candIsJobDivaUI(c)) return "";
  const email = String(c?.email || "").trim().toLowerCase();
  return isValidLaunchEmail(email) ? email : "";
}

// The name a Launch lookup is told: the row's name, else first + last name;
// never the title or "LinkedIn profile" that getCandidateDisplayName falls
// back to, nor a placeholder name ("" = none).
export function getCandidateLookupName(c: any): string {
  const clean = (value: unknown) => {
    const cleaned = String(value || "").replace(/\s+/g, " ").trim();
    return ANONYMOUS_CANDIDATE_NAMES.includes(cleaned.toLowerCase()) ? "" : cleaned;
  };
  return clean(c?.name) || clean([c?.firstName, c?.lastName].filter(Boolean).join(" "));
}

// One candidate's request to /candidates/enrich-contact(s). Held to the spend
// policy server-side: LinkedIn/JobDiva sources at or above the score floor
// only (a skip returns no contact). Seeds (2026-10-15): the JobDiva email the
// row holds, its name and current company; never the phone (a held number
// may be a work line, and a personal one is what the lookup is for).
export function buildEnrichRequest(
  c: any,
  candidateId: string,
  linkedinUrl: string,
  jobdivaId: unknown,
): Record<string, unknown> {
  const email = getCandidateSeedEmail(c);
  const fullName = getCandidateLookupName(c);
  const company = getCandidateCurrentCompanies(c)[0] || "";
  return {
    candidate_id: candidateId,
    jobdiva_id: jobdivaId,
    source: c.source || undefined,
    linkedin_url: linkedinUrl,
    trigger: "launch",
    match_score: typeof c.match_score === "number" ? c.match_score : undefined,
    email: email || undefined,
    full_name: fullName || undefined,
    company_name: company || undefined,
  };
}

export type EnrichOutcome = "failed" | "succeeded" | "already_reachable" | "not_eligible" | "no_contact";

// How the launch reads one lookup answer (page.tsx applyEnrichResult):
// `override` is the contact it found (it REPLACES the row's own phone/email at
// launch: override.x || c.x), `lookedUp` marks a real lookup, `kipplo` is the
// raw lookup.kipplo outcome for the "Kipplo issues" summary. `heldEmail` is
// the email the request was seeded with (getCandidateSeedEmail): the
// candidate's own, never replaced by one a provider found.
export function interpretEnrichAnswer(
  enriched: any,
  launchableNow: boolean,
  heldEmail = "",
): {
  outcome: EnrichOutcome;
  lookedUp: boolean;
  kipplo: string;
  override?: ContactOverride;
  phoneProvider?: string;
  emailProvider?: string;
} {
  if (!enriched || enriched.status === "error") {
    return { outcome: "failed", lookedUp: false, kipplo: "" };
  }
  const lookedUp = enriched.status === "success";
  const kipplo = String(enriched?.lookup?.kipplo || "");
  // `phone` is the candidate's personal number (never a work line).
  const nextPhone = enriched?.phone || enriched?.mobilePhone || "";
  const nextEmail = heldEmail ? "" : enriched?.email || "";
  if (nextPhone || nextEmail) {
    return {
      outcome: "succeeded",
      lookedUp,
      kipplo,
      override: {
        phone: nextPhone || undefined,
        email: nextEmail || undefined,
      },
      phoneProvider: nextPhone ? String(enriched?.phone_provider || "other") : undefined,
      emailProvider: nextEmail ? String(enriched?.email_provider || "other") : undefined,
    };
  }
  // Enrichment found nothing new, but the candidate already has a usable
  // phone or email — still launchable, not a miss.
  if (launchableNow || heldEmail) return { outcome: "already_reachable", lookedUp, kipplo };
  // "skipped" = held back by the spend policy (source / score floor).
  return { outcome: enriched?.status === "skipped" ? "not_eligible" : "no_contact", lookedUp, kipplo };
}

// ── DNC re-check, ready / needs-contact split (page.tsx 9062-9157) ──────────

// DNC re-check after enrichment: a candidate without a phone in search results
// may now have one from the lookup, and that phone may be on the DNC list.
// Checks the override phone, else the row's TOP-LEVEL phone.
export function dncDroppedIds(
  candidates: readonly any[],
  selectedIds: ReadonlySet<string>,
  contactOverrides: ContactOverrides,
  dncPhones: ReadonlySet<string>,
): Set<string> {
  const dncDropped = new Set<string>();
  if (dncPhones.size > 0) {
    for (const c of candidates) {
      const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
      if (!id || !selectedIds.has(id)) continue;
      const overridePhone = contactOverrides[id]?.phone;
      const phoneToCheck = overridePhone || c.phone;
      const np = normalizeDncPhone(phoneToCheck);
      if (np && dncPhones.has(np)) {
        dncDropped.add(id);
      }
    }
  }
  return dncDropped;
}

export type NeedsContactRow = {
  candidate_id: string;
  name: string;
  headline: string;
  location: string;
  source: string;
  jobdiva_id?: string;
  needsPhone: boolean;
  needsEmail: boolean;
  currentPhone: string;
  currentEmail: string;
};

// Partition the selection (minus DNC and hard-filter skips) into Ready (a
// usable phone OR a real email) and Needs-info (missing both). Duplicates are
// counted per channel over that same selection: a phone shared by two rows
// makes neither row's PHONE usable, but a row with a duplicated phone and a
// unique valid email is still ready. Duplicate keys are all phone digits and
// the lower-cased email. The effective contact is override-or-original.
export function splitReadyNeedsContact(
  candidates: readonly any[],
  opts: {
    selectedIds: ReadonlySet<string>;
    skipIds: ReadonlySet<string>;
    contactOverrides: ContactOverrides;
    jobdivaId?: unknown;
  },
): {
  readyIds: Set<string>;
  needsInfo: NeedsContactRow[];
  duplicateEmailIds: Set<string>;
  duplicatePhoneIds: Set<string>;
} {
  const { selectedIds, skipIds, contactOverrides, jobdivaId: launchJobdivaId } = opts;
  const readyIds = new Set<string>();
  const needsInfo: NeedsContactRow[] = [];
  const emailToCandidateIds = new Map<string, string[]>();
  const phoneToCandidateIds = new Map<string, string[]>();
  for (const c of candidates) {
    const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
    if (!id || !selectedIds.has(id) || skipIds.has(id)) continue;
    const overrideEmail = contactOverrides[id]?.email;
    const overridePhone = contactOverrides[id]?.phone;
    const effectiveEmail = String(overrideEmail || getCandidateLaunchEmail(c)).trim().toLowerCase();
    const effectivePhone = overridePhone || getCandidateLaunchPhone(c);
    if (isValidLaunchEmail(effectiveEmail)) {
      const ids = emailToCandidateIds.get(effectiveEmail) || [];
      ids.push(id);
      emailToCandidateIds.set(effectiveEmail, ids);
    }
    if (isValidLaunchPhone(effectivePhone)) {
      const normalizedPhone = launchPhoneDigits(effectivePhone);
      const ids = phoneToCandidateIds.get(normalizedPhone) || [];
      ids.push(id);
      phoneToCandidateIds.set(normalizedPhone, ids);
    }
  }
  const duplicateEmailIds = new Set<string>();
  const duplicatePhoneIds = new Set<string>();
  for (const ids of emailToCandidateIds.values()) {
    if (ids.length < 2) continue;
    for (const id of ids) duplicateEmailIds.add(id);
  }
  for (const ids of phoneToCandidateIds.values()) {
    if (ids.length < 2) continue;
    for (const id of ids) duplicatePhoneIds.add(id);
  }
  for (const c of candidates) {
    const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
    if (!id || !selectedIds.has(id) || skipIds.has(id)) continue;
    const overridePhone = contactOverrides[id]?.phone;
    const overrideEmail = contactOverrides[id]?.email;
    const effectivePhone = overridePhone || getCandidateLaunchPhone(c);
    const effectiveEmail = String(overrideEmail || getCandidateLaunchEmail(c)).trim().toLowerCase();
    const phoneOK = isValidLaunchPhone(effectivePhone) && !duplicatePhoneIds.has(id);
    const emailOK = isValidLaunchEmail(effectiveEmail) && !duplicateEmailIds.has(id);
    // PAIR can reach a candidate by phone OR email, so EITHER one is enough
    // to launch. A candidate with just a phone, or just an email, is
    // launchable; only those missing both get routed to the contact modal.
    if (phoneOK || emailOK) {
      readyIds.add(id);
    } else {
      needsInfo.push({
        candidate_id: id,
        name: getCandidateDisplayName(c) || c.name || "Unnamed",
        headline: c.title || c.headline || "",
        location: c.location || "",
        source: c.source || "",
        jobdiva_id: launchJobdivaId ? String(launchJobdivaId) : undefined,
        needsPhone: !phoneOK,
        needsEmail: !emailOK,
        currentPhone: effectivePhone || "",
        currentEmail: effectiveEmail || "",
      });
    }
  }
  return { readyIds, needsInfo, duplicateEmailIds, duplicatePhoneIds };
}

// The needs-contact modal's answers on top of the launch's pending overrides:
// a field the recruiter left empty keeps the lookup's value (page.tsx
// handleMissingContactsProvided).
export function mergeContactOverrides(
  pending: ContactOverrides,
  provided: ContactOverrides,
): ContactOverrides {
  const mergedOverrides: ContactOverrides = { ...pending };
  for (const [id, vals] of Object.entries(provided)) {
    mergedOverrides[id] = {
      phone: vals.phone || pending[id]?.phone,
      email: vals.email || pending[id]?.email,
    };
  }
  return mergedOverrides;
}

// ── /candidates/save payload (page.tsx runLaunchPair 7912-8024) ─────────────

// Contact overrides (lookup results / recruiter-entered) REPLACE the row's
// top-level phone and email: override.x || c.x. Returns `candidates` itself
// when there are no overrides.
export function applyContactOverrides(
  candidates: any[],
  contactOverrides?: ContactOverrides,
): any[] {
  return contactOverrides
    ? candidates.map(c => {
      const id = c.candidate_id || c.jobdiva_candidate_id || c.id;
      const override = contactOverrides[id];
      return override
        ? {
          ...c,
          phone: override.phone || c.phone,
          email: override.email || c.email,
        }
        : c;
    })
    : candidates;
}

// Which rows of the (override-applied) list are persisted by /candidates/save.
export function selectSaveCandidates(
  effective: readonly any[],
  opts: { launchIds: ReadonlySet<any>; job: ClientJob; dncPhones: ReadonlySet<string> },
): any[] {
  const { launchIds, job, dncPhones } = opts;
  return effective
    // Tolerate raw vs String() ids: normal launches pass raw selection ids,
    // the Retry path passes the String()-normalized ids from failedCandidates.
    .filter(c => {
      const cid = c.candidate_id || c.jobdiva_candidate_id || c.id;
      return launchIds.has(cid) || launchIds.has(String(cid));
    })
    // Launch-PAIR skip safety net: only candidates who currently work at the
    // hiring client company are withheld from /candidates/save (even via the
    // second MissingContactsModal pass). Low score / thin profile / location
    // are NOT skip reasons — everyone else stays launchable.
    .filter(c => !getCandidateExclusionReason(c))
    // No-contact company rows are display-only: unselectable in the table,
    // but re-filtered here so no code path (second MissingContactsModal
    // pass included) can ever persist one. Backend re-checks at
    // /candidates/save — defense in depth. NOTE: deliberately NOT part of
    // getCandidateExclusionReason — that helper also drives row VISIBILITY,
    // and no-contact rows must stay visible (greyed out).
    .filter(c => c.no_contact !== true)
    // Same treatment for the hiring-client conflict: the row stays visible
    // and greyed out in the table, but must never be persisted or launched.
    // Backend re-checks at the launch gate — defense in depth.
    .filter(c => withClientConflictFlag(c, job).client_conflict !== true)
    // Final DNC safety net on the top-level phone. The backend repeats this
    // check at /candidates/save — defense in depth.
    .filter(c => {
      if (dncPhones.size === 0) return true;
      const np = normalizeDncPhone(c.phone);
      return !(np && dncPhones.has(np));
    });
}

// One /candidates/save row.
export function buildSaveCandidatePayload(c: any) {
  const displayName = getCandidateDisplayName(c);
  // Send the SAME contact the launch gate used to mark this candidate
  // launchable. getCandidateLaunchEmail/Phone read nested fields
  // (workEmail/personalEmail/enhanced_info.*/data.*/zoominfo_contact_enrichment.*);
  // sending only top-level c.email/c.phone dropped nested-only contact to
  // null, which the backend then rejected — failing the whole batch with a
  // 400. Guard with the same validity checks so we never persist a
  // synthetic/placeholder address.
  const launchEmail = getCandidateLaunchEmail(c);
  const launchPhone = getCandidateLaunchPhone(c);
  let skillList: any[] = [];
  if (Array.isArray(c.skills)) {
    skillList = c.skills;
  } else if (typeof c.skills === 'string' && c.skills.trim()) {
    try {
      const parsed = JSON.parse(c.skills);
      skillList = Array.isArray(parsed) ? parsed : [c.skills];
    } catch {
      skillList = [c.skills];
    }
  }
  return {
    candidate_id: String(c.candidate_id || c.jobdiva_candidate_id || c.id || "unknown"),
    name: displayName || "Unnamed Candidate",
    email: isValidLaunchEmail(launchEmail) ? launchEmail : null,
    phone: isValidLaunchPhone(launchPhone) ? launchPhone : null,
    skills: skillList,
    experience_years: c.yearsExtracted || c.experience_years || 0,
    source: c.source || "JobDiva-Applicants",
    // Label-independent JobDiva provenance. The JobDiva pool emitters stamp
    // this with the row's OWN JobDiva id; the backend trusts it only when it
    // equals candidate_id, so Launch PAIR keeps attaching to the existing
    // profile even if a source label is ever renamed (a renamed label used
    // to turn JobDiva people into "unknown" people that got re-created).
    jobdiva_candidate_id: c.jobdiva_candidate_id ? String(c.jobdiva_candidate_id) : null,
    headline: c.title || c.headline || "",
    location: c.location || "",
    profile_url: c.profile_url || null,
    image_url: c.image_url || null,
    resume_text: c.resume_text || c.resumeText || "",
    resume_id: String(c.resumeId || c.resume_id || ""),
    is_selected: true,
    education: Array.isArray(c.education || c.candidate_education) ? (c.education || c.candidate_education) : [],
    certifications: Array.isArray(c.certifications || c.candidate_certification) ? (c.certifications || c.candidate_certification) : [],
    company_experience: Array.isArray(c.company_experience || c.enhanced_info?.company_experience) ? (c.company_experience || c.enhanced_info?.company_experience) : [],
    urls: (c.urls && typeof c.urls === 'object' && !Array.isArray(c.urls)) ? c.urls : (c.enhanced_info?.urls || {}),
    // Send null (not 0) when unscored so the backend re-scores at save
    // instead of locking a placeholder 0% into the rank list.
    match_score: typeof c.match_score === 'number' ? c.match_score : null,
    detail_failed: !!c.detail_failed,
    matched_skills: Array.isArray(c.matched_skills) ? c.matched_skills : [],
    missing_skills: Array.isArray(c.missing_skills) ? c.missing_skills : [],
    match_score_details: (c.match_score_details && typeof c.match_score_details === 'object' && !Array.isArray(c.match_score_details)) ? c.match_score_details : {},
    explainability: Array.isArray(c.explainability) ? c.explainability : [],
    enhanced_info: (c.enhanced_info && typeof c.enhanced_info === 'object' && !Array.isArray(c.enhanced_info)) ? c.enhanced_info : null,
    // Full LinkedIn profile captured at sourcing (experience with
    // descriptions, education, skills, summary ...). Launch PAIR builds the
    // JobDiva résumé from it for people JobDiva does not have yet.
    linkedin_profile: (c.linkedin_profile && typeof c.linkedin_profile === 'object' && !Array.isArray(c.linkedin_profile)) ? c.linkedin_profile : null
  };
}

export type SaveCandidatePayload = ReturnType<typeof buildSaveCandidatePayload>;

// The whole /candidates/save candidate list for one launch pass.
export function buildLaunchSavePayload(
  candidates: any[],
  opts: {
    launchIds: ReadonlySet<any>;
    contactOverrides?: ContactOverrides;
    job: ClientJob;
    dncPhones: ReadonlySet<string>;
  },
): SaveCandidatePayload[] {
  const effective = applyContactOverrides(candidates, opts.contactOverrides);
  return selectSaveCandidates(effective, opts).map(buildSaveCandidatePayload);
}

// ── Search stream parser (page.tsx runSearchStream 6560-6770) ───────────────

export const mapStageToStatus = (stage: string) => {
  const raw = String(stage || "").toLowerCase();
  // Exa deep-search messages have their own informative shape
  // ("warming up", "running (N seeds)", "done — X enriched, Y new",
  // "skipped: <reason>") — pass them through unchanged so the
  // recruiter can see Pass B progress in the status bar instead of
  // collapsing it into a generic "Searching at Exa portal..." line.
  if (raw.includes("exa deep-search")) {
    return stage;
  }
  if (raw.includes("jobdiva applicants")) {
    return "Searching at JobDiva portal (Applicants)...";
  }
  if (raw.includes("jobdiva talent")) {
    return "Searching at JobDiva portal (Talent Search)...";
  }
  if (raw.includes("linkedin")) {
    return "Searching at LinkedIn portal...";
  }
  if (raw.includes("exa")) {
    return "Searching at Exa portal...";
  }
  if (raw.includes("dice")) {
    return "Searching at Dice portal...";
  }
  return stage;
};

// The portal named by a mapped stage status ("" when it names none).
export function stagePortal(mapped: string): string {
  if (!mapped.toLowerCase().includes("portal")) return "";
  return mapped.replace(/^Searching at\s*/i, "").replace(/\.\.\.$/, "").trim();
}

// Status line while rows stream in: on the 1st row and every 5th.
export function foundProfilesStatus(foundCount: number, activePortal: string): string | null {
  if (foundCount === 1) {
    return `Found 1 profile from ${activePortal}. Matching resumes against the rubric...`;
  } else if (foundCount % 5 === 0) {
    return `Found ${foundCount} profiles from ${activePortal}. Matching resumes against the rubric...`;
  }
  return null;
}

// A `candidate` event's id (seen-set key). A `candidate_detail` patch or a
// `dropped` targets table rows by streamRowId, the run's own list by this.
export const streamCandidateId = (data: any): string =>
  String(data.candidate_id || data.id || "");

export const streamRowId = (c: any): string =>
  String(c.candidate_id || c.jobdiva_candidate_id || c.id || "");

// Detail-lookup failures (JobDiva 429 / no resume) are kept and scored from
// the JobAgent skills; the run tracks them for one summary toast.
export function isDetailFailedPatch(patch: any): boolean {
  return (
    patch.detail_failed === true ||
    ["kept_no_resume", "error"].includes(String(patch.enhanced_info_status || ""))
  );
}

// Merge a patch into a candidate row, guarding phone against a downgrade (an
// incoming patch must never replace a valid number with an empty/invalid one).
export function applyStreamPatch(c: any, patch: Record<string, unknown>): any {
  const merged = { ...c, ...patch };
  if (Object.prototype.hasOwnProperty.call(patch, "phone")) {
    merged.phone = betterPhoneUI(c.phone, (patch as any).phone);
  }
  return merged;
}

// Table-row transitions (the page's setCandidates updaters).
export const addStreamCandidate = (rows: any[], data: any): any[] =>
  deduplicateCandidatesUI([...rows, data]);

export const patchStreamCandidates = (rows: any[], targetId: string, patch: Record<string, unknown>): any[] =>
  deduplicateCandidatesUI(rows.map(c => (
    streamRowId(c) === targetId
      ? applyStreamPatch(c, patch)
      : c
  )));

export const dropStreamCandidate = (rows: any[], targetId: string): any[] =>
  rows.filter(c => streamRowId(c) !== targetId);

// "ok" is sticky within a run: a later tranche or relaxation pass that finds
// nothing (or fails) must not repaint a bucket that already has rows as broken.
export function mergeSourceStatus(prev: Record<string, any>, status: any): Record<string, any> {
  const src = String(status?.source || "");
  if (!src) return prev;
  const cur = prev[src];
  if (cur?.status === "ok" && status.status !== "ok") return prev;
  return { ...prev, [src]: status };
}

// Everything runSearchStream keeps while consuming /candidates/search events.
// `candidates` = the Step-5 table rows (de-duplicated after every row/patch),
// `runList` = the current attempt's rows (the relaxation loop's `accumulated`
// is the concatenation of every attempt's runList; countQualified runs on it).
export type StreamState = {
  candidates: any[];
  seenIds: string[];
  runList: any[];
  sourceStatuses: Record<string, any>;
  searchStatus: string;
  activePortal: string;
  jobdivaCriteriaUnconfigured: boolean;
  // The summary asked for the JobDiva-criteria guidance modal (sticky).
  criteriaModalRequested: boolean;
  detailFailedIds: string[];
  errors: string[];
};

export function initialStreamState(): StreamState {
  return {
    candidates: [],
    seenIds: [],
    runList: [],
    sourceStatuses: {},
    searchStatus: "Connecting to source portals...",
    activePortal: "source portal",
    jobdivaCriteriaUnconfigured: false,
    criteriaModalRequested: false,
    detailFailedIds: [],
    errors: [],
  };
}

// A new /candidates/search response started streaming (response OK).
// "replace" (the first attempt) clears the table, the seen ids and the source
// pills; "append" (a relaxation attempt) keeps them.
export function beginStreamAttempt(state: StreamState, mode: "replace" | "append"): StreamState {
  const next: StreamState = { ...state, runList: [], activePortal: "source portal" };
  if (mode === "replace") {
    next.candidates = [];
    next.sourceStatuses = {};
    next.seenIds = [];
  }
  return next;
}

// Pure reducer mirroring runSearchStream's per-event handling. The page
// still applies events inline (React state setters) with the same
// primitives; the server runner applies them through this reducer (Python
// apply_stream_event). Change both together — launch-rules.test.ts fails if
// the event types the two handle ever differ.
// - candidate: skipped entirely when its id was already seen this run; else
//   appended to runList and to the table (de-duplicated).
// - candidate_detail stage "dropped": removes the row (table + runList) and
//   forgets its id, so a later attempt can add it back as a new row.
// - candidate_detail anything else (any stage, or none — hydration,
//   merge-into-owner, exa_deep_search, gender, scored...): a non-empty patch
//   is merged into every table row with that id ({...row, ...patch}, phone via
//   betterPhoneUI) and into the first runList row with it; then de-dup. A
//   later attempt's `scored` patch therefore overrides the earlier score.
// - stage: status line; source_status: "ok" sticky; summary: criteria flag;
//   error: recorded (the run keeps going).
// Inputs are not mutated. Events whose `data` is not an object are ignored
// for `candidate` (the page would throw on null and skip the line).
export function applyStreamEvent(state: StreamState, event: any): StreamState {
  if (!event || typeof event !== "object") return state;
  if (event.type === "candidate") {
    const data = event.data;
    if (!data || typeof data !== "object") return state;
    const id = streamCandidateId(data);
    if (id && state.seenIds.includes(id)) return state;
    const next: StreamState = { ...state };
    if (id) next.seenIds = [...state.seenIds, id];
    next.runList = [...state.runList, data];
    next.candidates = addStreamCandidate(state.candidates, data);
    const status = foundProfilesStatus(next.runList.length, state.activePortal);
    if (status !== null) next.searchStatus = status;
    return next;
  }
  if (event.type === "candidate_detail") {
    const targetId = String(event.candidate_id || "");
    const stage = String(event.stage || "");
    if (!targetId) return state;
    if (stage === "dropped") {
      return {
        ...state,
        runList: state.runList.filter(r => streamCandidateId(r) !== targetId),
        seenIds: state.seenIds.filter(s => s !== targetId),
        candidates: dropStreamCandidate(state.candidates, targetId),
      };
    }
    const patch = (event.patch && typeof event.patch === "object") ? event.patch : {};
    if (Object.keys(patch).length === 0) return state;
    const next: StreamState = { ...state };
    if (isDetailFailedPatch(patch) && !state.detailFailedIds.includes(targetId)) {
      next.detailFailedIds = [...state.detailFailedIds, targetId];
    }
    const runList = [...state.runList];
    for (let i = 0; i < runList.length; i++) {
      if (streamCandidateId(runList[i]) === targetId) {
        runList[i] = applyStreamPatch(runList[i], patch);
        break;
      }
    }
    next.runList = runList;
    next.candidates = patchStreamCandidates(state.candidates, targetId, patch);
    return next;
  }
  if (event.type === "stage") {
    const mapped = mapStageToStatus(String(event.data || ""));
    const portalPart = stagePortal(mapped);
    return {
      ...state,
      activePortal: portalPart || state.activePortal,
      searchStatus: mapped,
    };
  }
  if (event.type === "summary") {
    const summary = event.data?.summary || event.data || {};
    const unconfigured = Boolean(summary?.jobdiva_criteria_unconfigured);
    return {
      ...state,
      searchStatus: `Found ${state.runList.length} profiles. Finalizing shortlist and quality scoring...`,
      jobdivaCriteriaUnconfigured: unconfigured,
      criteriaModalRequested: state.criteriaModalRequested || unconfigured,
    };
  }
  if (event.type === "source_status") {
    const status = event.data || {};
    const src = String(status.source || "");
    if (!src) return state;
    return {
      ...state,
      sourceStatuses: mergeSourceStatus(state.sourceStatuses, status),
      jobdivaCriteriaUnconfigured: status.criteria_unconfigured ? true : state.jobdivaCriteriaUnconfigured,
    };
  }
  if (event.type === "error") {
    return { ...state, errors: [...state.errors, String(event.message ?? "")] };
  }
  return state;
}
