"use client";

/**
 * Global candidate pool — the person-level view of outreach.
 *
 * Organized by recency of contact rather than by job, because the question this
 * page answers is "who did we last reach out to, for what, and how did it end?".
 * That is what lets a recruiter avoid calling someone a second job already
 * called last week.
 *
 * Sections, top to bottom:
 *   In progress      sent, awaiting their response, inside the 10-day window
 *   Completed        screened, result known — this is the reuse pool
 *   Never responded  sent, no activity at all, past the window
 *   Suppressed       DNC / cooldown / attempt cap
 *
 * "Not contacted" is intentionally NOT a browsable section: it is the large
 * majority of sourced rows and scrolling it tells a recruiter nothing. It is
 * reachable only by picking that section explicitly, which is search-first.
 *
 * Route note: this page must stay at exactly `/candidates`. nginx forwards
 * `^/candidates/.+` to the API, so any client sub-route here would 404 as HTML.
 * Section selection therefore lives in component state, not the path.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  Ban,
  CalendarClock,
  CheckCircle2,
  Clock,
  Loader2,
  Mail,
  MailX,
  Phone,
  Search,
  UserRound,
  XCircle,
} from "lucide-react";
import { api } from "@/lib/api";

const errorText = (e: unknown, fallback: string): string =>
  e instanceof Error && e.message ? e.message : fallback;

type Bucket =
  | "in_progress"
  | "completed"
  | "no_response"
  | "suppressed"
  | "never_contacted";

type PoolCandidate = {
  person_id: number;
  bucket: Bucket;
  display_name: string | null;
  headline: string | null;
  location: string | null;
  has_contact: boolean;
  last_outreach_at: string | null;
  last_job_id: string | null;
  last_job_title: string | null;
  last_job_in_scope: boolean;
  last_tier: string | null;
  last_completed_at: string | null;
  last_outcome: string | null;
  last_score: number | null;
  attempts_90d: number;
  attempts_total: number;
  next_eligible_at: string | null;
  suppressed_reason: string | null;
  facts_fresh_until: string | null;
};

type Cursor = { cursor_sort_at: string; cursor_person_id: number } | null;

const SECTIONS: {
  bucket: Bucket;
  label: string;
  blurb: string;
  icon: typeof Clock;
  accent: string;
}[] = [
  {
    bucket: "in_progress",
    label: "In progress",
    blurb: "Outreach sent, waiting on them. Don't contact these for another job yet.",
    icon: Clock,
    accent: "#f59e0b",
  },
  {
    bucket: "completed",
    label: "Completed",
    blurb:
      "Screened, result known. These are the candidates a new job can reuse instead of re-screening.",
    icon: CheckCircle2,
    accent: "#059669",
  },
  {
    bucket: "no_response",
    label: "Never responded",
    blurb:
      "Reached out, no activity at all. They sat pending until they aged out — worth a different channel, not another bot call.",
    icon: MailX,
    accent: "#64748b",
  },
  {
    bucket: "suppressed",
    label: "Suppressed",
    blurb: "Must not be contacted right now — Do Not Contact, cooldown, or attempt cap.",
    icon: Ban,
    accent: "#e11d48",
  },
  {
    bucket: "never_contacted",
    label: "Not contacted",
    blurb:
      "Sourced but never reached out to. Large — search rather than browse.",
    icon: UserRound,
    accent: "#94a3b8",
  },
];

const PAGE_SIZE = 50;

/**
 * `nowMs` is threaded in rather than read from `Date.now()` here: reading the
 * clock during render makes the output depend on when React happens to
 * re-render, so two rows rendered a tick apart could disagree. The page captures
 * one timestamp per data load and every relative label is computed against it.
 */
function relativeDays(iso: string | null, nowMs: number): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const days = Math.floor((nowMs - then) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

function absoluteDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

function OutcomeChip({ outcome, score }: { outcome: string | null; score: number | null }) {
  if (!outcome) return <span className="text-slate-400 text-[12px]">—</span>;
  const pass = outcome === "pass";
  const Icon = pass ? CheckCircle2 : XCircle;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ring-inset ${
        pass
          ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
          : "bg-rose-50 text-rose-700 ring-rose-200"
      }`}
    >
      <Icon className="h-3 w-3" />
      {pass ? "Pass" : "Fail"}
      {score != null ? ` · ${Math.round(score)}` : ""}
    </span>
  );
}

function FreshnessChip({ until, nowMs }: { until: string | null; nowMs: number }) {
  if (!until) {
    return (
      <span
        className="text-[11px] text-slate-400"
        title="No reusable answers on file — a new job would run a full screen."
      >
        no reusable answers
      </span>
    );
  }
  const fresh = new Date(until).getTime() > nowMs;
  return (
    <span
      className={`text-[11px] font-medium ${fresh ? "text-emerald-600" : "text-amber-600"}`}
      title={
        fresh
          ? `Their work authorization, arrangements and pay answers stay reusable until ${absoluteDate(until)}.`
          : `Reusable answers expired ${relativeDays(until, nowMs)} — a new job would re-ask them.`
      }
    >
      {fresh ? `reusable until ${absoluteDate(until)}` : "answers expired"}
    </span>
  );
}

export default function CandidatePoolPage() {
  const [bucket, setBucket] = useState<Bucket>("in_progress");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [rows, setRows] = useState<PoolCandidate[]>([]);
  const [cursor, setCursor] = useState<Cursor>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  // One clock reading per data load. Every relative label ("3d ago",
  // "expired") is computed against this, so rows can never disagree because
  // React re-rendered them a tick apart.
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  // Guards against a slow first page landing after the user has already switched
  // sections and overwriting the newer results.
  const requestSeq = useRef(0);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  useEffect(() => {
    let cancelled = false;
    api.candidatePool
      .summary()
      .then((res) => {
        if (!cancelled) setCounts(res?.counts || {});
      })
      .catch(() => {
        // Counts are decoration; a failure here must not blank the list.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadFirstPage = useCallback(async () => {
    const seq = ++requestSeq.current;
    setIsLoading(true);
    setError(null);
    try {
      const res = await api.candidatePool.list({
        bucket,
        search: debouncedSearch || undefined,
        limit: PAGE_SIZE,
      });
      if (seq !== requestSeq.current) return;
      setNowMs(Date.now());
      setRows(res?.candidates || []);
      setCursor(res?.next_cursor || null);
    } catch (e: unknown) {
      if (seq !== requestSeq.current) return;
      setRows([]);
      setCursor(null);
      setError(errorText(e, "Could not load the candidate pool."));
    } finally {
      if (seq === requestSeq.current) setIsLoading(false);
    }
  }, [bucket, debouncedSearch]);

  useEffect(() => {
    void loadFirstPage();
  }, [loadFirstPage]);

  const loadMore = useCallback(async () => {
    if (!cursor || isLoadingMore) return;
    const seq = requestSeq.current;
    setIsLoadingMore(true);
    try {
      const res = await api.candidatePool.list({
        bucket,
        search: debouncedSearch || undefined,
        limit: PAGE_SIZE,
        ...cursor,
      });
      if (seq !== requestSeq.current) return;
      setRows((prev) => [...prev, ...(res?.candidates || [])]);
      setCursor(res?.next_cursor || null);
    } catch (e: unknown) {
      if (seq === requestSeq.current) setError(errorText(e, "Could not load more."));
    } finally {
      if (seq === requestSeq.current) setIsLoadingMore(false);
    }
  }, [bucket, cursor, debouncedSearch, isLoadingMore]);

  const activeSection = useMemo(
    () => SECTIONS.find((s) => s.bucket === bucket) ?? SECTIONS[0],
    [bucket]
  );

  return (
    <div className="space-y-6 max-w-[1280px] mx-auto pb-16">
      <div className="flex items-center gap-4 mt-2">
        <Link
          href="/"
          className="rounded-full h-10 w-10 inline-flex items-center justify-center hover:bg-slate-100 transition-colors"
          aria-label="Back to jobs"
        >
          <ArrowLeft className="h-5 w-5 text-slate-400" />
        </Link>
        <div>
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Candidates</h1>
          <p className="text-slate-500 text-[14px]">
            Everyone we&apos;ve reached out to, newest first — so a new job can reuse a
            recent screen instead of calling them again.
          </p>
        </div>
      </div>

      {/* Section picker. Kept in state, not the URL path: nginx sends every
          /candidates/<sub> request to the API. */}
      <div className="flex flex-wrap gap-2">
        {SECTIONS.map((section) => {
          const Icon = section.icon;
          const isActive = section.bucket === bucket;
          const count = counts[section.bucket];
          return (
            <button
              key={section.bucket}
              type="button"
              onClick={() => setBucket(section.bucket)}
              className={`inline-flex items-center gap-2 rounded-lg border px-3.5 h-10 text-[13px] font-semibold transition-colors ${
                isActive
                  ? "border-primary bg-primary text-white shadow-sm"
                  : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-slate-900"
              }`}
            >
              <Icon
                className="h-4 w-4"
                style={{ color: isActive ? "#fff" : section.accent }}
              />
              {section.label}
              {count != null && (
                <span
                  className={`rounded-full px-1.5 py-0.5 text-[11px] font-bold ${
                    isActive ? "bg-white/20 text-white" : "bg-slate-100 text-slate-500"
                  }`}
                >
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <div className="bg-white rounded-2xl border border-slate-200 shadow-[0_2px_10px_-4px_rgba(0,0,0,0.1)]">
        <div className="flex flex-col gap-3 border-b border-slate-100 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-2.5">
            <activeSection.icon
              className="mt-0.5 h-4 w-4 flex-shrink-0"
              style={{ color: activeSection.accent }}
            />
            <p className="text-[13px] text-slate-500 leading-relaxed max-w-[640px]">
              {activeSection.blurb}
            </p>
          </div>
          <div className="relative sm:w-[280px] sm:flex-shrink-0">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search name, title, location"
              className="h-10 w-full rounded-lg border border-slate-200 pl-9 pr-3 text-[13px] text-slate-900 placeholder:text-slate-400 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
        </div>

        {error && (
          <div className="m-5 rounded-lg bg-rose-50 px-4 py-3 text-[13px] text-rose-700 ring-1 ring-inset ring-rose-200">
            {error}
          </div>
        )}

        {isLoading ? (
          <div className="flex items-center justify-center gap-2 py-16 text-[13px] text-slate-400">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading candidates…
          </div>
        ) : rows.length === 0 ? (
          <div className="py-16 text-center">
            <p className="text-[14px] font-semibold text-slate-700">
              {debouncedSearch ? "No matches" : `Nothing in ${activeSection.label.toLowerCase()}`}
            </p>
            <p className="mt-1 text-[13px] text-slate-400">
              {debouncedSearch
                ? "Try a different name, title, or location."
                : "Candidates appear here once they've been launched from a job."}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-left">
              <thead>
                <tr className="border-b border-slate-100 text-[11px] font-bold uppercase tracking-wide text-slate-400">
                  <th className="px-5 py-3">Candidate</th>
                  <th className="px-3 py-3">Last reached out</th>
                  <th className="px-3 py-3">For</th>
                  <th className="px-3 py-3">Screen result</th>
                  <th className="px-3 py-3">Attempts</th>
                  <th className="px-3 py-3">
                    {bucket === "suppressed" ? "Blocked because" : "Reusable answers"}
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.person_id}
                    className="border-b border-slate-50 last:border-0 hover:bg-slate-50/60"
                  >
                    <td className="px-5 py-3.5">
                      <div className="flex items-start gap-3">
                        <div className="mt-0.5 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-slate-100 text-[11px] font-bold text-slate-500">
                          {(row.display_name || "?").trim().charAt(0).toUpperCase()}
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="truncate text-[13.5px] font-semibold text-slate-900">
                              {row.display_name || "Unnamed candidate"}
                            </span>
                            {/* Wrapped in a span: lucide icons don't accept a
                                `title` prop, and the tooltip is the only thing
                                explaining what the icon means. */}
                            <span
                              title={
                                row.has_contact
                                  ? "Has a phone or email on file"
                                  : "No usable contact on file — can't be launched"
                              }
                              className="flex-shrink-0"
                            >
                              {row.has_contact ? (
                                <Phone className="h-3 w-3 text-emerald-500" />
                              ) : (
                                <Mail className="h-3 w-3 text-slate-300" />
                              )}
                            </span>
                          </div>
                          <p className="truncate text-[12px] text-slate-500">
                            {row.headline || "—"}
                            {row.location ? ` · ${row.location}` : ""}
                          </p>
                        </div>
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-3 py-3.5">
                      <span
                        className="text-[13px] text-slate-700"
                        title={absoluteDate(row.last_outreach_at)}
                      >
                        {relativeDays(row.last_outreach_at, nowMs)}
                      </span>
                      {row.last_tier === "recheck" && (
                        <span className="ml-1.5 rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-bold text-indigo-600">
                          RE-CHECK
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-3.5">
                      {row.last_job_in_scope && row.last_job_id ? (
                        <Link
                          href={`/jobs/${row.last_job_id}/rankings`}
                          className="text-[13px] font-medium text-primary hover:underline"
                        >
                          {row.last_job_title || row.last_job_id}
                        </Link>
                      ) : (
                        <span
                          className="text-[13px] italic text-slate-400"
                          title="This job isn't assigned to you, so its title is hidden. The contact history still counts."
                        >
                          {row.last_job_title || "—"}
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-3.5">
                      <OutcomeChip outcome={row.last_outcome} score={row.last_score} />
                      {row.last_completed_at && (
                        <div className="mt-0.5 text-[11px] text-slate-400">
                          {relativeDays(row.last_completed_at, nowMs)}
                        </div>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-3.5 text-[13px] text-slate-600">
                      {row.attempts_90d}
                      <span className="text-slate-400"> / 90d</span>
                      {row.attempts_total > row.attempts_90d && (
                        <span className="ml-1 text-[11px] text-slate-400">
                          ({row.attempts_total} total)
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-3.5">
                      {bucket === "suppressed" ? (
                        <div className="space-y-0.5">
                          <p className="text-[12px] text-rose-700">
                            {row.suppressed_reason || "Cooldown"}
                          </p>
                          {row.next_eligible_at && (
                            <p className="inline-flex items-center gap-1 text-[11px] text-slate-400">
                              <CalendarClock className="h-3 w-3" />
                              contactable {absoluteDate(row.next_eligible_at)}
                            </p>
                          )}
                        </div>
                      ) : (
                        <FreshnessChip until={row.facts_fresh_until} nowMs={nowMs} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {cursor && (
              <div className="flex justify-center border-t border-slate-100 p-4">
                <button
                  type="button"
                  onClick={() => void loadMore()}
                  disabled={isLoadingMore}
                  className="inline-flex h-9 items-center gap-2 rounded-lg border border-slate-200 px-4 text-[13px] font-semibold text-slate-600 transition-colors hover:border-slate-300 hover:text-slate-900 disabled:opacity-60"
                >
                  {isLoadingMore && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  Load more
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
