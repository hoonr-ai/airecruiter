"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getActiveUserEmail, launchSessionsApi, type LaunchSessionResult } from "@/lib/api";
import {
  addBrowserMark,
  classifyConfigResponse,
  classifyCreateResponse,
  decodeSessionHint,
  emptyRowCache,
  encodeSessionHint,
  failedRowsToCsv,
  findFullRow,
  isLaunchComplete,
  isSettledForPolling,
  isTerminal,
  keysetBeforeCursors,
  lookupRetryDelay,
  mergeKeysetPage,
  nextPollDelay,
  parseBrowserMarks,
  pickParkedOffer,
  planProvidedUpload,
  planReattach,
  pruneBrowserMarks,
  rowsAreStale,
  seedBrowserMarks,
  seedMarksForAttach,
  sessionHintKey,
  shouldLookForSessions,
  shouldRetryWrite,
  writeRetryDelay,
  type BrowserMarks,
  type CreateOutcome,
  type CreateSessionBody,
  type FailedCandidateCsvRow,
  type KeysetCursor,
  type LaunchSessionConfig,
  type LaunchSessionHint,
  type LaunchSessionRow,
  type LaunchSessionView,
  type RowCache,
} from "@/lib/launch-session";

// Watches a server launch session for Step 5 (apps/api/routers/launch_sessions.py).
// The rules — what to send, how to read answers, the back-off, the row merge,
// which session to reattach to — are pure functions in lib/launch-session.ts
// (unit-tested); this hook owns the side effects only: the config fetch,
// timers, fetches, visibility, and storage (the "which session was this tab
// watching" hint, the per-tab once-marks for toasts / modals, and the
// browser-wide once-marks for telemetry).
//
// Polling: GET the session every config.poll_ms; when its cand_rev moved, pull
// the changed rows with the keyset cursor (view=table) BEFORE publishing the
// new view, so a terminal view always comes with its final rows. Errors back
// off to 15 s, a hidden tab polls every 15 s, and it never gives up while
// mounted; it stops at a terminal status. Every request has a timeout
// (lib/api.ts lsFetch), so a black-holed connection can't freeze the loop.

/**
 * created / child: this tab clicked (or answered the parent's modal);
 * reattach: page load found the user's own session; attach: a click answered
 * 409 (another active session — possibly a teammate's); observe: the recruiter
 * chose to watch a teammate's launch from the banner.
 */
export type LaunchSessionOrigin = "created" | "reattach" | "attach" | "child" | "observe";

const CONFIG_WAIT_MS = 4000;
const ROW_PAGE_LIMIT = 500;
const ROW_PAGES_PER_TICK = 10;
const LOAD_PAGE_LIMIT = 200;
const LOAD_MAX_PAGES = 20;
const CREATE_ATTEMPTS = 4;
const WRITE_ATTEMPTS = 6;
// Uploads go in chunks of 25 (the /candidates/save batch size), under the
// server's own max_upload_chunk.
const UPLOAD_CHUNK = 25;
// A teammate's launch shown in the banner: re-read now and then so the banner
// goes away when it finishes.
const OTHER_POLL_MS = 15000;
// One row's full record (résumé text) when its keyset position moved: scan
// forward this far (view=full rows are heavy).
const FULL_ROW_SCAN_LIMIT = 10;
const FULL_ROW_SCAN_PAGES = 5;
// Keyset positions are kept for the last few sessions (the table's session
// stays on screen while a child session is followed).
const BEFORE_CURSOR_SESSIONS = 4;

const ONCE_PREFIX = "pair:launch-session-once:";
const BROWSER_MARKS_KEY = "pair:launch-session-telemetry";

function tabStore(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

function readStore(key: string): string | null {
  try {
    return tabStore()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function writeStore(key: string, value: string): void {
  try {
    tabStore()?.setItem(key, value);
  } catch {
    // private window / quota: the hint is only a convenience
  }
}

function removeStore(key: string): void {
  try {
    tabStore()?.removeItem(key);
  } catch {
    // ignore
  }
}

function readBrowserStore(): BrowserMarks {
  try {
    return typeof window !== "undefined" ? parseBrowserMarks(window.localStorage.getItem(BROWSER_MARKS_KEY)) : {};
  } catch {
    return {};
  }
}

function writeBrowserStore(marks: BrowserMarks): void {
  try {
    if (typeof window !== "undefined") window.localStorage.setItem(BROWSER_MARKS_KEY, JSON.stringify(marks));
  } catch {
    // blocked / quota: the in-memory copy still dedupes this page
  }
}

/** The stored marks with this page's in-memory copy folded in (storage may be unavailable). */
function mergeBrowserMarks(stored: BrowserMarks, mem: BrowserMarks): BrowserMarks {
  const out: BrowserMarks = { ...stored };
  for (const [id, v] of Object.entries(mem)) {
    const prev = out[id];
    out[id] = { k: [...new Set([...(prev?.k ?? []), ...v.k])], t: Math.max(prev?.t ?? 0, v.t) };
  }
  return out;
}

const currentUserEmail = (): string => {
  try {
    return String(getActiveUserEmail() ?? "").trim().toLowerCase();
  } catch {
    return "";
  }
};

const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** `p`, or null after `ms` (the timer is cleared either way). */
async function withinMs<T>(p: Promise<T>, ms: number): Promise<T | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([p, new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), ms); })]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * A write retried on 429 (Retry-After honoured), 5xx and network errors —
 * never on 503 {code: disabled} (shouldRetryWrite); no more retries once the
 * page is gone.
 */
async function writeWithRetry<T>(
  send: () => Promise<LaunchSessionResult<T>>,
  alive: () => boolean,
): Promise<LaunchSessionResult<T>> {
  let res = await send();
  for (let attempt = 1; attempt < WRITE_ATTEMPTS && !res.ok && shouldRetryWrite(res) && alive(); attempt++) {
    await delay(writeRetryDelay(attempt, res.retryAfterMs));
    if (!alive()) break;
    res = await send();
  }
  return res;
}

export type UseLaunchSessionOptions = {
  /** The job's JobDiva ref or numeric id (the server resolves either). Empty = idle. */
  jobRef: string;
  /** Look for a session to reattach to (Step 5 is on screen). */
  reattach: boolean;
};

export function useLaunchSession({ jobRef, reattach }: UseLaunchSessionOptions) {
  const ref = String(jobRef ?? "").trim();
  const jobRefRef = useRef(ref);
  useEffect(() => {
    jobRefRef.current = ref;
  }, [ref]);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const alive = useCallback(() => mountedRef.current, []);

  // ── config ──────────────────────────────────────────────────────────────
  // Asked on mount and again with back-off while the failures are blips
  // (network / 429 / 5xx — a deploy, the office's shared rate limit): one blip
  // must not pin the page to the browser flow, nor hide a running launch.
  // An explicit answer (enabled:false, 404, 401/403) is final.
  const [configState, setConfigState] = useState<"loading" | "ready" | "failed">("loading");
  const [config, setConfig] = useState<LaunchSessionConfig | null>(null);
  const configRef = useRef<LaunchSessionConfig | null>(null);
  const configTransientRef = useRef(true);
  const configInFlightRef = useRef<Promise<LaunchSessionConfig | null> | null>(null);
  const disabledRef = useRef(false);

  const fetchConfig = useCallback((): Promise<LaunchSessionConfig | null> => {
    if (configRef.current) return Promise.resolve(configRef.current);
    if (configInFlightRef.current) return configInFlightRef.current;
    const p = launchSessionsApi
      .config()
      .then((res) => classifyConfigResponse(res))
      .catch(() => ({ config: null, transient: true }))
      .then(({ config: cfg, transient }) => {
        configInFlightRef.current = null;
        if (cfg) {
          configRef.current = cfg;
          configTransientRef.current = false;
          if (mountedRef.current) {
            setConfig(cfg);
            setConfigState("ready");
          }
        } else {
          configTransientRef.current = transient;
          if (mountedRef.current) setConfigState("failed");
        }
        return cfg;
      });
    configInFlightRef.current = p;
    return p;
  }, []);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    const run = async () => {
      const cfg = await fetchConfig();
      if (!live || cfg || !configTransientRef.current) return;
      attempt += 1;
      timer = setTimeout(() => void run(), lookupRetryDelay(attempt));
    };
    void run();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [fetchConfig]);

  /** Use the server flow for this click? (config enabled; a missing config after a fresh ask = legacy). */
  const serverFlowReady = useCallback(async (): Promise<boolean> => {
    if (disabledRef.current) return false;
    let cfg = configRef.current;
    if (!cfg && (configInFlightRef.current || configTransientRef.current)) {
      cfg = await withinMs(fetchConfig(), CONFIG_WAIT_MS);
    }
    return Boolean(cfg?.enabled) && !disabledRef.current;
  }, [fetchConfig]);

  /** The server answered 503 {code: disabled}: legacy flow for the rest of this page. */
  const markDisabled = useCallback(() => {
    disabledRef.current = true;
  }, []);

  // ── once-marks ──────────────────────────────────────────────────────────
  // Per tab (sessionStorage, survives a reload): toasts, modals — every tab
  // watching a launch shows them, once. Per browser (localStorage): telemetry
  // — sent once per session, however many tabs watch it.
  const onceRef = useRef<Map<string, Set<string>>>(new Map());
  const marks = useCallback((sessionId: string): Set<string> => {
    let set = onceRef.current.get(sessionId);
    if (!set) {
      set = new Set<string>();
      try {
        const raw = readStore(ONCE_PREFIX + sessionId);
        const parsed = raw ? JSON.parse(raw) : [];
        if (Array.isArray(parsed)) parsed.forEach((k) => typeof k === "string" && set!.add(k));
      } catch {
        // ignore
      }
      onceRef.current.set(sessionId, set);
    }
    return set;
  }, []);
  const addTabMarks = useCallback((sessionId: string, keys: readonly string[]) => {
    const set = marks(sessionId);
    for (const k of keys) set.add(k);
    writeStore(ONCE_PREFIX + sessionId, JSON.stringify([...set]));
  }, [marks]);
  /** True the first time `key` is seen for this session (in this tab, across reloads). */
  const once = useCallback((sessionId: string, key: string): boolean => {
    const set = marks(sessionId);
    if (set.has(key)) return false;
    set.add(key);
    writeStore(ONCE_PREFIX + sessionId, JSON.stringify([...set]));
    return true;
  }, [marks]);
  /** Has this tab already handled `key` for the session? */
  const seen = useCallback((sessionId: string, key: string): boolean => marks(sessionId).has(key), [marks]);

  const browserMemRef = useRef<BrowserMarks>({});
  const updateBrowserMarks = useCallback((fn: (m: BrowserMarks) => BrowserMarks): BrowserMarks => {
    const now = Date.now();
    const next = pruneBrowserMarks(fn(mergeBrowserMarks(readBrowserStore(), browserMemRef.current)), now);
    browserMemRef.current = next;
    writeBrowserStore(next);
    return next;
  }, []);
  /** True the first time `key` is seen for this session in this BROWSER (all tabs); writes only then. */
  const onceBrowser = useCallback((sessionId: string, key: string): boolean => {
    const now = Date.now();
    const res = addBrowserMark(mergeBrowserMarks(readBrowserStore(), browserMemRef.current), sessionId, key, now);
    if (!res.added) return false;
    const next = pruneBrowserMarks(res.marks, now);
    browserMemRef.current = next;
    writeBrowserStore(next);
    return true;
  }, []);

  /** A tab that attaches mid-way: what already happened is not news (toasts / modals / telemetry). */
  const seedAttach = useCallback((sessionId: string, view: LaunchSessionView) => {
    const keys = seedMarksForAttach(view);
    addTabMarks(sessionId, keys);
    updateBrowserMarks((m) => seedBrowserMarks(m, sessionId, keys, Date.now()));
  }, [addTabMarks, updateBrowserMarks]);

  // ── the followed session ────────────────────────────────────────────────
  const [followId, setFollowId] = useState<string | null>(null);
  const followIdRef = useRef<string | null>(null);
  const [session, setSessionState] = useState<LaunchSessionView | null>(null);
  // The latest published view, for async handlers whose render is stale.
  const sessionRef = useRef<LaunchSessionView | null>(null);
  const setSession = useCallback((v: LaunchSessionView | null) => {
    sessionRef.current = v;
    setSessionState(v);
  }, []);
  const current = useCallback(() => sessionRef.current, []);
  const [origin, setOrigin] = useState<LaunchSessionOrigin | null>(null);
  const [rowsVersion, setRowsVersion] = useState(0);
  const rowsRef = useRef<RowCache>(emptyRowCache());
  const beforeRef = useRef<Map<string, Record<string, KeysetCursor>>>(new Map());
  const [hint, setHint] = useState<LaunchSessionHint | null>(null);
  const hintRef = useRef<LaunchSessionHint | null>(null);
  const seedPendingRef = useRef<string | null>(null);
  const wakeRef = useRef<(() => void) | null>(null);
  // Bumped when an action (cancel / confirm) publishes the view it got back:
  // a poll that started before it must not roll the screen back.
  const actionGenRef = useRef(0);
  // The followed session answered 404 / 403: the page resets its UI.
  const [lost, setLost] = useState<{ id: string; status: number } | null>(null);
  // Someone else's running launch (a banner offers to view it).
  const [other, setOther] = useState<LaunchSessionView | null>(null);
  // A session whose upload this tab's reload cut off (cancelled here).
  const [interrupted, setInterrupted] = useState<string | null>(null);
  // Sessions this mount is uploading into (never "interrupted").
  const uploadingRef = useRef<Set<string>>(new Set());

  const writeHint = useCallback((h: LaunchSessionHint | null) => {
    hintRef.current = h;
    setHint(h);
    const job = jobRefRef.current;
    if (!job) return;
    if (h) writeStore(sessionHintKey(job), encodeSessionHint(h));
    else removeStore(sessionHintKey(job));
  }, []);

  /**
   * Watch a session: a view in hand (created / child / reattach / observe) or
   * just its id (an attach after a 409). A tab that attaches mid-way marks the
   * milestones already passed as handled BEFORE publishing the view, so it
   * never replays their toasts, modals or telemetry.
   */
  const follow = useCallback(
    (target: LaunchSessionView | string, how: LaunchSessionOrigin, extra?: { attempts?: LaunchSessionHint["attempts"] }) => {
      const id = typeof target === "string" ? target : target.id;
      if (!id) return;
      const view = typeof target === "string" ? null : target;
      const attaching = how === "reattach" || how === "attach" || how === "observe";
      rowsRef.current = emptyRowCache();
      followIdRef.current = id;
      if (attaching && view) {
        seedAttach(id, view);
        seedPendingRef.current = null;
      } else {
        seedPendingRef.current = attaching ? id : null;
        // This browser now tracks the session: a later tab sends what this one could not.
        if (!attaching) updateBrowserMarks((m) => seedBrowserMarks(m, id, [], Date.now()));
      }
      setLost(null);
      setOther((prev) => (prev && prev.id === id ? null : prev));
      setRowsVersion((v) => v + 1);
      setOrigin(how);
      // A launch that is already over is published by the first poll, with
      // its rows (its outcome — toasts, failed CSV, redirect — needs them).
      setSession(view && !(attaching && isLaunchComplete(view)) ? view : null);
      setFollowId(id);
      const prev = hintRef.current?.id === id ? hintRef.current : null;
      const own = how === "created" || how === "child" || Boolean(prev?.own);
      writeHint({ id, attempts: extra?.attempts ?? prev?.attempts ?? [], ...(own ? { own: true } : {}) });
    },
    [seedAttach, setSession, updateBrowserMarks, writeHint],
  );

  /** Stop watching (and forget the hint). */
  const stop = useCallback(() => {
    followIdRef.current = null;
    rowsRef.current = emptyRowCache();
    setFollowId(null);
    setSession(null);
    setOrigin(null);
    setRowsVersion((v) => v + 1);
    writeHint(null);
  }, [setSession, writeHint]);

  const clearHint = useCallback(() => {
    const job = jobRefRef.current;
    if (job) removeStore(sessionHintKey(job));
  }, []);

  // Poll loop for the followed session.
  useEffect(() => {
    if (!followId || typeof document === "undefined") return;
    const id = followId;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wake: (() => void) | null = null;
    let errors = 0;

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        wake = resolve;
        timer = setTimeout(resolve, ms);
      });
    const wakeNow = () => {
      if (timer) clearTimeout(timer);
      const w = wake;
      wake = null;
      w?.();
    };
    wakeRef.current = wakeNow;
    const onVisibility = () => {
      if (document.visibilityState === "visible") wakeNow();
    };
    document.addEventListener("visibilitychange", onVisibility);

    // Pull the rows changed since the cursor. Pages already merged are kept
    // when a later page fails (the cursor resumes there next time).
    const syncRows = async (): Promise<{ changed: boolean; ok: boolean }> => {
      const before = rowsRef.current.version;
      for (let page = 0; page < ROW_PAGES_PER_TICK; page++) {
        const cache = rowsRef.current;
        const res = await launchSessionsApi.candidates(id, {
          view: "table",
          limit: ROW_PAGE_LIMIT,
          afterRev: cache.cursor.after_rev,
          afterId: cache.cursor.after_id,
        });
        if (!live || followIdRef.current !== id) return { changed: false, ok: false };
        if (!res.ok) return { changed: rowsRef.current.version !== before, ok: false };
        rowsRef.current = mergeKeysetPage(cache, res.data);
        const positions = beforeRef.current;
        positions.set(id, { ...(positions.get(id) ?? {}), ...keysetBeforeCursors(cache.cursor, res.data) });
        while (positions.size > BEFORE_CURSOR_SESSIONS) positions.delete(positions.keys().next().value as string);
        if (!res.data?.has_more) break;
      }
      return { changed: rowsRef.current.version !== before, ok: true };
    };

    void (async () => {
      // Slower polling while the session waits on the recruiter or only
      // JobDiva provisioning is left (lib/launch-session isSettledForPolling).
      let settled = false;
      while (live && followIdRef.current === id) {
        let terminal = false;
        let again = false;
        try {
          const gen = actionGenRef.current;
          const res = await launchSessionsApi.get(id);
          if (!live || followIdRef.current !== id) break;
          if (!res.ok) {
            if (res.status === 404 || res.status === 403) {
              // Gone, or no longer ours to see: stop watching it (the page resets).
              followIdRef.current = null;
              setFollowId(null);
              setSession(null);
              setLost({ id, status: res.status });
              writeHint(null);
              break;
            }
            throw new Error(`session ${res.status}`);
          }
          const view = res.data?.session;
          if (!view || typeof view.id !== "string") throw new Error("session missing");
          const rows = rowsAreStale(rowsRef.current, view) ? await syncRows() : { changed: false, ok: true };
          if (!live || followIdRef.current !== id) break;
          // The outcome is computed from the rows: never publish a finished
          // launch with rows that could not be brought up to date.
          if (!rows.ok && isLaunchComplete(view)) {
            if (rows.changed) setRowsVersion((v) => v + 1);
            throw new Error("rows behind a finished launch");
          }
          if (actionGenRef.current !== gen) {
            // An action published a newer view while this GET was out: ask again.
            if (rows.changed) setRowsVersion((v) => v + 1);
            again = true;
          } else {
            if (seedPendingRef.current === id) {
              // Attached by id (a 409): what already happened is not news.
              seedPendingRef.current = null;
              seedAttach(id, view);
            }
            setSession(view);
            if (rows.changed) setRowsVersion((v) => v + 1);
            errors = rows.ok ? 0 : errors + 1;
            terminal = isTerminal(view);
            settled = isSettledForPolling(view);
          }
        } catch {
          errors += 1;
        }
        if (terminal) break;
        if (again) continue;
        const cfg = configRef.current;
        await sleep(
          nextPollDelay({
            baseMs: cfg?.poll_ms ?? 3000,
            hiddenMs: cfg?.hidden_poll_ms ?? 15000,
            hidden: document.visibilityState === "hidden",
            errors,
            settled,
          }),
        );
      }
    })();

    return () => {
      live = false;
      if (timer) clearTimeout(timer);
      const w = wake;
      wake = null;
      w?.();
      if (wakeRef.current === wakeNow) wakeRef.current = null;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [followId, seedAttach, setSession, writeHint]);

  /** Poll right away (after an action changed the session). */
  const pollNow = useCallback(() => wakeRef.current?.(), []);

  // ── a teammate's running launch (banner) ────────────────────────────────
  const otherId = other?.id ?? null;
  useEffect(() => {
    if (!otherId) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (!live) return;
      // Nobody sees the banner in a hidden tab: skip the request, look again later.
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        timer = setTimeout(() => void tick(), OTHER_POLL_MS);
        return;
      }
      const res = await launchSessionsApi.get(otherId);
      if (!live) return;
      if (res.ok && res.data?.session) {
        const v = res.data.session;
        if (isLaunchComplete(v) || followIdRef.current === v.id) {
          setOther(null);
          return;
        }
        setOther((prev) => (prev && prev.id === v.id ? v : prev));
      } else if (!res.ok && (res.status === 404 || res.status === 403)) {
        setOther(null);
        return;
      }
      timer = setTimeout(() => void tick(), OTHER_POLL_MS);
    };
    timer = setTimeout(() => void tick(), OTHER_POLL_MS);
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [otherId]);

  // ── reattach on page load ───────────────────────────────────────────────
  // The user's own active session (the one this tab watched first), else a
  // banner for a teammate's, else the newest finished session of theirs that
  // still has rows parked for contact details. A failed lookup is retried
  // with back-off for as long as the page is open. Runs whatever
  // config.enabled says (shouldLookForSessions): switching server launch off
  // stops NEW server launches only — existing sessions still finish on the
  // server and stay visible / answerable here.
  const [parked, setParked] = useState<LaunchSessionView | null>(null);
  const reattachDoneRef = useRef<string | null>(null);
  const reattachBusyRef = useRef(false);
  const reattachTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reattachFailuresRef = useRef(0);
  const [reattachTry, setReattachTry] = useState(0);
  useEffect(() => () => {
    if (reattachTimerRef.current) clearTimeout(reattachTimerRef.current);
  }, []);
  useEffect(() => {
    if (!shouldLookForSessions({ reattach, jobRef: ref, configState })) return;
    if (reattachDoneRef.current === ref || reattachBusyRef.current) return;
    reattachBusyRef.current = true;
    let retryInMs: number | null = null;
    void (async () => {
      try {
        const stored = decodeSessionHint(readStore(sessionHintKey(ref)));
        if (stored && !hintRef.current) {
          hintRef.current = stored;
          setHint(stored);
        }
        const res = await launchSessionsApi.listForJob(ref, { active: true, limit: 10 });
        if (!mountedRef.current) return;
        if (jobRefRef.current !== ref) {
          // The job ref changed while this was out (a numeric-id URL whose
          // JobDiva ref arrived with the draft): look again for the new one.
          retryInMs = 0;
          return;
        }
        if (!res.ok) {
          if (res.status !== 403 && res.status !== 404) {
            reattachFailuresRef.current += 1;
            retryInMs = lookupRetryDelay(reattachFailuresRef.current + 2);
          }
          return;
        }
        reattachFailuresRef.current = 0;
        reattachDoneRef.current = ref;
        const me = currentUserEmail();
        const plan = planReattach(res.data?.sessions, {
          hintId: stored?.id,
          me,
          outcomeHandled: (id) => marks(id).has("outcome"),
          uploadingIds: uploadingRef.current,
        });
        if (plan.interrupted) {
          // This tab's upload, cut off by the reload: it can never finish.
          const id = plan.interrupted.id;
          setInterrupted(id);
          void writeWithRetry(() => launchSessionsApi.cancel(id, "upload interrupted by a page reload"), alive);
        }
        if (stored && stored.id !== plan.follow?.id && decodeSessionHint(readStore(sessionHintKey(ref)))?.id === stored.id) {
          removeStore(sessionHintKey(ref));
        }
        if (plan.follow && !followIdRef.current) follow(plan.follow, "reattach");
        if (plan.other && !followIdRef.current) setOther(plan.other);
        if (plan.parked) {
          setParked(plan.parked);
          return;
        }
        if (plan.follow) return;
        const parkedRes = await launchSessionsApi.listForJob(ref, { needsContact: true, limit: 5 });
        if (!mountedRef.current || jobRefRef.current !== ref || !parkedRes.ok) return;
        const offer = pickParkedOffer(parkedRes.data?.sessions, me, Date.now());
        if (offer && !followIdRef.current) setParked(offer);
      } finally {
        reattachBusyRef.current = false;
        if (retryInMs !== null && mountedRef.current) {
          if (reattachTimerRef.current) clearTimeout(reattachTimerRef.current);
          reattachTimerRef.current = setTimeout(() => {
            reattachTimerRef.current = null;
            setReattachTry((t) => t + 1);
          }, retryInMs);
        }
      }
    })();
  }, [reattach, ref, configState, follow, reattachTry, marks, alive]);

  const clearParked = useCallback(() => setParked(null), []);
  const clearLost = useCallback(() => setLost(null), []);
  const clearInterrupted = useCallback(() => setInterrupted(null), []);
  const dismissOther = useCallback(() => setOther(null), []);

  // ── actions ─────────────────────────────────────────────────────────────

  /** POST create, re-sent with the SAME body (same idempotency key) on 429 / 5xx / network. */
  const create = useCallback(async (job: string, body: CreateSessionBody): Promise<CreateOutcome> => {
    let outcome: CreateOutcome = { kind: "transient", status: 0, retryAfterMs: null };
    for (let attempt = 1; attempt <= CREATE_ATTEMPTS; attempt++) {
      outcome = classifyCreateResponse(await launchSessionsApi.create(job, body));
      if (outcome.kind !== "transient" || !mountedRef.current) return outcome;
      if (attempt < CREATE_ATTEMPTS) await delay(writeRetryDelay(attempt, outcome.retryAfterMs));
      if (!mountedRef.current) return outcome;
    }
    return {
      kind: "error",
      status: outcome.kind === "transient" ? outcome.status : 0,
      message: "the server is busy or unreachable — please try again",
    };
  }, []);

  /**
   * Provided mode with an upload: the first chunk rides on the create, the
   * rest go to …/candidates (429 honoured), then …/start. The session id is
   * the tab's hint from the create on, so a reload mid-upload finds it and
   * cancels it (it can never finish). A failed upload cancels it here.
   */
  const createProvided = useCallback(
    async (job: string, base: CreateSessionBody, rows: readonly unknown[]): Promise<CreateOutcome> => {
      const size = Math.max(1, Math.min(UPLOAD_CHUNK, configRef.current?.max_upload_chunk ?? UPLOAD_CHUNK));
      const plan = planProvidedUpload(rows, size);
      const outcome = await create(job, { ...base, candidates: plan.first, start: plan.startOnCreate });
      if (outcome.kind !== "created" && outcome.kind !== "replayed") return outcome;
      const id = outcome.session.id;
      if (outcome.session.status !== "collecting") return outcome;
      uploadingRef.current.add(id);
      if (jobRefRef.current === job) writeStore(sessionHintKey(job), encodeSessionHint({ id, attempts: [], own: true }));
      const abandon = (reason: string) =>
        void writeWithRetry(() => launchSessionsApi.cancel(id, reason), alive);
      try {
        for (const part of plan.rest) {
          const res = await writeWithRetry(() => launchSessionsApi.addCandidates(id, part), alive);
          if (!res.ok) {
            abandon("upload failed");
            return { kind: "error", status: res.status, message: "uploading the candidates failed — please try again" };
          }
          if (!mountedRef.current) return { kind: "error", status: 0, message: "the page was closed" };
        }
        const started = await writeWithRetry(() => launchSessionsApi.start(id), alive);
        if (started.ok && started.data?.session) return { kind: outcome.kind, session: started.data.session };
        if (!started.ok && started.status === 409) {
          // Someone (a replayed request) started it already: watch it.
          return outcome;
        }
        abandon("start failed");
        return { kind: "error", status: started.status, message: "starting the launch failed — please try again" };
      } finally {
        uploadingRef.current.delete(id);
      }
    },
    [create, alive],
  );

  /** POST cancel; ok or 409 (already over) means nothing more will be sent. */
  const cancel = useCallback(async (id: string, reason: string) => {
    const res = await writeWithRetry(() => launchSessionsApi.cancel(id, reason), alive);
    if (res.ok && res.data?.session && followIdRef.current === id) {
      actionGenRef.current += 1;
      setSession(res.data.session);
    }
    pollNow();
    return res;
  }, [pollNow, alive, setSession]);

  const confirm = useCallback(
    async (id: string, body: { contacts: Record<string, { phone?: string; email?: string }>; candidate_ids: string[] }) => {
      const res = await writeWithRetry(() => launchSessionsApi.confirm(id, body), alive);
      if (res.ok && res.data?.session && followIdRef.current === id) {
        actionGenRef.current += 1;
        setSession(res.data.session);
      }
      pollNow();
      return res;
    },
    [pollNow, alive, setSession],
  );

  const needsContact = useCallback(
    (id: string, body: { contacts: Record<string, { phone?: string; email?: string }>; candidate_ids: string[]; skip_ids: string[]; idempotency_key?: string }) =>
      writeWithRetry(() => launchSessionsApi.needsContact(id, body), alive),
    [alive],
  );

  const retryFailed = useCallback(
    (id: string, body: { candidate_ids?: string[]; idempotency_key?: string }) =>
      writeWithRetry(() => launchSessionsApi.retryFailed(id, body), alive),
    [alive],
  );

  /** Every row of a session in the given states / view (offset pages); null on error. */
  const loadRows = useCallback(
    async (id: string, q: { states?: string[]; view?: "table" | "full" | "failed" } = {}): Promise<LaunchSessionRow[] | null> => {
      const out: LaunchSessionRow[] = [];
      for (let page = 0; page < LOAD_MAX_PAGES; page++) {
        const res = await writeWithRetry(
          () => launchSessionsApi.candidates(id, { states: q.states, view: q.view ?? "table", limit: LOAD_PAGE_LIMIT, offset: out.length }),
          alive,
        );
        if (!res.ok) return null;
        const rows = res.data?.rows ?? [];
        out.push(...rows);
        if (!res.data?.has_more || rows.length === 0) break;
      }
      return out;
    },
    [alive],
  );

  /** The failed-candidate CSV rows (GET …/candidates?view=failed), minus `exclude`. */
  const loadFailed = useCallback(
    async (id: string, exclude?: ReadonlySet<string>): Promise<FailedCandidateCsvRow[] | null> => {
      const rows = await loadRows(id, { view: "failed" });
      return rows ? failedRowsToCsv(rows, exclude) : null;
    },
    [loadRows],
  );

  /**
   * One row's FULL record (with résumé text — the table holds light rows):
   * by id first (works for a table restored after a reload), else the keyset
   * position just before it and a short forward scan (lib findFullRow).
   * null when it can't be found.
   */
  const fullRow = useCallback(async (sessionId: string, candidateId: string): Promise<Record<string, unknown> | null> => {
    const cachedRev = followIdRef.current === sessionId ? Number(rowsRef.current.rows[candidateId]?.rev) : NaN;
    return findFullRow({
      candidateId,
      read: async (q) => {
        const res = await launchSessionsApi.candidates(sessionId, q);
        return res.ok ? res.data : null;
      },
      known: beforeRef.current.get(sessionId)?.[candidateId] ?? null,
      cachedRev,
      scanLimit: FULL_ROW_SCAN_LIMIT,
      scanPages: FULL_ROW_SCAN_PAGES,
    });
  }, []);

  const rows = useCallback(() => rowsRef.current, []);
  /** The signed-in user's email (lower-cased), as the server compares requested_by. */
  const me = useCallback(() => currentUserEmail(), []);

  return useMemo(
    () => ({
      configState,
      config,
      serverFlowReady,
      markDisabled,
      session,
      current,
      origin,
      rowsVersion,
      rows,
      hint,
      parked,
      clearParked,
      lost,
      clearLost,
      other,
      dismissOther,
      interrupted,
      clearInterrupted,
      follow,
      stop,
      clearHint,
      once,
      onceBrowser,
      seen,
      me,
      create,
      createProvided,
      cancel,
      confirm,
      needsContact,
      retryFailed,
      loadRows,
      loadFailed,
      fullRow,
      pollNow,
    }),
    [
      configState, config, serverFlowReady, markDisabled, session, current, origin, rowsVersion, rows, hint, parked,
      clearParked, lost, clearLost, other, dismissOther, interrupted, clearInterrupted, follow, stop, clearHint,
      once, onceBrowser, seen, me, create, createProvided, cancel, confirm, needsContact, retryFailed, loadRows,
      loadFailed, fullRow, pollNow,
    ],
  );
}

export type LaunchSessionController = ReturnType<typeof useLaunchSession>;
