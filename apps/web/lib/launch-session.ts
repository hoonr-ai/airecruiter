/* eslint-disable @typescript-eslint/no-explicit-any -- session rows carry untyped candidate JSON (see lib/launch-rules.ts) */
// Step-5 Source & Launch driven by a SERVER launch session
// (apps/api/routers/launch_sessions.py, services/launch_sessions/; design and
// runbook: docs/launch-sessions.md). One recruiter click creates one durable
// session that the API runs to the end; the browser only creates it, watches
// it and answers its questions (QA review, missing contacts, retry).
//
// Everything here is PURE: the request bodies the click sends (the relaxation
// chain is precomputed exactly as handleRunSearch would run it), the
// session → Step-5 UI mapping (table, search status, progress modal), the
// redirect rule, the failed-candidate CSV rows, the polling back-off, the
// keyset row merge and how create / attach answers are read. The React side
// is hooks/use-launch-session.ts (timers, fetch, visibility) and thin glue in
// app/jobs/new/page.tsx. Tests: lib/launch-session.test.ts.
//
// No value imports from sibling modules (node --test loads this file directly
// and the Next tsconfig does not allow ".ts" specifiers): page-level rules
// such as deduplicateCandidatesUI or getCandidateDisplayName are INJECTED.

// ── Wire shapes ─────────────────────────────────────────────────────────────

export type LaunchSessionStatus =
  | "collecting"
  | "queued"
  | "running"
  | "awaiting_input"
  | "completed"
  | "failed"
  | "cancelled";

export type LaunchSessionStage =
  | "created"
  | "sourcing"
  | "selecting"
  | "enriching"
  | "review"
  | "splitting"
  | "saving"
  | "launching"
  | "provisioning"
  | "done";

export type LaunchRowState =
  | "pooled"
  | "dropped"
  | "selected"
  | "skipped"
  | "needs_contact"
  | "ready"
  | "saved"
  | "launching"
  | "launched"
  | "failed"
  | "unconfirmed"
  | "handed_off";

export const TERMINAL_STATUSES: readonly LaunchSessionStatus[] = ["completed", "failed", "cancelled"];
export const ACTIVE_STATUSES: readonly LaunchSessionStatus[] = ["collecting", "queued", "running", "awaiting_input"];

export interface LaunchSessionConfig {
  enabled: boolean;
  poll_ms: number;
  hidden_poll_ms: number;
  max_upload_chunk: number;
  max_candidates: number;
  require_review_default: boolean;
}

export interface LaunchSessionSourcingUi {
  source_statuses?: Record<string, any>;
  search_status?: string;
  criteria_unconfigured?: boolean;
  criteria_modal_requested?: boolean;
  detail_failed_count?: number;
  errors?: string[];
  error_count?: number;
}

export interface LaunchSessionEnrichCounts {
  total?: number;
  looked_up?: number;
  succeeded?: number;
  already_reachable?: number;
  missing_linkedin?: number;
  no_contact?: number;
  not_eligible?: number;
  failed?: number;
  phone_found_by?: Record<string, number>;
  email_found_by?: Record<string, number>;
  kipplo_issues?: Record<string, number>;
}

export interface LaunchSessionCounts {
  pool?: number;
  qualified?: number;
  selected?: number;
  selection?: {
    pool?: number;
    table?: number;
    selected?: number;
    eligible_total?: number;
    below_floor?: number;
    unscored?: number;
    limit?: number;
    min_score?: number | null;
    skipped?: Record<string, number>;
  };
  enrich?: LaunchSessionEnrichCounts;
  split?: {
    ready?: number;
    needs_contact?: number;
    dnc?: number;
    hard_filter?: number;
    duplicate_email?: number;
    duplicate_phone?: number;
  };
  states?: Record<string, number>;
  reasons?: Record<string, Record<string, number>>;
  sent?: number;
  already_launched?: number;
  launched?: number;
  failed?: number;
  unconfirmed?: number;
  needs_contact?: number;
  unrecorded?: number;
  /** S6's total (``saved`` holds the same number once S6 is done). */
  saved_total?: number;
  /** Rows Pairbot created no interview for (failed, reason pairbot_rejected). */
  no_interview?: number;
  failed_batches?: number;
  rows_in_saved?: number;
  side_effects?: string;
  [key: string]: unknown;
}

export interface LaunchSessionEvent {
  seq: number;
  payload: { type?: string; [key: string]: any } | null;
  created_at?: string | null;
}

export interface LaunchSessionView {
  id: string;
  job_id: string;
  jobdiva_ref?: string | null;
  parent_session_id?: string | null;
  trigger?: string | null;
  /** The requester's email, lower-cased (the server compares it with the signed-in user's). */
  requested_by?: string | null;
  mode: "full_search" | "provided" | string;
  status: LaunchSessionStatus | string;
  stage: LaunchSessionStage | string;
  terminal?: boolean;
  /** Terminal, or past S7: the launch is over (only JobDiva provisioning / the wrap-up left). */
  launch_complete?: boolean;
  /** While waiting for a worker: sessions of the same phase ahead of it (0 = next). */
  queue_position?: number | null;
  phase?: string | null;
  retry_at?: string | null;
  error?: string | null;
  require_review?: boolean;
  attempt?: number;
  claims?: number;
  cancel_requested?: boolean;
  cancel_reason?: string | null;
  counts?: LaunchSessionCounts | null;
  needs_contact_count?: number;
  failed_count?: number;
  unconfirmed_count?: number;
  launched_count?: number;
  sourcing?: {
    attempts_total?: number;
    next_attempt?: number;
    current_attempt?: number | null;
    current_label?: string | null;
    pool?: number;
    qualified?: number;
    done?: boolean;
    attempts?: Array<{ label?: string | null; began?: boolean; rows?: number; qualified?: number }>;
    ui?: LaunchSessionSourcingUi;
    /** The relaxation chain the session runs (label + boolean string per attempt). */
    plan?: Array<{ label?: string | null; boolean_string?: string | null }>;
  } | null;
  review?: Record<string, any> | null;
  input?: {
    launch_count?: number | null;
    min_score?: number | null;
    qualified_target?: number | null;
    search_attempts?: number;
    options?: Record<string, any>;
    skip_enrichment?: boolean;
  } | null;
  timings?: Record<string, any> | null;
  timestamps?: Record<string, string | null> | null;
  last_event_seq?: number;
  cand_rev?: number;
  events_tail?: LaunchSessionEvent[];
  children?: Array<{ id: string; trigger?: string; status?: string; stage?: string }>;
}

export interface LaunchSessionRow {
  candidate_id: string;
  state: LaunchRowState | string;
  reason?: string | null;
  match_score?: number | null;
  source?: string | null;
  ord?: number | null;
  rev?: number | null;
  updated_at?: string | null;
  failure_stage?: string | null;
  batch_index?: number | null;
  interview_id?: string | null;
  error?: string | null;
  contact?: Record<string, any> | null;
  current_phone?: string | null;
  current_email?: string | null;
  row?: Record<string, any> | null;
}

export interface LaunchSessionRowsPage {
  rows: LaunchSessionRow[];
  total: number;
  cand_rev: number;
  has_more: boolean;
  next?: { after_rev: number; after_id: string };
}

// The browser's LaunchFailedCandidate (components/launch-pair-progress-modal.tsx),
// restated so this file stays import-free; page.tsx assigns it structurally.
export interface FailedCandidateCsvRow {
  candidate_id: string;
  name: string;
  email: string | null;
  phone: string | null;
  source: string;
  headline: string;
  location: string;
  experience_years: number;
  match_score: number;
  skills: string;
  matched_skills: string;
  resume_id: string;
  profile_url: string | null;
  batch_index: number;
  failure_stage: "save" | "engage";
  error_message: string;
}

// ── Config ──────────────────────────────────────────────────────────────────

const positiveInt = (v: unknown, fallback: number, min = 1): number => {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) && n >= min ? Math.floor(n) : fallback;
};

/** The config answer, sanitised; null when it is not usable (→ legacy flow). */
export function normalizeConfig(raw: unknown): LaunchSessionConfig | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.enabled !== "boolean") return null;
  return {
    enabled: r.enabled,
    poll_ms: positiveInt(r.poll_ms, 3000, 500),
    hidden_poll_ms: positiveInt(r.hidden_poll_ms, 15000, 1000),
    max_upload_chunk: positiveInt(r.max_upload_chunk, 50),
    max_candidates: positiveInt(r.max_candidates, 1000),
    require_review_default: Boolean(r.require_review_default),
  };
}

/**
 * The config answer → the config (null = legacy flow) and whether a failure
 * is worth asking again: network, 408 / 429 and 5xx are a blip (a deploy, the
 * shared office rate limit), and so is 401 (the sign-in token not ready yet
 * when the page mounts), so the page keeps asking; an explicit answer —
 * `enabled:false`, a 404 (an API without the router), 403, a body that is
 * not a config — is final for this page.
 */
export function classifyConfigResponse(res: {
  ok: boolean;
  status: number;
  data?: unknown;
  network?: boolean;
}): { config: LaunchSessionConfig | null; transient: boolean } {
  if (res.ok) return { config: normalizeConfig(res.data), transient: false };
  return { config: null, transient: Boolean(res.network) || res.status === 401 || isTransientStatus(res.status) };
}

/** The wait before config / reattach attempt `attempt` (1-based): 1, 2, 4, 8, 16 s, then `capMs`. */
export const lookupRetryDelay = (attempt: number, capMs = 30000): number =>
  Math.min(capMs, 1000 * 2 ** Math.max(0, Math.floor(attempt) - 1));

/**
 * Look for this job's sessions on page load (reattach to the user's own,
 * the banner for a teammate's, the parked needs-contact offer)? Once the
 * config has answered — WHATEVER its `enabled` says. `enabled` gates only
 * STARTING a new server launch (serverFlowReady): a session that already
 * exists still runs to the end on the server when server launch (or its
 * runner) is switched off, so the page must keep showing it, let the
 * recruiter answer its QA review / needs-contact rows, and keep a legacy
 * launch from starting on the job underneath it.
 */
export const shouldLookForSessions = (args: {
  reattach: boolean;
  jobRef: string | null | undefined;
  configState: "loading" | "ready" | "failed";
}): boolean => args.reattach && String(args.jobRef ?? "").trim() !== "" && args.configState === "ready";

// ── Click-time snapshot (POST /jobs/{job}/launch-sessions) ──────────────────

export type SearchAttempt<P> = { label: string; query: string; payload: P };

/**
 * The boolean-relaxation chain handleRunSearch would run, precomputed at click
 * time: attempt 0 = the generated boolean with no structural overrides; tier
 * t (1, 2, 3 …) relaxes the PREVIOUS attempt's query and stops as soon as a
 * tier changes nothing; its structural overrides are a pure function of the
 * tier and the click-time state (not cumulative). The loop's only other
 * inputs — the qualified count and abort — are applied by the server between
 * attempts, so precomputing is exactly equivalent (review-parity #16).
 */
export function buildSearchAttemptChain<P, O>(args: {
  initialQuery: string;
  maxAttempts: number;
  relax: (query: string, tier: number) => { query: string; label: string };
  overridesFor: (tier: number) => O;
  buildPayload: (query: string, overrides?: O) => P;
  initialLabel?: string;
}): SearchAttempt<P>[] {
  const max = Math.max(1, Math.floor(args.maxAttempts));
  const chain: SearchAttempt<P>[] = [
    { label: args.initialLabel ?? "PAIR generated", query: args.initialQuery, payload: args.buildPayload(args.initialQuery) },
  ];
  while (chain.length < max) {
    const tier = chain.length;
    const prev = chain[chain.length - 1].query;
    const relaxed = args.relax(prev, tier);
    if (relaxed.query === prev) break;
    chain.push({ label: relaxed.label, query: relaxed.query, payload: args.buildPayload(relaxed.query, args.overridesFor(tier)) });
  }
  return chain;
}

export interface LaunchOptionsBody {
  notify_recruiters: boolean;
  send_job_posting_email: boolean;
  is_initial_launch: boolean;
  job_board_ids: string[];
  batch_size: number;
}

/** What runLaunchPair passes to /engage/launch today (a retry turns every side effect off). */
export function buildLaunchOptions(args: {
  wizardMode: string;
  isRetry?: boolean;
  jobBoardIds: readonly string[];
  batchSize: number;
}): LaunchOptionsBody {
  const retry = Boolean(args.isRetry);
  return {
    is_initial_launch: args.wizardMode !== "source" && !retry,
    notify_recruiters: !retry,
    send_job_posting_email: args.jobBoardIds.length > 0 && !retry,
    job_board_ids: [...args.jobBoardIds],
    batch_size: args.batchSize,
  };
}

/** lib/launch-rules.ts clampLaunchCount, restated (the test asserts they agree). */
export const clampCount = (n: number, max = 1000): number => Math.min(max, Math.max(1, Math.floor(Number(n) || 0)));

export type LaunchTrigger = "button" | "qa_quick" | "retry" | "needs_contact" | "launch_remaining";

export interface CreateSessionBody {
  idempotency_key: string;
  mode: "full_search" | "provided";
  trigger: LaunchTrigger;
  search_attempts?: Array<{ label: string; payload: unknown }>;
  min_score?: number | null;
  qualified_target?: number | null;
  launch_count?: number;
  require_review: boolean;
  options: LaunchOptionsBody;
  app_base_url: string;
  candidates?: any[];
  start?: boolean;
  from_session_id?: string;
  candidate_ids?: string[];
}

export function buildFullSearchCreateBody(args: {
  idempotencyKey: string;
  attempts: ReadonlyArray<SearchAttempt<unknown>>;
  launchCount: number;
  minScore: number;
  qualifiedTarget: number;
  requireReview: boolean;
  options: LaunchOptionsBody;
  appBaseUrl: string;
  maxCount?: number;
}): CreateSessionBody {
  return {
    idempotency_key: args.idempotencyKey,
    mode: "full_search",
    trigger: "button",
    search_attempts: args.attempts.map((a) => ({ label: a.label, payload: a.payload })),
    min_score: args.minScore,
    qualified_target: args.qualifiedTarget,
    launch_count: clampCount(args.launchCount, args.maxCount),
    require_review: args.requireReview,
    options: args.options,
    app_base_url: args.appBaseUrl,
  };
}

/**
 * A provided-mode create ("launch remaining", the QA quick launch). Rows that
 * came from a server session are copied server-side (from_session_id, no
 * re-upload); otherwise the first chunk rides on the create and `start` is set
 * when it is the only one. No score floor: the browser already chose them.
 */
export function buildProvidedCreateBody(args: {
  idempotencyKey: string;
  trigger: LaunchTrigger;
  ids: readonly string[];
  requireReview: boolean;
  options: LaunchOptionsBody;
  appBaseUrl: string;
  fromSessionId?: string | null;
  firstChunk?: readonly any[];
  startOnCreate?: boolean;
  maxCount?: number;
}): CreateSessionBody {
  const base: CreateSessionBody = {
    idempotency_key: args.idempotencyKey,
    mode: "provided",
    trigger: args.trigger,
    min_score: null,
    launch_count: clampCount(args.ids.length, args.maxCount),
    require_review: args.requireReview,
    options: args.options,
    app_base_url: args.appBaseUrl,
  };
  if (args.fromSessionId) {
    return { ...base, from_session_id: args.fromSessionId, candidate_ids: [...args.ids] };
  }
  return { ...base, candidates: [...(args.firstChunk ?? [])], start: Boolean(args.startOnCreate) };
}

export function chunk<T>(items: readonly T[], size: number): T[][] {
  const step = Math.max(1, Math.floor(size));
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += step) out.push(items.slice(i, i + step));
  return out;
}

/** Upload plan: the first chunk rides on the create; the rest go to …/candidates, then …/start. */
export function planProvidedUpload<T>(rows: readonly T[], chunkSize: number): { first: T[]; rest: T[][]; startOnCreate: boolean } {
  const parts = chunk(rows, chunkSize);
  return { first: parts[0] ?? [], rest: parts.slice(1), startOnCreate: parts.length <= 1 };
}

/** "Launch remaining" can copy the rows server-side only when every chosen id is in that session. */
export function canCopyFromSession(ids: readonly string[], sessionRowIds: ReadonlySet<string> | null | undefined): boolean {
  if (!sessionRowIds || ids.length === 0) return false;
  return ids.every((id) => sessionRowIds.has(id));
}

export function newIdempotencyKey(cryptoLike?: { randomUUID?: () => string } | null): string {
  try {
    if (cryptoLike && typeof cryptoLike.randomUUID === "function") return cryptoLike.randomUUID();
  } catch {
    // fall through
  }
  return `ls-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

// ── Reading API answers ─────────────────────────────────────────────────────

/** A FastAPI error body's readable message: detail string, detail.message, or the first 422 item. */
export function errorMessageOf(body: unknown, fallback = ""): string {
  if (!body || typeof body !== "object") return typeof body === "string" && body.trim() ? body.trim().slice(0, 300) : fallback;
  const detail = (body as Record<string, any>).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    if (typeof detail.message === "string" && detail.message) return detail.message;
    if (typeof detail.code === "string") return detail.code;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0];
    if (first && typeof first.msg === "string") return first.msg;
  }
  const message = (body as Record<string, any>).message;
  return typeof message === "string" && message ? message : fallback;
}

const detailCode = (body: unknown): string => {
  const detail = body && typeof body === "object" ? (body as Record<string, any>).detail : null;
  return detail && typeof detail === "object" && !Array.isArray(detail) && typeof detail.code === "string" ? detail.code : "";
};

export type CreateOutcome =
  | { kind: "created" | "replayed"; session: LaunchSessionView }
  | { kind: "active_session"; activeSessionId: string | null; status: string | null }
  | { kind: "outreach_stopped"; message: string }
  | { kind: "email_gate"; message: string }
  | { kind: "disabled" }
  | { kind: "transient"; status: number; retryAfterMs: number | null }
  | { kind: "error"; status: number; message: string };

/**
 * How a create (or a needs-contact / retry-failed child create) answer is
 * handled: a 409 for another active session ATTACHES to it, outreach-stopped
 * and the recruiter-email gate become today's toasts, "disabled" falls back to
 * the legacy flow, 429 / 5xx / network are retried with the same key.
 */
export function classifyCreateResponse(res: {
  ok: boolean;
  status: number;
  body?: unknown;
  data?: unknown;
  network?: boolean;
  retryAfterMs?: number | null;
}): CreateOutcome {
  if (res.ok) {
    const session = (res.data as Record<string, any> | null)?.session;
    if (session && typeof session === "object" && typeof session.id === "string") {
      return { kind: res.status === 200 ? "replayed" : "created", session: session as LaunchSessionView };
    }
    return { kind: "error", status: res.status, message: "The server answered without a launch session." };
  }
  if (res.network || res.status === 0) return { kind: "transient", status: 0, retryAfterMs: null };
  const code = detailCode(res.body);
  const detail = res.body && typeof res.body === "object" ? (res.body as Record<string, any>).detail : null;
  if (res.status === 409) {
    if (code === "active_session") {
      return {
        kind: "active_session",
        activeSessionId: typeof detail?.active_session_id === "string" ? detail.active_session_id : null,
        status: typeof detail?.status === "string" ? detail.status : null,
      };
    }
    if (code === "outreach_stopped") {
      return { kind: "outreach_stopped", message: errorMessageOf(res.body, "Job activity has been stopped. Cannot launch new candidates.") };
    }
    if (code === "idempotency_key_in_use") {
      return { kind: "error", status: 409, message: "This launch request is already in use by another user. Please click again." };
    }
    if (code === "parent_active") {
      return { kind: "error", status: 409, message: "that launch is still running — try again once it has finished" };
    }
    return { kind: "error", status: 409, message: errorMessageOf(res.body, "Another launch changed this job. Please try again.") };
  }
  if (res.status === 503 && code === "disabled") return { kind: "disabled" };
  if (res.status === 400) return { kind: "email_gate", message: errorMessageOf(res.body, "This job has no recruiter email assigned.") };
  if (res.status === 429 || res.status >= 500) {
    return { kind: "transient", status: res.status, retryAfterMs: res.retryAfterMs ?? null };
  }
  if (res.status === 401 || res.status === 403) {
    return { kind: "error", status: res.status, message: errorMessageOf(res.body, "You do not have access to launch PAIR for this job.") };
  }
  return { kind: "error", status: res.status, message: errorMessageOf(res.body, `HTTP ${res.status}`) };
}

/** Worth sending the same request again (same idempotency key)? */
export const isTransientStatus = (status: number): boolean => status === 0 || status === 408 || status === 429 || status >= 500;

/**
 * Resend this write (hooks/use-launch-session writeWithRetry)? 429 / 5xx /
 * network — but never 503 `{code: disabled}`: launches are switched off
 * (PAIR_SERVER_LAUNCH_ENABLED / PAIR_LAUNCH_RUNNER_ENABLED), which no retry
 * within the next half minute changes.
 */
export function shouldRetryWrite(res: { ok: boolean; status: number; body?: unknown }): boolean {
  if (res.ok) return false;
  if (res.status === 503 && detailCode(res.body) === "disabled") return false;
  return isTransientStatus(res.status);
}

/** Retry-After (seconds or an HTTP date) → ms; null when absent / unreadable. */
export function retryAfterMs(header: string | null | undefined, nowMs: number = Date.now()): number | null {
  if (header === null || header === undefined) return null;
  const text = String(header).trim();
  if (!text) return null;
  const seconds = Number(text);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.round(seconds * 1000);
  const at = Date.parse(text);
  return Number.isFinite(at) ? Math.max(0, at - nowMs) : null;
}

/** The wait before retry `attempt` (1-based) of a write: Retry-After wins when longer. */
export function writeRetryDelay(attempt: number, retryAfter: number | null, baseMs = 800, capMs = 15000): number {
  const backoff = Math.min(capMs, baseMs * 2 ** Math.max(0, attempt - 1));
  return retryAfter !== null && retryAfter > backoff ? Math.min(60000, retryAfter) : backoff;
}

/** The poll interval while a session is settled (see isSettledForPolling). */
export const SETTLED_POLL_MS = 10_000;

/**
 * Nothing on screen moves fast: the session waits for the recruiter
 * (awaiting_input — their confirm / cancel wakes the poll at once), or the
 * launch is done and only JobDiva provisioning is left.
 */
export const isSettledForPolling = (view: Pick<LaunchSessionView, "status" | "terminal" | "launch_complete" | "stage"> | null | undefined): boolean =>
  Boolean(view && !isTerminal(view) && (view.status === "awaiting_input" || isLaunchComplete(view)));

/**
 * The next poll: config.poll_ms normally, SETTLED_POLL_MS while `settled`;
 * doubling per consecutive error up to `capMs` (15 s); never faster than
 * hidden_poll_ms (15 s) while the tab is hidden. Polling never gives up while
 * the page is mounted.
 */
export function nextPollDelay(args: {
  baseMs: number;
  hiddenMs: number;
  hidden: boolean;
  errors: number;
  capMs?: number;
  settled?: boolean;
}): number {
  const cap = Math.max(1000, args.capMs ?? 15000);
  const configured = Math.max(500, Number.isFinite(args.baseMs) ? args.baseMs : 3000);
  const base = Math.min(cap, args.settled ? Math.max(configured, SETTLED_POLL_MS) : configured);
  const errors = Math.max(0, Math.floor(args.errors || 0));
  const delay = errors > 0 ? Math.min(cap, base * 2 ** errors) : base;
  return args.hidden ? Math.max(delay, Number.isFinite(args.hiddenMs) ? args.hiddenMs : 15000) : delay;
}

/** Did `me` (the signed-in user's email) start this session? Unknown user = no. */
export function isRequestedBy(view: Pick<LaunchSessionView, "requested_by"> | null | undefined, me: string | null | undefined): boolean {
  const who = String(me ?? "").trim().toLowerCase();
  return Boolean(who) && String(view?.requested_by ?? "").trim().toLowerCase() === who;
}

export interface ReattachPlan {
  /** Watch this one (as "reattach"): the tab's own session, else the user's newest running one. */
  follow: LaunchSessionView | null;
  /** This tab's upload that a reload cut off (still collecting): cancel it, it can never finish. */
  interrupted: LaunchSessionView | null;
  /** Someone else's running launch: a banner offers to view it — never taken over. */
  other: LaunchSessionView | null;
  /** The user's own launch that is over (still provisioning JobDiva) with rows parked for contact details. */
  parked: LaunchSessionView | null;
}

/**
 * Reattach on page load, from GET /jobs/{job}/launch-sessions?active=true.
 * Only the user's OWN sessions are followed: the one this tab watched before
 * the reload (sessionStorage hint), else the newest one they requested. A
 * teammate's running launch is only offered (a banner), so opening a job
 * never hands someone else's table, Stop button, review or redirect to you.
 * A session whose launch is over (only JobDiva provisioning left) is followed
 * only when it is this tab's and its outcome was never handled here (a reload
 * at the very end), so the result, the failed CSV and Retry still show.
 */
export function planReattach(
  sessions: readonly LaunchSessionView[] | null | undefined,
  opts: {
    hintId?: string | null;
    me?: string | null;
    outcomeHandled?: (id: string) => boolean;
    uploadingIds?: ReadonlySet<string> | null;
  } = {},
): ReattachPlan {
  const active = (sessions ?? []).filter((s) => s && typeof s.id === "string" && ACTIVE_STATUSES.includes(s.status as LaunchSessionStatus));
  const plan: ReattachPlan = { follow: null, interrupted: null, other: null, parked: null };
  const hinted = opts.hintId ? active.find((s) => s.id === opts.hintId) ?? null : null;
  if (hinted) {
    if (hinted.status === "collecting" && !opts.uploadingIds?.has(hinted.id)) plan.interrupted = hinted;
    else if (!isLaunchComplete(hinted) || !opts.outcomeHandled?.(hinted.id)) plan.follow = hinted;
  }
  const running = active.filter((s) => s !== plan.interrupted && s !== hinted && !isLaunchComplete(s));
  if (!plan.follow) plan.follow = running.find((s) => isRequestedBy(s, opts.me)) ?? null;
  if (!plan.follow) plan.other = running.find((s) => !isRequestedBy(s, opts.me)) ?? null;
  plan.parked = active.find(
    (s) => s !== plan.follow && isLaunchComplete(s) && n(s.needs_contact_count) > 0 && isRequestedBy(s, opts.me),
  ) ?? null;
  return plan;
}

/**
 * Page load: the finished session to offer the missing-contacts modal for —
 * the newest COMPLETED one (a cancelled or failed launch never offers its
 * parked rows, as at the end of a run) that this user requested and that
 * finished within `maxAgeMs` (24 h); null when there is none.
 */
export function pickParkedOffer(
  sessions: readonly LaunchSessionView[] | null | undefined,
  me: string | null | undefined,
  nowMs: number,
  maxAgeMs = 24 * 60 * 60 * 1000,
): LaunchSessionView | null {
  for (const s of sessions ?? []) {
    if (!s || s.status !== "completed" || n(s.needs_contact_count) <= 0 || !isRequestedBy(s, me)) continue;
    const at = Date.parse(String(s.timestamps?.completed_at ?? s.timestamps?.updated_at ?? ""));
    if (Number.isFinite(at) && nowMs - at <= maxAgeMs) return s;
  }
  return null;
}

// ── Session hint (sessionStorage) ───────────────────────────────────────────

export interface LaunchSessionHint {
  id: string;
  attempts?: Array<{ query: string; label: string }>;
  /** This tab started it (or a child of it): its own launch even if requested_by can't be matched. */
  own?: boolean;
}

export const sessionHintKey = (jobRef: string): string => `pair:launch-session:${jobRef}`;

export function encodeSessionHint(hint: LaunchSessionHint): string {
  return JSON.stringify({
    id: hint.id,
    attempts: (hint.attempts ?? []).map((a) => ({ query: String(a.query ?? ""), label: String(a.label ?? "") })),
    ...(hint.own ? { own: true } : {}),
  });
}

export function decodeSessionHint(raw: string | null | undefined): LaunchSessionHint | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.id !== "string" || !parsed.id) return null;
    const attempts = Array.isArray(parsed.attempts)
      ? parsed.attempts
          .filter((a: any) => a && typeof a.query === "string")
          .map((a: any) => ({ query: a.query, label: typeof a.label === "string" ? a.label : "" }))
      : [];
    return { id: parsed.id, attempts, ...(parsed.own === true ? { own: true } : {}) };
  } catch {
    return null;
  }
}

// ── Keyset row cache (GET …/candidates?after_rev=&after_id=) ────────────────

export interface RowCache {
  rows: Record<string, LaunchSessionRow>;
  cursor: { after_rev: number; after_id: string };
  candRev: number;
  version: number;
}

export const emptyRowCache = (): RowCache => ({ rows: {}, cursor: { after_rev: 0, after_id: "" }, candRev: -1, version: 0 });

/**
 * Upsert one keyset page: a row replaces the cached copy unless the cached
 * one is newer (higher rev); the cursor moves to the page's `next`. `version`
 * changes only when something changed, so the table re-renders only then.
 */
export function mergeKeysetPage(cache: RowCache, page: LaunchSessionRowsPage | null | undefined): RowCache {
  if (!page) return cache;
  let rows = cache.rows;
  let changed = false;
  let maxRev = cache.candRev;
  for (const r of page.rows ?? []) {
    const id = String(r?.candidate_id ?? "");
    if (!id) continue;
    const rev = Number(r.rev ?? 0);
    if (rev > maxRev) maxRev = rev;
    const prev = rows[id];
    if (prev && Number(prev.rev ?? 0) > rev) continue;
    if (!changed) rows = { ...rows };
    rows[id] = r;
    changed = true;
  }
  const next = page.next && Number.isFinite(Number(page.next.after_rev))
    ? { after_rev: Number(page.next.after_rev), after_id: String(page.next.after_id ?? "") }
    : cache.cursor;
  // The session's cand_rev counts as seen only once the cursor has caught up
  // (no more pages); a partial read keeps the next poll fetching.
  const pageRev = !page.has_more && Number.isFinite(Number(page.cand_rev)) ? Number(page.cand_rev) : -1;
  const candRev = Math.max(maxRev, pageRev);
  const cursorMoved = next.after_rev !== cache.cursor.after_rev || next.after_id !== cache.cursor.after_id;
  if (!changed && !cursorMoved && candRev === cache.candRev) return cache;
  return { rows, cursor: next, candRev, version: changed ? cache.version + 1 : cache.version };
}

export type KeysetCursor = { after_rev: number; after_id: string };

/**
 * The keyset position just BEFORE each row of a page: the request's cursor for
 * the first row, the previous row's (rev, candidate_id) for the others. A GET
 * with that cursor and limit=1 returns exactly that row (rows are ordered by
 * (rev, candidate_id) and a later write only ever lands after every existing
 * position) — how one row's full record (résumé text) is fetched while the
 * table holds light rows. A row updated since comes back as another id; the
 * caller then searches forward.
 */
export function keysetBeforeCursors(
  requestCursor: KeysetCursor,
  page: Pick<LaunchSessionRowsPage, "rows"> | null | undefined,
): Record<string, KeysetCursor> {
  const out: Record<string, KeysetCursor> = {};
  let before: KeysetCursor = { after_rev: Number(requestCursor.after_rev) || 0, after_id: String(requestCursor.after_id ?? "") };
  for (const r of page?.rows ?? []) {
    const id = String(r?.candidate_id ?? "");
    if (!id) continue;
    out[id] = before;
    before = { after_rev: Number(r.rev ?? 0), after_id: id };
  }
  return out;
}

/** At most this many ids per `GET …/candidates?ids=` (the server answers 422 above it). */
export const MAX_ROW_IDS = 50;

export interface CandidatesQuery {
  states?: readonly string[];
  view?: "table" | "full" | "failed";
  limit?: number;
  offset?: number;
  afterRev?: number;
  afterId?: string;
  /** Read these rows by candidate id (deduplicated; the first MAX_ROW_IDS are sent). */
  ids?: readonly string[];
}

/** The `GET /launch-sessions/{id}/candidates` query string: "" or "?…". */
export function candidatesQueryString(q: CandidatesQuery = {}): string {
  const qs = new URLSearchParams();
  if (q.states && q.states.length) qs.set("state", q.states.join(","));
  if (q.view) qs.set("view", q.view);
  if (q.limit) qs.set("limit", String(q.limit));
  if (q.offset) qs.set("offset", String(q.offset));
  if (typeof q.afterRev === "number") {
    qs.set("after_rev", String(q.afterRev));
    qs.set("after_id", q.afterId ?? "");
  }
  const ids = [...new Set((q.ids ?? []).map((i) => String(i ?? "").trim()).filter(Boolean))].slice(0, MAX_ROW_IDS);
  if (ids.length) qs.set("ids", ids.join(","));
  const out = qs.toString();
  return out ? `?${out}` : "";
}

/**
 * One row's FULL record (with résumé text — the table holds light rows).
 *  1. By id (`view=full&ids=<id>&limit=1`): needs no keyset position, so it
 *     also works for a table restored from storage after a reload, whose
 *     session this page never followed. An answer with `total: 0` is final
 *     (the row is not in that session).
 *  2. Fallback — an API without `ids` (it ignores the parameter and answers
 *     a page of other rows) or a failed read: the keyset position just
 *     before the row with limit 1, then a short forward scan from that
 *     position, else from the row's cached rev, else from the start.
 * null when it can't be found.
 */
export async function findFullRow(args: {
  candidateId: string;
  read: (q: CandidatesQuery) => Promise<LaunchSessionRowsPage | null>;
  known?: KeysetCursor | null;
  cachedRev?: number;
  scanLimit?: number;
  scanPages?: number;
}): Promise<Record<string, any> | null> {
  const { candidateId, read } = args;
  const find = (page: LaunchSessionRowsPage | null) => page?.rows?.find((r) => r.candidate_id === candidateId);
  const byId = await read({ view: "full", ids: [candidateId], limit: 1 });
  const direct = find(byId);
  if (direct) return direct.row ?? null;
  if (byId && Number(byId.total) === 0) return null;
  const known = args.known ?? null;
  if (known) {
    const hit = find(await read({ view: "full", limit: 1, afterRev: known.after_rev, afterId: known.after_id }));
    if (hit) return hit.row ?? null;
  }
  const rev = Number(args.cachedRev);
  let cursor: KeysetCursor = known ?? { after_rev: Number.isFinite(rev) ? rev : 0, after_id: "" };
  for (let page = 0; page < (args.scanPages ?? 5); page++) {
    const data = await read({ view: "full", limit: args.scanLimit ?? 10, afterRev: cursor.after_rev, afterId: cursor.after_id });
    if (!data) return null;
    const hit = find(data);
    if (hit) return hit.row ?? null;
    if (!data.has_more || !data.next) return null;
    cursor = data.next;
  }
  return null;
}

/** The résumé text of a full candidate row (the browser's stream row), "" when it has none. */
export const resumeTextOfRow = (row: Record<string, any> | null | undefined): string =>
  String(row?.resume_text || row?.resumeText || row?.data?.resume_text || "");

/** Fetch rows when the session reports writes this cache has not seen (or on the very first load). */
export const rowsAreStale = (cache: RowCache, view: Pick<LaunchSessionView, "cand_rev"> | null | undefined): boolean =>
  cache.candRev < 0 || Number(view?.cand_rev ?? 0) > cache.candRev;

export const rowList = (cache: RowCache): LaunchSessionRow[] => Object.values(cache.rows);

/** Ids of the rows Pairbot created no interview for (failed, reason pairbot_rejected). */
export const noInterviewIds = (rows: readonly LaunchSessionRow[]): Set<string> =>
  new Set(rows.filter((r) => r.state === "failed" && String(r.reason ?? "") === "pairbot_rejected").map((r) => r.candidate_id));

// ── Contact layers (services/launch_sessions/contacts.py) ───────────────────

const clean = (v: unknown): string => String(v ?? "").trim();

// Machine cancel reasons the server writes itself, in the recruiter's words.
const CANCEL_REASON_TEXT: Record<string, string> = {
  queue_expired: "it waited too long for a free launch slot — please launch again",
};

/** A session's cancel_reason for a toast ("" when there is none). */
export const cancelReasonText = (reason: unknown): string => {
  const r = clean(reason);
  return CANCEL_REASON_TEXT[r] ?? r;
};

/**
 * The contact the browser would launch with: QA-confirmed FINAL contacts
 * replace both channels (empty = none); otherwise the lookup override with
 * the recruiter's provided values on top (an empty provided field keeps the
 * lookup's — mergeContactOverrides).
 */
export function sessionContactOverlay(contact: Record<string, any> | null | undefined): { phone?: string; email?: string; final: boolean } {
  const c = contact ?? {};
  if (c.final && typeof c.final === "object") {
    return { phone: clean(c.final.phone), email: clean(c.final.email), final: true };
  }
  const override = c.override && typeof c.override === "object" ? c.override : {};
  const provided = c.provided && typeof c.provided === "object" ? c.provided : {};
  const phone = clean(provided.phone) || clean(override.phone);
  const email = clean(provided.email) || clean(override.email);
  return { ...(phone ? { phone } : {}), ...(email ? { email } : {}), final: false };
}

const HIDDEN_ROW_STATES = new Set(["dropped", "handed_off"]);
const HIDDEN_REASON_KINDS = new Set(["merged", "identity_duplicate", "duplicate_id"]);

/** Is this session row part of the Step-5 table (not dropped / merged away / handed off)? */
export function isTableRow(r: LaunchSessionRow): boolean {
  if (HIDDEN_ROW_STATES.has(String(r.state))) return false;
  const kind = String(r.reason ?? "").split(":", 1)[0];
  return !HIDDEN_REASON_KINDS.has(kind);
}

/** lib/launch-rules.ts isPlaceholderEmailUI, restated (the de-dup test pins the fast path to it). */
const isPlaceholderEmail = (e: string): boolean => {
  const v = e.trim().toLowerCase();
  if (!v || !v.includes("@")) return true;
  if ((v.split("@").pop() || "") === "jobdiva.com") return true;
  return v.endsWith("@noemail.pair.ai");
};

/** deduplicateCandidatesUI's phone normalisation (≥ 7 digits, ≥ 4 distinct, last 10). */
const dedupePhone = (p: unknown): string => {
  const digits = String(p || "").replace(/\D/g, "");
  if (digits.length < 7 || new Set(digits.split("")).size < 4) return "";
  return digits.slice(-10);
};

/**
 * Could deduplicateCandidatesUI merge anything in `rows`? O(n). Uses a LOOSER
 * superset of its identity keys — the same email (placeholders such as
 * "Available upon request" / @jobdiva.com ignored, as it does) and LinkedIn
 * keys, the same normalised phone WITHOUT the name condition — so "false"
 * proves the browser de-dup would return the rows unchanged (no shared key →
 * every row is pushed as-is, in order).
 */
export function mayHaveIdentityDuplicates(rows: readonly any[]): boolean {
  const seen = new Set<string>();
  for (const c of rows) {
    if (!c || typeof c !== "object") continue;
    const keys: string[] = [];
    const email = String(c.email || c.data?.email || c.enhanced_info?.email || "").trim().toLowerCase();
    if (email && !isPlaceholderEmail(email)) keys.push(`e:${email}`);
    const phone = dedupePhone(c.phone || c.data?.phone || c.enhanced_info?.phone);
    if (phone) keys.push(`p:${phone}`);
    const li = String(c.profile_url || c.data?.urls?.linkedin || c.linkedin_url || "").trim().toLowerCase();
    if (li.includes("linkedin.com")) keys.push(`l:${li.split("?")[0].replace(/\/+$/, "")}`);
    for (const k of new Set(keys)) {
      if (seen.has(k)) return true;
      seen.add(k);
    }
  }
  return false;
}

/** `dedupe(rows)` only when it could change something (it is O(n²) in the browser). */
export const dedupeIfNeeded = (rows: any[], dedupe: (rows: any[]) => any[]): any[] =>
  mayHaveIdentityDuplicates(rows) ? dedupe(rows) : rows;

/**
 * The Step-5 table from a full-search session's rows: table rows in pool
 * order, each the server's candidate row (the browser's own stream row,
 * without résumé text / LinkedIn profile) with found / confirmed contacts
 * applied the way the legacy flow wrote them back (`override.x || c.x`),
 * then the browser's cross-source de-dup (`dedupe`, injected:
 * deduplicateCandidatesUI) as a safety net over the server's.
 */
export function tableCandidatesFromRows(cache: RowCache, dedupe: (rows: any[]) => any[]): any[] {
  const list = rowList(cache)
    .filter((r) => isTableRow(r) && r.row && typeof r.row === "object")
    .sort((a, b) => {
      const ao = Number.isFinite(Number(a.ord)) && a.ord !== null ? Number(a.ord) : Number.MAX_SAFE_INTEGER;
      const bo = Number.isFinite(Number(b.ord)) && b.ord !== null ? Number(b.ord) : Number.MAX_SAFE_INTEGER;
      return ao - bo || (a.candidate_id < b.candidate_id ? -1 : a.candidate_id > b.candidate_id ? 1 : 0);
    })
    .map((r) => {
      const row = r.row as Record<string, any>;
      const overlay = sessionContactOverlay(r.contact);
      if (overlay.final) return { ...row, phone: overlay.phone || "", email: overlay.email || "" };
      if (!overlay.phone && !overlay.email) return row;
      return { ...row, phone: overlay.phone || row.phone, email: overlay.email || row.email };
    });
  return dedupeIfNeeded(list, dedupe);
}

/**
 * Keep what the browser resolved itself on rows the server re-sends: the
 * open-to-work poller writes `open_to_work: true|false` into the table, and a
 * server row (still "pending") must not undo it — that would re-arm the poller
 * on every sync. Matched by row id, else by profile URL (what the poller keys on).
 */
export function keepResolvedOpenToWork(prev: readonly any[], next: any[]): any[] {
  const byId = new Map<string, boolean>();
  const byUrl = new Map<string, boolean>();
  for (const c of prev) {
    const otw = c?.open_to_work;
    if (otw !== true && otw !== false) continue;
    const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
    if (id) byId.set(id, otw);
    const url = String(c.profile_url || "");
    if (url) byUrl.set(url, otw);
  }
  if (byId.size === 0 && byUrl.size === 0) return next;
  return next.map((c) => {
    if (!c || c.open_to_work === true || c.open_to_work === false) return c;
    const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
    const otw = byId.get(id) ?? byUrl.get(String(c.profile_url || ""));
    return otw === undefined ? c : { ...c, open_to_work: otw };
  });
}

// ── Session → Step-5 UI ─────────────────────────────────────────────────────

export type SessionUiPhase =
  | "collecting"
  | "sourcing"
  | "selecting"
  | "enriching"
  | "review"
  | "launching"
  | "completed"
  | "failed"
  | "cancelled";

export const isTerminal = (view: Pick<LaunchSessionView, "status" | "terminal"> | null | undefined): boolean =>
  Boolean(view && (view.terminal || TERMINAL_STATUSES.includes(view.status as LaunchSessionStatus)));

const LAUNCH_COMPLETE_STAGES = new Set(["provisioning", "done"]);

/**
 * The launch is over: terminal, or past S7 (side effects fired, every row
 * final; only JobDiva provisioning / the wrap-up left). The browser shows the
 * result, opens the missing-contacts modal and allows a retry / a new launch
 * from here, without waiting for `terminal`.
 */
export const isLaunchComplete = (
  view: Pick<LaunchSessionView, "status" | "terminal" | "launch_complete" | "stage"> | null | undefined,
): boolean => Boolean(view && (view.launch_complete || isTerminal(view) || LAUNCH_COMPLETE_STAGES.has(String(view.stage))));

/** The outcome to report: the terminal status, "completed" for a launch that is over but still provisioning, else null. */
export function outcomeStatus(view: LaunchSessionView): "completed" | "failed" | "cancelled" | null {
  if (isTerminal(view)) {
    const st = String(view.status);
    return st === "failed" || st === "cancelled" ? st : "completed";
  }
  return isLaunchComplete(view) ? "completed" : null;
}

export function sessionUiPhase(view: LaunchSessionView): SessionUiPhase {
  const outcome = outcomeStatus(view);
  if (outcome) return outcome;
  const status = String(view.status);
  if (status === "collecting") return "collecting";
  if (status === "awaiting_input") return "review";
  switch (String(view.stage)) {
    case "created":
      return view.mode === "full_search" ? "sourcing" : "selecting";
    case "sourcing":
      return "sourcing";
    case "selecting":
      return "selecting";
    case "enriching":
      return "enriching";
    default:
      // review (confirmed, about to move on), splitting, saving, launching,
      // provisioning, done.
      return "launching";
  }
}

const sourcingDone = (view: LaunchSessionView): boolean =>
  Boolean(view.timestamps?.sourcing_done_at) || Boolean(view.sourcing?.done);

const cancelledOrStopping = (view: LaunchSessionView): boolean =>
  view.status === "cancelled" || Boolean(view.cancel_requested);

export interface SourcingUi {
  /** Drives isSearching (Stop Search, the pulsing status line). */
  searching: boolean;
  /**
   * "full" while streaming; "complete" ONLY when the search finished
   * (sourcing_done_at) — a search that was cancelled or failed before that
   * never is (parity #15: the next click must run the full search again, not
   * "launch remaining" over a partial pool); else "sampled". A launch
   * cancelled AFTER its search finished keeps "complete": the full pool is on
   * screen, so the next click launches the remainder (as closing the QA
   * review did in the browser flow) instead of paying for the search again.
   */
  phase: "full" | "complete" | "sampled";
  /** The search ended before it finished (stopped / failed): the status line says so and the boolean goes back to plan[0]. */
  stoppedEarly: boolean;
  statusText: string;
  sourceStatuses: Record<string, any>;
  criteriaUnconfigured: boolean;
  criteriaModalRequested: boolean;
  /** Index of the relaxation attempt on screen (booleanAttempts / generatedBoolean). */
  attemptIndex: number;
  attemptsTotal: number;
  detailFailedCount: number;
  errorCount: number;
  done: boolean;
  /** The relaxation chain (query + label per attempt) when the server reports it. */
  plan: Array<{ query: string; label: string }>;
}

export function sourcingUi(view: LaunchSessionView): SourcingUi {
  const s = view.sourcing ?? {};
  const ui = s.ui ?? {};
  const done = sourcingDone(view);
  const stopping = cancelledOrStopping(view);
  const searching =
    view.mode === "full_search" &&
    !done &&
    !stopping &&
    !isTerminal(view) &&
    (view.stage === "created" || view.stage === "sourcing");
  const total = Math.max(1, Number(s.attempts_total ?? view.input?.search_attempts ?? 1) || 1);
  const current = Number.isFinite(Number(s.current_attempt)) && s.current_attempt !== null
    ? Number(s.current_attempt)
    : Math.max(0, Number(s.next_attempt ?? 0) - 1);
  const attemptIndex = Math.min(total - 1, Math.max(0, current));
  let statusText = String(ui.search_status || "").trim() || "Connecting to source portals...";
  if (searching && attemptIndex > 0) statusText = `${statusText} (attempt ${attemptIndex + 1}/${total})`;
  if (searching && view.status === "queued" && typeof view.queue_position === "number") {
    statusText = view.queue_position > 0
      ? `Waiting for a free search slot — ${view.queue_position} search${view.queue_position === 1 ? "" : "es"} ahead…`
      : "Starting the search…";
  }
  const stoppedEarly = view.mode === "full_search" && !done && (stopping || view.status === "failed");
  if (stoppedEarly) statusText = stopping ? "Search stopped" : "Search failed";
  const phase: SourcingUi["phase"] = searching ? "full" : done ? "complete" : "sampled";
  return {
    searching,
    phase,
    stoppedEarly,
    statusText,
    sourceStatuses: ui.source_statuses && typeof ui.source_statuses === "object" ? ui.source_statuses : {},
    criteriaUnconfigured: Boolean(ui.criteria_unconfigured),
    criteriaModalRequested: Boolean(ui.criteria_modal_requested),
    attemptIndex,
    attemptsTotal: total,
    detailFailedCount: Math.max(0, Number(ui.detail_failed_count ?? 0) || 0),
    errorCount: Math.max(0, Number(ui.error_count ?? 0) || 0),
    done,
    plan: (s.plan ?? [])
      .filter((p) => p && typeof p.boolean_string === "string")
      .map((p) => ({ query: String(p.boolean_string), label: String(p.label ?? "") })),
  };
}

const n = (v: unknown): number => {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
};

/** Per-state / per-reason tallies of the rows this tab has (live) — falls back to the session's counts. */
export function tallyRows(rows: readonly LaunchSessionRow[]): {
  states: Record<string, number>;
  reasons: Record<string, Record<string, number>>;
} {
  const states: Record<string, number> = {};
  const reasons: Record<string, Record<string, number>> = {};
  for (const r of rows) {
    const st = String(r.state);
    states[st] = (states[st] || 0) + 1;
    const kind = String(r.reason ?? "").split(":", 1)[0];
    const bucket = (reasons[st] ||= {});
    bucket[kind] = (bucket[kind] || 0) + 1;
  }
  return { states, reasons };
}

export interface SessionOutcome {
  sent: number;
  alreadyLaunched: number;
  /** Rows /candidates/save stored (legacy totalSaved; S6's total). */
  saved: number;
  /** Save / launch failures — what the browser counted as failed (no Pairbot per-person rejections). */
  failed: number;
  /** People Pairbot created no interview for (failed rows, reason pairbot_rejected). */
  noInterview: number;
  needsContact: number;
  unconfirmed: number;
  launched: number;
  selected: number;
  dncSkipped: number;
  hardFilterSkipped: number;
  excludedAtLaunch: number;
}

const SENT_REASONS = ["sent", "reconciled", "unrecorded"];

/** Outcome numbers from the session counts, preferring the live row tallies when given. */
export function sessionOutcome(view: LaunchSessionView, tally?: ReturnType<typeof tallyRows> | null): SessionOutcome {
  const counts = view.counts ?? {};
  const useTally = Boolean(tally && Object.keys(tally.states).length > 0);
  const states = useTally ? tally!.states : counts.states ?? {};
  const reasons = useTally ? tally!.reasons : counts.reasons ?? {};
  const launchedReasons = reasons.launched ?? {};
  const skipped = reasons.skipped ?? {};
  const sentFromReasons = SENT_REASONS.reduce((acc, k) => acc + n(launchedReasons[k]), 0);
  const noInterview = useTally
    ? n(reasons.failed?.pairbot_rejected)
    : n(counts.no_interview ?? reasons.failed?.pairbot_rejected);
  const reachedSave = n(states.saved) + n(states.launching) + n(states.launched) + n(states.unconfirmed) + noInterview;
  return {
    sent: useTally ? sentFromReasons : n(counts.sent ?? sentFromReasons),
    alreadyLaunched: useTally ? n(launchedReasons.already_launched) : n(counts.already_launched ?? launchedReasons.already_launched),
    saved: Math.max(reachedSave, n(counts.saved_total)),
    failed: Math.max(0, n(states.failed) - noInterview),
    noInterview,
    needsContact: n(states.needs_contact),
    unconfirmed: n(states.unconfirmed),
    launched: n(states.launched),
    selected: n(counts.selected ?? counts.selection?.selected),
    dncSkipped: n(skipped.dnc),
    hardFilterSkipped: n(skipped.hard_filter),
    excludedAtLaunch: n(skipped.excluded),
  };
}

/**
 * Redirect to the rank list iff the launch finished clean: nothing failed
 * (save / launch failures — a person Pairbot created no interview for is not
 * a failure, as in the browser flow), something was saved, and nobody is
 * waiting for contact details (parity #10). Evaluated once the launch is over
 * (`launch_complete`), not after the JobDiva provisioning that follows.
 * Otherwise the progress modal stays (failures: CSV + Retry) or the
 * missing-contacts modal opens.
 */
export function shouldRedirectToRankings(view: LaunchSessionView, tally?: ReturnType<typeof tallyRows> | null): boolean {
  if (outcomeStatus(view) !== "completed") return false;
  const o = sessionOutcome(view, tally);
  return o.failed === 0 && o.saved > 0 && o.needsContact === 0;
}

// The progress modal (components/launch-pair-progress-modal.tsx LaunchPairProgress),
// restated structurally.
export interface SessionProgress {
  open: boolean;
  phase: "idle" | "enriching" | "launching" | "completed" | "failed";
  totalCandidates: number;
  batchSize: number;
  enrichTotal: number;
  enrichDone: number;
  enrichSucceeded: number;
  enrichAlreadyReachable: number;
  enrichMissingLinkedIn: number;
  enrichNoContact: number;
  enrichFailed: number;
  hardFilterSkipped: number;
  hardFilterSkippedNames: string[];
  batches: Array<{
    index: number;
    size: number;
    status: "pending" | "saving" | "engaging" | "completed" | "failed";
    savedCount: number;
    dncSkipped: number;
    engageSent: number;
    alreadySent: number;
    errorMessage?: string;
  }>;
  currentBatchIndex: number;
  totalSaved: number;
  totalEngaged: number;
  totalFailedBatches: number;
  jobIdForRelaunch?: string;
  finalMessage?: string;
  /** Why a running launch is not moving right now (waiting to resume / queued for a worker). */
  statusNote?: string;
}

const ENTERED_SAVE = new Set(["ready", "saved", "launching", "launched", "unconfirmed", "failed"]);

/** The save batches (25 rows each) the modal lists, from the rows' batch_index and state. */
export function batchesFromRows(rows: readonly LaunchSessionRow[], saveBatchSize: number): SessionProgress["batches"] {
  const size = Math.max(1, Math.floor(saveBatchSize));
  const entered = rows
    .filter((r) => ENTERED_SAVE.has(String(r.state)) && !(r.state === "failed" && !r.failure_stage && r.batch_index == null))
    .sort((a, b) => n(a.ord) - n(b.ord) || (a.candidate_id < b.candidate_id ? -1 : 1));
  const groups = new Map<number, LaunchSessionRow[]>();
  const pending: LaunchSessionRow[] = [];
  for (const r of entered) {
    if (r.batch_index !== null && r.batch_index !== undefined && Number.isFinite(Number(r.batch_index))) {
      const idx = Number(r.batch_index);
      const g = groups.get(idx) ?? [];
      g.push(r);
      groups.set(idx, g);
    } else {
      pending.push(r);
    }
  }
  const indices = [...groups.keys()].sort((a, b) => a - b);
  let nextIndex = indices.length ? indices[indices.length - 1] + 1 : 0;
  for (const part of chunk(pending, size)) groups.set(nextIndex++, part);
  return [...groups.keys()]
    .sort((a, b) => a - b)
    .map((idx, position) => {
      const g = groups.get(idx) ?? [];
      const rejected = (r: LaunchSessionRow) => r.state === "failed" && String(r.reason ?? "") === "pairbot_rejected";
      const failedRows = g.filter((r) => r.state === "failed" && !rejected(r));
      const launchedRows = g.filter((r) => r.state === "launched");
      const engageSent = launchedRows.filter((r) => SENT_REASONS.includes(String(r.reason ?? "").split(":", 1)[0])).length;
      const alreadySent = launchedRows.filter((r) => String(r.reason ?? "").startsWith("already_launched")).length;
      const savedCount = g.filter((r) => ["saved", "launching", "launched", "unconfirmed"].includes(String(r.state)) || rejected(r)).length
        + failedRows.filter((r) => r.failure_stage === "engage").length;
      let status: SessionProgress["batches"][number]["status"];
      let errorMessage: string | undefined;
      if (failedRows.length > 0) {
        status = "failed";
        const f = failedRows[0];
        errorMessage = `${f.failure_stage === "save" ? "Save" : "Engage"} failed: ${clean(f.error) || clean(f.reason) || "unknown error"}`;
      } else if (g.every((r) => r.state === "launched" || r.state === "unconfirmed" || rejected(r))) {
        status = "completed";
      } else if (g.some((r) => r.state === "saved" || r.state === "launching")) {
        status = "engaging";
      } else {
        status = "pending";
      }
      return { index: position, size: g.length, status, savedCount, dncSkipped: 0, engageSent, alreadySent, errorMessage };
    });
}

const ENRICH_BUCKETS: Record<string, keyof Pick<SessionProgress, "enrichSucceeded" | "enrichAlreadyReachable" | "enrichMissingLinkedIn" | "enrichNoContact" | "enrichFailed">> = {
  succeeded: "enrichSucceeded",
  already_reachable: "enrichAlreadyReachable",
  missing_linkedin: "enrichMissingLinkedIn",
  no_contact: "enrichNoContact",
  not_eligible: "enrichNoContact",
  failed: "enrichFailed",
};

/** Lookups announced by the latest `enrich_started` event (the in-stage total). */
function announcedLookups(view: LaunchSessionView): number {
  const events = view.events_tail ?? [];
  for (let i = events.length - 1; i >= 0; i--) {
    const p = events[i]?.payload;
    if (p?.type === "enrich_started") return n(p.lookups);
  }
  return 0;
}

/**
 * The launch progress modal for a session, in the legacy modal's vocabulary:
 * enrichment counters (live from the rows while S3 runs, the stage's summary
 * after), save batches, saved / sent / failed. Closed while sourcing (the
 * table shows that), during the QA review (that modal shows), on cancel, and
 * on a completed session that launched nobody or only parked rows for the
 * missing-contacts modal.
 */
export function sessionToProgress(
  view: LaunchSessionView,
  rows: readonly LaunchSessionRow[],
  opts: {
    saveBatchSize: number;
    jobRef?: string;
    displayName?: (row: any) => string;
    /** Clock time for "resumes at …" (default: the browser's short local time). */
    formatTime?: (ms: number) => string;
  },
): SessionProgress {
  const phase = sessionUiPhase(view);
  const tally = rows.length ? tallyRows(rows) : null;
  const outcome = sessionOutcome(view, tally);
  const counts = view.counts ?? {};

  // Enrichment.
  let enrich = {
    enrichTotal: 0, enrichDone: 0, enrichSucceeded: 0, enrichAlreadyReachable: 0,
    enrichMissingLinkedIn: 0, enrichNoContact: 0, enrichFailed: 0,
  };
  if (counts.enrich) {
    const e = counts.enrich;
    enrich = {
      enrichTotal: n(e.total),
      enrichDone: n(e.total),
      enrichSucceeded: n(e.succeeded),
      enrichAlreadyReachable: n(e.already_reachable),
      enrichMissingLinkedIn: n(e.missing_linkedin),
      enrichNoContact: n(e.no_contact),
      enrichFailed: n(e.failed),
    };
  } else if (phase === "enriching") {
    let decided = 0;
    for (const r of rows) {
      const c = r.contact ?? {};
      if (!c.done || !c.outcome) continue;
      enrich.enrichDone += 1;
      const bucket = ENRICH_BUCKETS[String(c.outcome)];
      if (bucket) enrich[bucket] += 1;
      if (!c.looked_up && (c.outcome === "missing_linkedin" || c.outcome === "already_reachable") && !c.phone_provider && !c.email_provider) {
        decided += 1;
      }
    }
    enrich.enrichTotal = Math.max(enrich.enrichDone, announcedLookups(view) + decided);
  }

  // Skips shown in the modal's amber panel: client-company employees caught
  // before the save, plus what the launch checks excluded server-side.
  const names: string[] = [];
  const name = (r: LaunchSessionRow) => (opts.displayName && r.row ? opts.displayName(r.row) : "") || r.candidate_id;
  for (const r of rows) {
    if (r.state !== "skipped") continue;
    const reason = String(r.reason ?? "");
    if (reason === "hard_filter") names.push(name(r));
    else if (reason.startsWith("excluded")) names.push(`${name(r)} (${reason.slice("excluded:".length) || "excluded"})`);
  }
  const hardFilterSkipped = Math.max(outcome.hardFilterSkipped + outcome.excludedAtLaunch, names.length);

  const batches = batchesFromRows(rows, opts.saveBatchSize);
  const failedBatches = batches.filter((b) => b.status === "failed").length;
  const current = batches.findIndex((b) => b.status !== "completed" && b.status !== "failed");
  const split = counts.split;
  const totalCandidates = split ? n(split.ready) : batches.reduce((a, b) => a + b.size, 0) || outcome.selected;

  let progressPhase: SessionProgress["phase"];
  let open: boolean;
  let finalMessage: string | undefined;
  switch (phase) {
    case "enriching":
      progressPhase = "enriching";
      open = true;
      break;
    case "selecting":
    case "launching":
    case "collecting":
      progressPhase = "launching";
      open = true;
      break;
    case "completed": {
      const handled = outcome.sent + outcome.alreadyLaunched + outcome.unconfirmed;
      progressPhase = outcome.failed > 0 && handled === 0 ? "failed" : "completed";
      if (outcome.failed > 0) {
        // Failures keep the modal: failed-candidate CSV + Retry.
        open = true;
        finalMessage = clean(view.error) || undefined;
      } else if (outcome.needsContact > 0) {
        // The missing-contacts modal takes over (legacy closes this one).
        open = false;
      } else if (outcome.saved > 0) {
        open = true;
      } else if (hardFilterSkipped > 0) {
        // Nothing to launch only because of client-company skips: show why.
        open = true;
        finalMessage = "No candidates launched — all selected candidates currently work at the client company.";
      } else {
        // Nothing selected / everything skipped: a toast, like today.
        open = false;
      }
      break;
    }
    case "failed":
      progressPhase = "failed";
      // A session that failed before any launch work (sourcing / selection)
      // is a toast, not a modal.
      open = !(view.stage === "created" || view.stage === "sourcing");
      finalMessage = clean(view.error) || "The launch stopped before it finished.";
      break;
    default:
      // sourcing, review, cancelled
      progressPhase = "idle";
      open = false;
  }

  return {
    open,
    phase: progressPhase,
    totalCandidates,
    batchSize: Math.max(1, Math.floor(opts.saveBatchSize)),
    ...enrich,
    hardFilterSkipped,
    hardFilterSkippedNames: names,
    batches,
    currentBatchIndex: current >= 0 ? current : batches.length - 1,
    totalSaved: outcome.saved,
    totalEngaged: outcome.sent,
    totalFailedBatches: failedBatches || (outcome.failed > 0 ? 1 : 0),
    jobIdForRelaunch: opts.jobRef || undefined,
    finalMessage,
    ...(open && progressPhase !== "completed" && progressPhase !== "failed" ? waitingNote(view, opts.formatTime) : {}),
  };
}

const shortTime = (ms: number): string => {
  try {
    return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  } catch {
    return new Date(ms).toISOString().slice(11, 16);
  }
};

/**
 * A running session released by its worker (`phase: "waiting"`: a retry
 * back-off, PAIR still creating the interviews, a deploy) or one queued for a
 * free worker shows nothing moving for minutes — say why, so nobody reads it
 * as stuck.
 */
export function waitingNote(view: LaunchSessionView, formatTime: (ms: number) => string = shortTime): { statusNote?: string } {
  if (isLaunchComplete(view)) return {};
  if (view.cancel_requested) return { statusNote: "Stopping — finishing what is already in flight; nobody else will be contacted." };
  if (view.status === "queued") {
    const ahead = typeof view.queue_position === "number" ? view.queue_position : null;
    return {
      statusNote: ahead && ahead > 0
        ? `Waiting for a free launch slot — ${ahead} launch${ahead === 1 ? "" : "es"} ahead…`
        : "Waiting for a free launch slot…",
    };
  }
  if (view.status === "running" && view.phase === "waiting") {
    const at = Date.parse(String(view.retry_at ?? ""));
    return {
      statusNote: Number.isFinite(at)
        ? `Waiting for PAIR — resumes automatically at ${formatTime(at)}.`
        : "Waiting for PAIR — resumes automatically shortly.",
    };
  }
  return {};
}

/**
 * GET …/candidates?view=failed rows → the failed-candidate CSV rows
 * (LaunchFailedCandidate). `exclude`: ids not to list (the people Pairbot
 * created no interview for — not failures in the browser flow, and a retry
 * would not change Pairbot's answer).
 */
export function failedRowsToCsv(
  rows: readonly any[] | null | undefined,
  exclude?: ReadonlySet<string> | null,
): FailedCandidateCsvRow[] {
  const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));
  const opt = (v: unknown) => (v === null || v === undefined || String(v).trim() === "" ? null : String(v));
  return (rows ?? [])
    .filter((r) => r && clean(r.candidate_id) && !exclude?.has(clean(r.candidate_id)))
    .map((r) => ({
      candidate_id: str(r.candidate_id),
      name: str(r.name) || str(r.candidate_id),
      email: opt(r.email),
      phone: opt(r.phone),
      source: str(r.source),
      headline: str(r.headline),
      location: str(r.location),
      experience_years: n(r.experience_years),
      match_score: n(r.match_score),
      skills: Array.isArray(r.skills) ? r.skills.map(str).filter(Boolean).join("; ") : str(r.skills),
      matched_skills: Array.isArray(r.matched_skills) ? r.matched_skills.map(str).filter(Boolean).join("; ") : str(r.matched_skills),
      resume_id: str(r.resume_id),
      profile_url: opt(r.profile_url),
      batch_index: Math.max(0, Math.floor(n(r.batch_index))),
      failure_stage: r.failure_stage === "save" ? "save" : "engage",
      error_message: str(r.error_message),
    }));
}

// ── The two contact modals (QA review, missing contacts) ────────────────────

// components/missing-contacts-modal.tsx MissingContactCandidate, restated.
export interface ContactModalCandidate {
  candidate_id: string;
  name: string;
  headline?: string;
  location?: string;
  source?: string;
  jobdiva_id?: string;
  needsPhone: boolean;
  needsEmail: boolean;
  currentPhone?: string;
  currentEmail?: string;
  lookedUp?: boolean;
  foundPhone?: string;
  foundPhoneBy?: string;
  foundEmail?: string;
  foundEmailBy?: string;
}

/**
 * QA review (awaiting_input): every selected row, both fields editable and
 * pre-filled with override-or-own contact, marked with what the lookup found
 * (the legacy reviewList).
 */
export function reviewCandidatesFromRows(
  rows: readonly LaunchSessionRow[],
  opts: { jobdivaId?: string; displayName: (row: any) => string; providerLabel: (p: string | null | undefined) => string },
): ContactModalCandidate[] {
  return [...rows]
    .filter((r) => r.state === "selected")
    .sort((a, b) => n(a.ord) - n(b.ord))
    .map((r) => {
      const row = r.row ?? {};
      const c = r.contact ?? {};
      const override = c.override && typeof c.override === "object" ? c.override : {};
      return {
        candidate_id: r.candidate_id,
        name: opts.displayName(row) || clean(row.name) || "Unnamed",
        headline: clean(row.title) || clean(row.headline),
        location: clean(row.location),
        source: clean(row.source ?? r.source),
        jobdiva_id: opts.jobdivaId || undefined,
        needsPhone: true,
        needsEmail: true,
        currentPhone: clean(r.current_phone),
        currentEmail: clean(r.current_email),
        lookedUp: Boolean(c.looked_up),
        foundPhone: clean(override.phone) || undefined,
        foundPhoneBy: opts.providerLabel(c.phone_provider),
        foundEmail: clean(override.email) || undefined,
        foundEmailBy: opts.providerLabel(c.email_provider),
      };
    });
}

/** Missing contacts (completed with parked rows): S5's needs-info entries. */
export function needsContactCandidatesFromRows(
  rows: readonly LaunchSessionRow[],
  opts: { jobdivaId?: string; displayName: (row: any) => string },
): ContactModalCandidate[] {
  return [...rows]
    .filter((r) => r.state === "needs_contact")
    .sort((a, b) => n(a.ord) - n(b.ord))
    .map((r) => {
      const row = r.row ?? {};
      const needs = r.contact?.needs && typeof r.contact.needs === "object" ? r.contact.needs : {};
      return {
        candidate_id: r.candidate_id,
        name: clean(needs.name) || opts.displayName(row) || "Unnamed",
        headline: clean(needs.headline) || clean(row.title) || clean(row.headline),
        location: clean(needs.location) || clean(row.location),
        source: clean(needs.source) || clean(row.source ?? r.source),
        jobdiva_id: clean(needs.jobdiva_id) || opts.jobdivaId || undefined,
        needsPhone: typeof needs.needsPhone === "boolean" ? needs.needsPhone : true,
        needsEmail: typeof needs.needsEmail === "boolean" ? needs.needsEmail : true,
        currentPhone: clean(needs.currentPhone) || clean(r.current_phone),
        currentEmail: clean(needs.currentEmail) || clean(r.current_email),
      };
    });
}

type Provided = Record<string, { phone?: string; email?: string }>;

const cleanContacts = (provided: Provided): Record<string, { phone?: string; email?: string }> => {
  const out: Record<string, { phone?: string; email?: string }> = {};
  for (const [id, v] of Object.entries(provided ?? {})) {
    const phone = clean(v?.phone);
    const email = clean(v?.email);
    out[id] = { ...(phone ? { phone } : {}), ...(email ? { email } : {}) };
  }
  return out;
};

/**
 * POST /confirm: the confirmed contacts are FINAL — exactly what the modal
 * returned (an omitted channel means none, never a fallback to the row's own
 * or the lookup's: QA must never dial a real candidate it overrode).
 */
export function buildConfirmBody(provided: Provided): { contacts: Record<string, { phone?: string; email?: string }>; candidate_ids: string[] } {
  const contacts = cleanContacts(provided);
  return { contacts, candidate_ids: Object.keys(contacts) };
}

/** POST /needs-contact: launch the rows the recruiter completed, skip the rest of the modal's rows. */
export function buildNeedsContactBody(
  provided: Provided,
  modalIds: readonly string[],
  idempotencyKey?: string,
): { contacts: Record<string, { phone?: string; email?: string }>; candidate_ids: string[]; skip_ids: string[]; idempotency_key?: string } {
  const contacts = cleanContacts(provided);
  const ids = Object.keys(contacts);
  const chosen = new Set(ids);
  return {
    contacts,
    candidate_ids: ids,
    skip_ids: modalIds.filter((id) => !chosen.has(id)),
    ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
  };
}

// ── Milestones: toasts and telemetry, each once per session ─────────────────

export type SessionMilestone =
  | "search_failed"
  | "search_finished"
  | "selection"
  | "enrich"
  | "split"
  | "review"
  | "outcome";

/** Milestones the session has reached (a page emits each one once). */
export function reachedMilestones(view: LaunchSessionView): SessionMilestone[] {
  const out: SessionMilestone[] = [];
  const counts = view.counts ?? {};
  const over = isLaunchComplete(view);
  // As handleRunSearch: a failed search reports candidate_search_failed, then
  // (its finally) candidate_search_finished.
  if (view.mode === "full_search" && view.status === "failed" && !sourcingDone(view)) out.push("search_failed");
  if (view.mode === "full_search" && (sourcingDone(view) || over)) out.push("search_finished");
  if (counts.selection || view.timestamps?.selection_done_at) out.push("selection");
  if (counts.enrich || view.timestamps?.enrich_done_at) out.push("enrich");
  if (counts.split) out.push("split");
  if (view.status === "awaiting_input") out.push("review");
  if (over) out.push("outcome");
  return out;
}

/**
 * The once-per-session marks a tab that ATTACHES mid-way (reload, another
 * tab, a 409) pre-sets, so it does not replay toasts / telemetry for what
 * already happened. The QA review and the outcome handling are left out:
 * those still need doing in the attaching tab.
 */
export function seedMarksForAttach(view: LaunchSessionView): string[] {
  const marks: string[] = reachedMilestones(view).filter((m) => m !== "review" && m !== "outcome");
  const ui = view.sourcing?.ui ?? {};
  if (ui.criteria_modal_requested) marks.push("criteria_modal");
  const errors = n(ui.error_count);
  if (errors > 0) marks.push(`search_error:${errors}`);
  return marks;
}

// ── Browser-wide once-marks (localStorage) ──────────────────────────────────
// Telemetry must go out once per session, not once per tab: a second tab (or
// a tab reopened later) that reattaches must not re-send what the first one
// sent. The tab-level marks (sessionStorage) keep deciding the toasts.

export type BrowserMarks = Record<string, { k: string[]; t: number }>;

const BROWSER_MARKS_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const BROWSER_MARKS_MAX_ENTRIES = 100;

export function parseBrowserMarks(raw: string | null | undefined): BrowserMarks {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: BrowserMarks = {};
    for (const [id, v] of Object.entries(parsed as Record<string, any>)) {
      if (!v || !Array.isArray(v.k)) continue;
      out[id] = { k: v.k.filter((x: unknown) => typeof x === "string"), t: Number(v.t) || 0 };
    }
    return out;
  } catch {
    return {};
  }
}

/** Drop entries older than 7 days, then keep the newest 100. */
export function pruneBrowserMarks(marks: BrowserMarks, nowMs: number): BrowserMarks {
  const fresh = Object.entries(marks)
    .filter(([, v]) => nowMs - v.t <= BROWSER_MARKS_MAX_AGE_MS)
    .sort((a, b) => b[1].t - a[1].t)
    .slice(0, BROWSER_MARKS_MAX_ENTRIES);
  return Object.fromEntries(fresh);
}

/** Mark `key` for session `id`; `added` = it was not marked before. */
export function addBrowserMark(marks: BrowserMarks, id: string, key: string, nowMs: number): { marks: BrowserMarks; added: boolean } {
  const entry = marks[id];
  if (entry?.k.includes(key)) return { marks, added: false };
  return { marks: { ...marks, [id]: { k: [...(entry?.k ?? []), key], t: nowMs } }, added: true };
}

/**
 * A tab that attaches to a session this BROWSER has no record of (a teammate's,
 * or one started on another device) marks what already happened as sent; a
 * session this browser already tracks keeps its own record (its tab may have
 * closed before sending everything — the reattaching tab sends the rest).
 */
export function seedBrowserMarks(marks: BrowserMarks, id: string, keys: readonly string[], nowMs: number): BrowserMarks {
  if (marks[id]) return marks;
  return { ...marks, [id]: { k: [...keys], t: nowMs } };
}

/** ms from the launch click to the end of the launch (server clock, both stamps). */
export function clickToDoneMs(view: LaunchSessionView): number | null {
  const t = view.timestamps ?? {};
  const start = Date.parse(String(t.clicked_at ?? ""));
  // A launch that is over but still provisioning JobDiva has no completed_at yet.
  const end = Date.parse(String(t.completed_at ?? t.last_batch_done_at ?? ""));
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
}

/** ms the sourcing stage took (candidate_search_finished.runtime_seconds). */
export function sourcingMs(view: LaunchSessionView): number | null {
  const t = view.timestamps ?? {};
  const start = Date.parse(String(t.sourcing_started_at ?? ""));
  const end = Date.parse(String(t.sourcing_done_at ?? t.completed_at ?? ""));
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
}

/** "N below 60%, M unscored" — the held-back note of the selection toasts. */
export function heldBackNote(selection: LaunchSessionCounts["selection"], minScore: number): string {
  if (!selection) return "";
  return [
    n(selection.below_floor) > 0 ? `${n(selection.below_floor)} below ${minScore}%` : "",
    n(selection.unscored) > 0 ? `${n(selection.unscored)} unscored` : "",
  ].filter(Boolean).join(", ");
}

/** The "Search complete — launching PAIR for …" toast, as handleSourceAndLaunch words it. */
export function selectionToast(view: LaunchSessionView, minScore: number): { message: string; type: "info" } | null {
  const sel = view.counts?.selection;
  if (!sel) return null;
  const selected = n(sel.selected ?? view.counts?.selected);
  const eligible = n(sel.eligible_total);
  const held = heldBackNote(sel, minScore);
  const lead = view.mode === "full_search" ? "Search complete — launching" : "Launching";
  if (selected === 0) {
    return {
      type: "info",
      message: view.mode === "full_search"
        ? `Search complete — no launchable candidates at ${minScore}%+${held ? ` (${held} held back)` : ""}, so nothing was launched.`
        : `No new candidates left to launch${held ? ` (${held} held back)` : ""}.`,
    };
  }
  if (view.mode !== "full_search") return null; // the click already said "Launching PAIR for N …"
  return {
    type: "info",
    message: eligible > selected
      ? `${lead} PAIR for the best ${selected} of ${eligible} candidates at ${minScore}%+ (your launch count). Raise the number or click Launch PAIR again for the rest.`
      : `${lead} PAIR for ${selected} candidate${selected === 1 ? "" : "s"} at ${minScore}%+${held ? ` (${held} held back)` : ""}…`,
  };
}

/** The end-of-launch toasts runLaunchPair shows today, in order (the last one stays on screen). */
export function completionToasts(
  view: LaunchSessionView,
  rows: readonly LaunchSessionRow[],
  opts: { displayName?: (row: any) => string } = {},
): Array<{ message: string; type: "success" | "info" | "error" }> {
  const out: Array<{ message: string; type: "success" | "info" | "error" }> = [];
  const tally = rows.length ? tallyRows(rows) : null;
  const o = sessionOutcome(view, tally);
  const plural = (k: number) => (k === 1 ? "" : "s");
  if (view.status === "cancelled") {
    out.push({
      type: "info",
      message: o.sent > 0
        ? `Launch stopped — ${o.sent} candidate${plural(o.sent)} already launched stay launched.`
        : `Launch stopped${cancelReasonText(view.cancel_reason) && !/by user|by recruiter/i.test(cancelReasonText(view.cancel_reason)) ? ` (${cancelReasonText(view.cancel_reason)})` : ""} — nothing more will be sent.`,
    });
    return out;
  }
  const already = rows.filter((r) => r.state === "launched" && String(r.reason ?? "").startsWith("already_launched"));
  if (already.length > 0) {
    const names = already.map((r) => (opts.displayName && r.row ? opts.displayName(r.row) : "") || r.candidate_id);
    out.push({ type: "info", message: `Skipped ${names.join(", ")} (Already Launched)` });
  }
  if (o.dncSkipped > 0) {
    out.push({ type: "info", message: `${o.dncSkipped} candidate${plural(o.dncSkipped)} skipped — Do Not Contact list match` });
  }
  if (o.excludedAtLaunch > 0) {
    const reasons = rows
      .filter((r) => r.state === "skipped" && String(r.reason ?? "").startsWith("excluded"))
      .slice(0, 3)
      .map((r) => String(r.reason ?? "").slice("excluded:".length) || "excluded");
    out.push({ type: "info", message: `${o.excludedAtLaunch} candidate${plural(o.excludedAtLaunch)} excluded by launch checks (${reasons.join(", ")})` });
  }
  if (o.noInterview > 0) {
    out.push({
      type: "info",
      message: `${o.noInterview} candidate${plural(o.noInterview)} not launched — PAIR created no interview (e.g. already interviewing for this role).`,
    });
  }
  if (o.unconfirmed > 0) {
    out.push({
      type: "info",
      message: `${o.unconfirmed} candidate${plural(o.unconfirmed)} sent to PAIR but not confirmed yet — they are re-checked automatically and appear in the rank list once confirmed.`,
    });
  }
  const handled = o.sent + o.alreadyLaunched;
  if (view.status === "failed") {
    out.push({ type: "error", message: `Launch PAIR failed${clean(view.error) ? `: ${clean(view.error)}` : ""}` });
  } else if (handled > 0 && o.failed === 0) {
    out.push({
      type: "success",
      message: o.sent > 0 ? `Launched PAIR for ${o.sent} candidate${plural(o.sent)}` : "All selected candidates were already launched",
    });
  } else if (handled > 0 && o.failed > 0) {
    out.push({ type: "info", message: `Launched ${o.sent} · ${o.failed} candidate${plural(o.failed)} failed${clean(view.error) ? ` (${clean(view.error)})` : ""}` });
  } else if (handled === 0 && o.failed === 0 && o.needsContact === 0 && o.unconfirmed === 0 && o.selected > 0) {
    out.push({ type: "info", message: "No candidates were launched (all were skipped or blocked)" });
  } else if (handled === 0 && o.failed > 0) {
    out.push({ type: "error", message: `Launch PAIR failed${clean(view.error) ? `: ${clean(view.error)}` : ""}` });
  }
  return out;
}

/** The contact-lookup toasts handleLaunchPairClick shows after enrichment (from counts.enrich). */
export function enrichToasts(
  view: LaunchSessionView,
  opts: { minScore: number; describeFound: (phoneBy: Record<string, number>, emailBy: Record<string, number>) => string },
): Array<{ message: string; type: "success" | "info" }> {
  const e = view.counts?.enrich;
  if (!e) return [];
  const out: Array<{ message: string; type: "success" | "info" }> = [];
  const plural = (k: number) => (k === 1 ? "" : "s");
  const succeeded = n(e.succeeded);
  if (succeeded > 0) {
    out.push({
      type: "success",
      message: `Contact lookup found ${opts.describeFound(e.phone_found_by ?? {}, e.email_found_by ?? {})} for ${succeeded} candidate${plural(succeeded)}.`,
    });
  }
  const issues = Object.entries(e.kipplo_issues ?? {}).sort((a, b) => n(b[1]) - n(a[1]));
  if (issues.length > 0 && n(issues[0][1]) > 0) {
    out.push({
      type: "info",
      message: `Kipplo gave no result for ${n(issues[0][1])} candidate${plural(n(issues[0][1]))} (${issues[0][0]}); the other providers were asked instead.`,
    });
  }
  const notEligible = n(e.not_eligible);
  const noContact = Math.max(0, n(e.no_contact) - notEligible);
  const bits = [
    n(e.missing_linkedin) > 0 ? `${n(e.missing_linkedin)} missing LinkedIn URL` : "",
    noContact > 0 ? `${noContact} still missing phone & email` : "",
    notEligible > 0 ? `${notEligible} not looked up (below ${opts.minScore}% or an ineligible source)` : "",
    n(e.failed) > 0 ? `${n(e.failed)} enrichment call failed` : "",
  ].filter(Boolean);
  if (bits.length > 0) out.push({ type: "info", message: `Enrichment summary: ${bits.join(" · ")}` });
  return out;
}

/** The duplicate-contact toasts of the ready / needs-contact split. */
export function splitToasts(view: LaunchSessionView): Array<{ message: string; type: "info" }> {
  const s = view.counts?.split;
  if (!s) return [];
  const out: Array<{ message: string; type: "info" }> = [];
  const plural = (k: number) => (k === 1 ? "" : "s");
  if (n(s.duplicate_email) > 0) {
    out.push({ type: "info", message: `${n(s.duplicate_email)} selected candidate${plural(n(s.duplicate_email))} share duplicate email addresses. PAIR needs a unique real email per candidate.` });
  }
  if (n(s.duplicate_phone) > 0) {
    out.push({ type: "info", message: `${n(s.duplicate_phone)} selected candidate${plural(n(s.duplicate_phone))} share duplicate phone numbers. PAIR needs a unique real phone per candidate.` });
  }
  return out;
}

const truncateText = (text: string, max: number): string => (text.length > max ? `${text.slice(0, max)}…` : text);

/**
 * The New Relic events a milestone emits — today's event names
 * (candidate_search_failed / _finished, auto_launch_selection,
 * launch_pair_clicked, launch_contact_lookup) plus launch_session_finished —
 * with job_ref + launch_session_id. Ids, counts and the boolean queries only
 * (no PII). Empty: nothing to send for this milestone.
 */
export function milestoneEvents(
  m: SessionMilestone,
  view: LaunchSessionView,
  ctx: {
    jobRef: string;
    minScore: number;
    launchCount: number;
    rows?: readonly LaunchSessionRow[];
    /** truncateForTelemetry (page.tsx); a plain cut by default. */
    truncate?: (text: string, max: number) => string;
  },
): Array<{ name: string; props: Record<string, unknown> }> {
  const props = sessionEventProps(view, ctx.jobRef);
  const cut = ctx.truncate ?? truncateText;
  const sum = (o?: Record<string, number>) => Object.values(o ?? {}).reduce((a, b) => a + n(b), 0);
  if (m === "search_failed") {
    return [{
      name: "job_wizard_step5_candidate_search_failed",
      props: { ...props, error: cut(clean(view.error) || "launch session failed while sourcing", 300) },
    }];
  }
  if (m === "search_finished") {
    const ms = sourcingMs(view);
    const attempts = view.sourcing?.attempts ?? [];
    const plan = view.sourcing?.plan ?? [];
    const queryOf = (idx: number) => cut(String(plan[idx]?.boolean_string ?? ""), 260);
    const perAttempt = attempts.reduce((acc, a) => acc + n(a.rows), 0);
    return [{
      name: "job_wizard_step5_candidate_search_finished",
      props: {
        ...props,
        status: String(view.status),
        // As handleRunSearch counted it: every attempt's rows (a person found
        // by two attempts counts twice); pool_size is the de-duplicated pool.
        candidates_found: attempts.length > 0 ? perAttempt : n(view.sourcing?.pool),
        pool_size: n(view.sourcing?.pool),
        qualified: n(view.sourcing?.qualified),
        runtime_seconds: ms === null ? null : Number((ms / 1000).toFixed(2)),
        runs_executed: attempts.length || 1,
        runs: attempts.map((a, idx) => ({
          attempt: idx + 1,
          label: a.label ?? "",
          query: queryOf(idx),
          results_count: n(a.rows),
          qualified: n(a.qualified),
        })),
        boolean_attempts: attempts.map((a, idx) => ({
          attempt: idx + 1,
          label: a.label ?? plan[idx]?.label ?? "",
          query: queryOf(idx),
        })),
      },
    }];
  }
  if (m === "selection") {
    const sel = view.counts?.selection ?? {};
    const selected = n(sel.selected ?? view.counts?.selected);
    const out: Array<{ name: string; props: Record<string, unknown> }> = [];
    // "Launch remaining" / the QA quick launch report their selection at the click.
    if (view.mode === "full_search") {
      out.push({
        name: "job_wizard_step5_auto_launch_selection",
        props: {
          ...props,
          pool_size: n(sel.pool),
          selected_count: selected,
          eligible_total: n(sel.eligible_total),
          below_floor: n(sel.below_floor),
          unscored: n(sel.unscored),
          min_score: ctx.minScore,
          launch_count: view.input?.launch_count ?? ctx.launchCount,
        },
      });
    }
    // The browser fired launch_pair_clicked when the selected rows went into
    // the Launch PAIR pipeline (only when there was something to launch).
    if (selected > 0) {
      out.push({
        name: "job_wizard_step5_launch_pair_clicked",
        props: {
          ...props,
          selected_candidates_count: selected,
          hard_filter_skipped: n(view.counts?.reasons?.skipped?.hard_filter),
        },
      });
    }
    return out;
  }
  if (m === "enrich") {
    const e = view.counts?.enrich;
    if (!e || n(e.total) <= 0) return [];
    return [{
      name: "job_wizard_step5_launch_contact_lookup",
      props: {
        ...props,
        looked_up: n(e.looked_up),
        found: n(e.succeeded),
        phones_found: sum(e.phone_found_by),
        emails_found: sum(e.email_found_by),
        phone_found_by: e.phone_found_by ?? {},
        email_found_by: e.email_found_by ?? {},
        kipplo_issues: e.kipplo_issues ?? {},
        not_eligible: n(e.not_eligible),
        failed: n(e.failed),
        qa_review: Boolean(view.require_review),
      },
    }];
  }
  if (m === "outcome") {
    const rows = ctx.rows ?? [];
    const o = sessionOutcome(view, rows.length ? tallyRows(rows) : null);
    const clickToDone = clickToDoneMs(view);
    return [{
      name: "job_wizard_step5_launch_session_finished",
      props: {
        ...props,
        status: outcomeStatus(view) ?? String(view.status),
        sent: o.sent,
        already_launched: o.alreadyLaunched,
        failed: o.failed,
        no_interview: o.noInterview,
        needs_contact: o.needsContact,
        unconfirmed: o.unconfirmed,
        selected: o.selected,
        ...(clickToDone !== null ? { click_to_done_ms: clickToDone } : {}),
      },
    }];
  }
  return [];
}

/**
 * The New Relic click → done measure, on this page's clock: null when the
 * click happened before this page loaded (a reattached tab) — the duration
 * would otherwise be clamped to the page's age. click_to_done_ms on the
 * finished event carries the true value either way.
 */
export function clickToDoneMeasure(view: LaunchSessionView, nowPerfMs: number): { start: number; end: number } | null {
  const ms = clickToDoneMs(view);
  if (ms === null || !Number.isFinite(nowPerfMs) || nowPerfMs - ms < 0) return null;
  return { start: nowPerfMs - ms, end: nowPerfMs };
}

/** New Relic props shared by every launch-session event (ids and counts only, no PII). */
export function sessionEventProps(view: LaunchSessionView, jobRef: string): Record<string, string | number | boolean> {
  return {
    step: 5,
    job_ref: jobRef,
    launch_session_id: view.id,
    mode: view.trigger === "launch_remaining" ? "launch_remaining" : view.mode === "full_search" ? "full_search" : String(view.trigger || "provided"),
    trigger: String(view.trigger || ""),
  };
}
