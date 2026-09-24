"use client";

/**
 * Cross Submissions panel (rank list).
 *
 * Candidates PAIR already phone-screened for OTHER jobs in the last 60 days
 * who also match this job. They are NOT on this job's candidate list — every
 * other row on the rank list is — so the panel is visually separate and each
 * row carries an action: "Add to job" copies the person into this job's pool
 * (Pending, goes through the normal Launch PAIR gate), "Screen report" opens
 * the prior job's report. Hidden entirely when the list is empty.
 *
 * Data: GET /jobs/{id}/cross-submissions (stored list, filled by the Step-5
 * search hook or by "Refresh" → POST …/run).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ChevronDown, ChevronUp, ExternalLink, Loader2, Mail, Phone, RefreshCw, Repeat, UserPlus, Check } from "lucide-react";
import { api, type CrossSubmission } from "@/lib/api";
import { getScoreBand, getScoreTone } from "@/lib/match-score";
import { cn } from "@/lib/utils";

// These rows are scored by the same matrix as Step 5
// (services/unified_candidate_search.apply_scoring_policy), so they are shown
// with the same bands — 85+ Excellent · 75–84 Strong · 60–74 Good — straight
// from lib/match-score.ts. A bare percentage here let the panel drift from the
// colours and labels the recruiter reads everywhere else.
function MatchScorePill({ score }: { score: number | null | undefined }) {
  const tone = getScoreTone(score);
  if (score == null || !tone) {
    return (
      <span
        className="inline-flex items-center justify-center px-2 py-1 rounded-md text-[11px] font-bold bg-slate-100 text-slate-500 border border-slate-200"
        title="Couldn't score this candidate against this job's criteria."
      >
        N/A
      </span>
    );
  }
  const band = getScoreBand(score);
  return (
    <span
      className="inline-flex items-center justify-center px-2 py-1 rounded-md text-[11px] font-bold border"
      style={{ backgroundColor: tone.bg, color: tone.text, borderColor: tone.ring }}
      title={`${band.label} — ${band.action}`}
    >
      {Math.round(Number(score))}%
    </span>
  );
}

type Notify = (type: "info" | "error" | "success", message: string) => void;

type Props = {
  jobId: string;
  /** Called after a candidate is added so the parent can refetch its table. */
  onAdded?: () => void | Promise<void>;
  notify?: Notify;
  className?: string;
};

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function screenScore(c: CrossSubmission): string {
  if (c.engage_score == null) return "";
  if (c.engage_total_score && c.engage_total_score > 0) {
    return `${Math.round((c.engage_score / c.engage_total_score) * 100)}%`;
  }
  return `${c.engage_score}`;
}

function ResultBadge({ result }: { result: string | null | undefined }) {
  const label = result || "Pending";
  const cls =
    label === "Pass"
      ? "bg-emerald-50 text-emerald-700 border-emerald-200"
      : label === "Fail"
        ? "bg-rose-50 text-rose-700 border-rose-200"
        : label === "In Progress"
          ? "bg-amber-50 text-amber-700 border-amber-200"
          : "bg-slate-50 text-slate-600 border-slate-200";
  return (
    <span className={cn("inline-flex items-center px-2 py-0.5 rounded-full border text-[10px] font-bold leading-4", cls)}>
      {label}
    </span>
  );
}

export function CrossSubmissionsPanel({ jobId, onAdded, notify, className }: Props) {
  const [rows, setRows] = useState<CrossSubmission[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(true);
  const [addingId, setAddingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    if (!jobId) return;
    try {
      const res = await api.jobs.getCrossSubmissions(jobId);
      setRows(Array.isArray(res?.candidates) ? res.candidates : []);
      setError(null);
    } catch (e: any) {
      // Non-fatal — the rank list must render without this panel.
      setError(e?.message || "Failed to load cross submissions");
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    setLoading(true);
    void load();
  }, [load]);

  const handleRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const summary = await api.jobs.runCrossSubmissions(jobId, { send_email: false });
      await load();
      if (summary?.criteria_missing) {
        notify?.("info", "No sourcing filters saved for this job yet — nothing to match against.");
      } else if (summary?.ran === false && summary?.skipped_reason) {
        notify?.("info", `Cross submissions scan skipped: ${summary.skipped_reason}`);
      } else {
        notify?.("success", `Cross submissions refreshed — ${summary?.selected ?? 0} matching, ${summary?.new ?? 0} new.`);
      }
    } catch (e: any) {
      notify?.("error", e?.message || "Cross submissions refresh failed");
    } finally {
      setRefreshing(false);
    }
  };

  const handleAdd = async (row: CrossSubmission) => {
    if (addingId != null) return;
    setAddingId(row.id);
    try {
      const res = await api.jobs.addCrossSubmission(jobId, row.id);
      setRows(prev => prev.map(r => (r.id === row.id ? { ...r, added_at: r.added_at || new Date().toISOString() } : r)));
      notify?.(
        "success",
        res?.already_present
          ? `${row.name || "Candidate"} is already on this job.`
          : `${row.name || "Candidate"} added to this job as Pending — launch PAIR from the list when ready.`,
      );
      await onAdded?.();
    } catch (e: any) {
      notify?.("error", e?.message || "Failed to add candidate");
    } finally {
      setAddingId(null);
    }
  };

  const pendingCount = useMemo(() => rows.filter(r => !r.added_at).length, [rows]);

  // Hide entirely when there is nothing to show (fresh / unlaunched jobs look unchanged).
  if (!loading && !error && rows.length === 0) return null;
  if (loading) return null;
  if (error) return null;

  return (
    <div className={cn("mb-4 bg-white border border-indigo-200 rounded-[8px] shadow-sm", className)} data-testid="cross-submissions-panel">
      <div className="w-full flex items-center justify-between px-4 py-3 gap-3">
        <button
          type="button"
          onClick={() => setOpen(v => !v)}
          className="flex-1 flex items-center gap-2 text-left min-w-0"
          aria-expanded={open}
        >
          <Repeat className="w-4 h-4 text-indigo-500 flex-shrink-0" />
          <span className="text-[13px] font-semibold text-slate-800">Cross Submissions</span>
          <span className="inline-flex items-center justify-center min-w-[22px] h-[20px] px-1.5 rounded-full bg-indigo-600 text-white text-[11px] font-bold">
            {pendingCount}
          </span>
          <span className="text-[11px] text-slate-500 truncate hidden sm:inline">
            Screened by PAIR for other jobs in the last 60 days · match this role · not on this list yet
          </span>
        </button>
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-slate-200 text-[12px] font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          title="Re-scan PAIR's screened pool against this job's saved sourcing filters"
        >
          {refreshing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          Refresh
        </button>
        <button type="button" onClick={() => setOpen(v => !v)} aria-label={open ? "Collapse" : "Expand"} className="p-1 rounded hover:bg-slate-50">
          {open ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
        </button>
      </div>

      {open && (
        <div className="border-t border-indigo-100 overflow-x-auto">
          <table className="w-full min-w-[900px] border-separate border-spacing-0 text-[12px]">
            <thead>
              <tr className="bg-indigo-50/60 text-[10px] uppercase tracking-wide text-slate-500">
                <th className="text-left font-semibold px-4 py-2">Candidate</th>
                <th className="text-left font-semibold px-3 py-2">Contact</th>
                <th className="text-left font-semibold px-3 py-2">Screened for</th>
                <th className="text-left font-semibold px-3 py-2">Screen result</th>
                <th className="text-right font-semibold px-3 py-2">Match</th>
                <th className="text-right font-semibold px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(row => {
                const added = !!row.added_at;
                const reportHref = row.prior_jobdiva_id
                  ? `/jobs/${encodeURIComponent(row.prior_jobdiva_id)}/report?candidateId=${encodeURIComponent(row.candidate_id || "")}`
                  : null;
                const score = screenScore(row);
                return (
                  <tr key={row.id} className={cn("border-b border-slate-100 align-top", added && "opacity-60")}>
                    <td className="px-4 py-2.5">
                      <div className="font-semibold text-slate-800">{row.name || "Unnamed candidate"}</div>
                      {(row.headline || row.location) && (
                        <div className="text-[11px] text-slate-500 truncate max-w-[260px]">
                          {[row.headline, row.location].filter(Boolean).join(" · ")}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-slate-600">
                      {row.email && (
                        <div className="flex items-center gap-1 truncate max-w-[220px]"><Mail className="w-3 h-3 text-slate-400" />{row.email}</div>
                      )}
                      {row.phone && (
                        <div className="flex items-center gap-1"><Phone className="w-3 h-3 text-slate-400" />{row.phone}</div>
                      )}
                      {!row.email && !row.phone && <span className="text-slate-400">—</span>}
                    </td>
                    <td className="px-3 py-2.5 text-slate-700">
                      <div className="font-medium">{row.prior_jobdiva_id || "—"}</div>
                      <div className="text-[11px] text-slate-500 truncate max-w-[240px]">
                        {[row.prior_job_title, row.prior_customer_name].filter(Boolean).join(" · ")}
                      </div>
                    </td>
                    <td className="px-3 py-2.5 whitespace-nowrap">
                      <div className="flex items-center gap-1.5">
                        <ResultBadge result={row.screen_result} />
                        {score && <span className="text-[11px] text-slate-500">{score}</span>}
                      </div>
                      <div className="text-[11px] text-slate-400 mt-0.5">{formatDate(row.screened_at)}</div>
                    </td>
                    <td className="px-3 py-2.5 text-right whitespace-nowrap">
                      <MatchScorePill score={row.match_score} />
                    </td>
                    <td className="px-4 py-2.5 text-right whitespace-nowrap">
                      <div className="inline-flex items-center gap-1.5">
                        {reportHref && (
                          <Link
                            href={reportHref}
                            target="_blank"
                            className="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-slate-200 text-[11px] font-medium text-slate-700 hover:bg-slate-50"
                            title="Open the PAIR screen report from the previous job"
                          >
                            <ExternalLink className="w-3 h-3" /> Screen report
                          </Link>
                        )}
                        <button
                          type="button"
                          onClick={() => handleAdd(row)}
                          disabled={added || addingId != null}
                          className={cn(
                            "inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-semibold",
                            added
                              ? "bg-emerald-50 text-emerald-700 border border-emerald-200 cursor-default"
                              : "bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-60",
                          )}
                          title={added ? `Added ${formatDate(row.added_at)}` : "Copy this candidate into this job's list (Pending — launch PAIR from the list)"}
                        >
                          {addingId === row.id ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : added ? (
                            <Check className="w-3 h-3" />
                          ) : (
                            <UserPlus className="w-3 h-3" />
                          )}
                          {added ? "Added" : "Add to job"}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="px-4 py-2 text-[11px] text-slate-500 border-t border-slate-100">
            "Screen result" is the PAIR phone-screen outcome for the <em>previous</em> job (In Progress = partially completed).
            "Match" is the résumé match against <em>this</em> job's Step 5 criteria. Adding a candidate does not start outreach.
          </div>
        </div>
      )}
    </div>
  );
}

export default CrossSubmissionsPanel;
