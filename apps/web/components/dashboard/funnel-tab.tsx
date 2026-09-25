"use client";

import { Fragment, useState } from "react";
import Link from "next/link";
import { ArrowDown, ArrowUp, Ban, CheckCircle2, Clock, Filter, Hourglass, Rocket, Send, Timer } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { conversion, formatCount, formatHours, formatRate, hoursAsDays, shareOfTop } from "@/lib/dashboard";
import type { FunnelData, FunnelStage, PendingCandidate, SpeedMetric } from "./types";

const STAGE_COLORS: Record<string, string> = {
  initiated: "#8b5cf6",
  completed: "#7c3aed",
  passed: "#16a34a",
  pair_submission: "#0891b2",
  pair_interview: "#0284c7",
  pair_start: "#0e7490",
};

const SEGMENT_COLORS: Record<string, Record<string, string>> = {
  initiated: { applied: "#a78bfa", jobdiva: "#6d28d9", linkedin: "#4c1d95", other: "#c4b5fd" },
  pair_submission: { direct: "#0891b2", cross: "#67e8f9" },
  pair_interview: { direct: "#0284c7", cross: "#7dd3fc" },
  pair_start: { direct: "#0e7490", cross: "#5eead4" },
};

function FunnelRow({ stage, top }: { stage: FunnelStage; top: number }) {
  const isOther = stage.group === "non_pair";
  const width = shareOfTop(top, stage.count) * 100;
  const color = isOther ? "#94a3b8" : STAGE_COLORS[stage.key] ?? "#7c3aed";
  const segments = stage.segments?.filter((s) => s.count > 0) ?? [];
  return (
    <div className="grid grid-cols-[150px_1fr_64px] items-center gap-3">
      <div className={`text-[12.5px] font-semibold ${isOther ? "text-slate-500" : "text-slate-800"}`}>{stage.label}</div>
      <div>
        <div className="h-5 w-full rounded bg-slate-100">
          {stage.unavailable ? null : segments.length > 0 ? (
            <div className="flex h-5 overflow-hidden rounded" style={{ width: `${Math.max(width, stage.count ? 1.5 : 0)}%` }}>
              {segments.map((s) => (
                <div
                  key={s.key}
                  title={`${s.label}: ${formatCount(s.count)}`}
                  style={{ width: `${(s.count / stage.count) * 100}%`, background: SEGMENT_COLORS[stage.key]?.[s.key] ?? color }}
                />
              ))}
            </div>
          ) : (
            <div className="h-5 rounded" style={{ width: `${Math.max(width, stage.count ? 1.5 : 0)}%`, background: color }} />
          )}
        </div>
        {stage.segments && stage.segments.length > 0 && !stage.unavailable && (
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-500">
            {stage.segments.map((s) => (
              <span key={s.key} className="inline-flex items-center gap-1">
                <span
                  className="inline-block h-2 w-2 rounded-sm"
                  style={{ background: SEGMENT_COLORS[stage.key]?.[s.key] ?? color }}
                />
                {s.label} <span className="font-semibold text-slate-700">{formatCount(s.count)}</span>
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="text-right">
        <div className="text-[15px] font-bold tabular-nums text-slate-900">
          {stage.unavailable ? "—" : formatCount(stage.count)}
        </div>
        <div className="text-[10.5px] font-semibold text-slate-400 tabular-nums">
          {stage.unavailable ? "syncing" : formatRate(top ? stage.count / top : null)}
        </div>
      </div>
    </div>
  );
}

function ConversionRow({ previous, current }: { previous: FunnelStage; current: FunnelStage }) {
  if (previous.unavailable || current.unavailable) return <div className="h-2" />;
  const rate = conversion(previous.count, current.count);
  const drop = previous.count - current.count;
  const good = rate !== null && rate >= 0.5;
  // A later stage can outgrow an earlier one: JobDiva interviews and starts
  // whose submittal predates the mirrored history still count.
  const Arrow = rate !== null && rate > 1 ? ArrowUp : ArrowDown;
  return (
    <div className="grid grid-cols-[150px_1fr_64px] gap-3">
      <div />
      <div
        className={`flex items-center gap-1.5 text-[11px] font-semibold ${good ? "text-emerald-600" : "text-rose-600"}`}
        title="Share of the stage above that reached this stage"
      >
        <Arrow className="h-3 w-3" />
        {rate === null ? "—" : formatRate(rate)}
        {drop > 0 && <span className="font-medium text-slate-400">−{formatCount(drop)}</span>}
      </div>
      <div />
    </div>
  );
}

function Funnel({ stages }: { stages: FunnelStage[] }) {
  const top = stages[0]?.count ?? 0;
  return (
    <div className="space-y-1.5">
      {stages.map((stage, i) => {
        const previous = stages[i - 1];
        const startsOther = stage.group === "non_pair" && previous?.group !== "non_pair";
        return (
          <Fragment key={stage.key}>
            {startsOther && (
              <div className="flex items-center gap-3 py-2">
                <div className="h-px flex-1 bg-slate-200" />
                <span className="text-[10.5px] font-bold uppercase tracking-wider text-slate-400">
                  Non-PAIR pipeline — not from PAIR Engage
                </span>
                <div className="h-px flex-1 bg-slate-200" />
              </div>
            )}
            {previous && !startsOther && <ConversionRow previous={previous} current={stage} />}
            <FunnelRow stage={stage} top={top} />
          </Fragment>
        );
      })}
    </div>
  );
}

const SPEED_ICONS: Record<string, typeof Rocket> = {
  time_to_launch: Rocket,
  time_to_first_pass: CheckCircle2,
  time_to_first_submit: Send,
  time_to_first_submit_posted: Timer,
};
const SPEED_COLORS: Record<string, string> = {
  time_to_launch: "#7c3aed",
  time_to_first_pass: "#16a34a",
  time_to_first_submit: "#0891b2",
  time_to_first_submit_posted: "#9333ea",
};

function SpeedTile({ metric }: { metric: SpeedMetric }) {
  const Icon = SPEED_ICONS[metric.key] ?? Clock;
  const color = SPEED_COLORS[metric.key] ?? "#475569";
  return (
    <div className="flex items-start gap-3 rounded-lg border border-slate-100 bg-slate-50/50 p-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white shadow-sm">
        <Icon className="h-4 w-4" style={{ color }} />
      </div>
      <div className="min-w-0">
        <div className="text-[12px] font-semibold text-slate-700">
          {metric.label} <span className="font-medium text-slate-400">{metric.from}</span>
        </div>
        <div className="mt-0.5 text-[20px] font-bold leading-tight tabular-nums" style={{ color }}>
          {formatHours(metric.median_hours)}
        </div>
        <div className="text-[11px] text-slate-400">
          {metric.jobs
            ? `median · ${hoursAsDays(metric.median_hours)} · avg ${formatHours(metric.mean_hours)} · ${metric.jobs} ${
                metric.jobs === 1 ? "req" : "reqs"
              }`
            : "no requirement has reached this yet"}
        </div>
      </div>
    </div>
  );
}

function PendingFeedbackModal({
  open,
  onOpenChange,
  candidates,
  truncated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  candidates: PendingCandidate[];
  truncated: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl bg-white">
        <DialogHeader>
          <DialogTitle className="text-[16px] font-bold text-slate-900">Pending Feedback</DialogTitle>
          <DialogDescription className="text-[12.5px] text-slate-500">
            Candidates who passed PAIR Engage and have no Submit, Reject or Unreachable yet. Days are counted from the pass.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] overflow-auto rounded-lg border border-slate-200">
          <table className="w-full text-[12.5px]">
            <thead className="sticky top-0 bg-slate-50 text-left text-[11px] font-bold uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2">Candidate</th>
                <th className="px-3 py-2">Job</th>
                <th className="px-3 py-2">Recruiter</th>
                <th className="px-3 py-2 text-right">Days Pending</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c) => (
                <tr key={`${c.job_key}:${c.candidate_id}`} className="border-t border-slate-100 align-top">
                  <td className="px-3 py-2">
                    <Link
                      href={`/jobs/${encodeURIComponent(c.job_key)}/rankings`}
                      className="font-semibold text-primary hover:underline"
                    >
                      {c.name}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-slate-700">
                    <span className="font-semibold">{c.job_ref}</span>
                    {c.job_title && <span className="text-slate-500"> · {c.job_title}</span>}
                  </td>
                  <td className="px-3 py-2 text-slate-600" title={c.recruiters.join(", ")}>
                    {c.recruiters.length === 0
                      ? "—"
                      : c.recruiters.length <= 2
                        ? c.recruiters.join(", ")
                        : `${c.recruiters.slice(0, 2).join(", ")} +${c.recruiters.length - 2} more`}
                  </td>
                  <td
                    className={`px-3 py-2 text-right font-bold tabular-nums ${
                      (c.days_pending ?? 0) >= 7 ? "text-rose-600" : (c.days_pending ?? 0) >= 3 ? "text-amber-600" : "text-slate-700"
                    }`}
                  >
                    {c.days_pending === null ? "—" : formatCount(c.days_pending)}
                  </td>
                </tr>
              ))}
              {candidates.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-3 py-6 text-center text-slate-400">
                    Nobody is waiting on feedback.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {truncated && <p className="text-[12px] text-slate-500">Showing the {candidates.length} longest-waiting candidates.</p>}
      </DialogContent>
    </Dialog>
  );
}

export function FunnelTab({ data, loading }: { data: FunnelData | null; loading: boolean }) {
  const [pendingOpen, setPendingOpen] = useState(false);
  if (loading && !data) {
    return (
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="h-[420px] animate-pulse rounded-xl border border-slate-200 bg-white lg:col-span-2" />
        <div className="h-[420px] animate-pulse rounded-xl border border-slate-200 bg-white" />
      </div>
    );
  }
  if (!data) return null;
  const pending = data.pending_feedback;
  const givenRate = pending.passed ? pending.given / pending.passed : null;
  const maxReason = Math.max(...data.rejection_reasons.reasons.map((r) => r.count), 1);
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-2">
          <div className="mb-4 flex items-center gap-2">
            <Filter className="h-4 w-4 text-violet-600" />
            <h3 className="text-[14px] font-bold text-slate-900">Candidate Funnel</h3>
            <span className="text-[12px] text-slate-400">
              {formatCount(data.cohort.reqs)} {data.cohort.reqs === 1 ? "requirement" : "requirements"}
            </span>
          </div>
          {data.cohort.reqs === 0 ? (
            <p className="py-10 text-center text-[13px] text-slate-400">No launched PAIR requirement was posted in this range.</p>
          ) : (
            <Funnel stages={data.stages} />
          )}
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-1 flex items-center gap-2">
            <Timer className="h-4 w-4 text-sky-600" />
            <h3 className="text-[14px] font-bold text-slate-900">Speed to Pipeline</h3>
          </div>
          <p className="mb-4 text-[12px] text-slate-400">Hours, median across requirements — the Launch Report&apos;s times, rolled up</p>
          <div className="space-y-2.5">
            {data.speed.map((metric) => (
              <SpeedTile key={metric.key} metric={metric} />
            ))}
          </div>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-2">
          <div className="mb-1 flex items-center gap-2">
            <Ban className="h-4 w-4 text-rose-500" />
            <h3 className="text-[14px] font-bold text-slate-900">Rejection Reasons</h3>
          </div>
          <p className="mb-4 text-[12px] text-slate-400">
            Of {formatCount(data.rejection_reasons.total)} PAIR {data.rejection_reasons.total === 1 ? "candidate" : "candidates"} recruiters
            rejected
          </p>
          {data.rejection_reasons.reasons.length === 0 ? (
            <p className="py-6 text-center text-[13px] text-slate-400">No rejections recorded yet.</p>
          ) : (
            <div className="space-y-2">
              {data.rejection_reasons.reasons.map((r) => (
                <div key={r.reason} className="grid grid-cols-[minmax(0,220px)_1fr_80px] items-center gap-3">
                  <div className="text-[12.5px] leading-snug text-slate-700">{r.reason}</div>
                  <div className="h-2 rounded bg-slate-100">
                    <div className="h-2 rounded bg-violet-600" style={{ width: `${(r.count / maxReason) * 100}%` }} />
                  </div>
                  <div className="text-right text-[12px] tabular-nums text-slate-500">
                    {formatCount(r.count)} · {formatRate(r.count / data.rejection_reasons.total)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={() => setPendingOpen(true)}
          className="flex flex-col rounded-xl border border-slate-200 bg-white p-5 text-left shadow-sm transition-all hover:border-slate-300 hover:shadow-md"
          title="See who is waiting on feedback"
        >
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-amber-50">
            <Hourglass className="h-4 w-4 text-amber-600" />
          </div>
          <div className="mt-3 text-[28px] font-bold leading-none tabular-nums text-amber-600">{formatCount(pending.pending)}</div>
          <div className="mt-2 text-[11.5px] font-bold uppercase tracking-wide text-slate-600">Pending Feedback</div>
          <div className="mt-1 text-[12px] font-semibold">
            <span className="text-emerald-700">
              {formatCount(pending.given)} of {formatCount(pending.passed)} passed have feedback
            </span>
            <span className="text-slate-300"> · </span>
            <span className="text-slate-600">{formatRate(givenRate)}</span>
          </div>
          <div className="mt-3 h-2 w-full rounded bg-slate-100">
            <div className="h-2 rounded bg-emerald-500" style={{ width: `${(givenRate ?? 0) * 100}%` }} />
          </div>
          <div className="mt-auto pt-3 text-[12px] font-semibold text-primary">View candidates →</div>
        </button>
      </div>
      <PendingFeedbackModal
        open={pendingOpen}
        onOpenChange={setPendingOpen}
        candidates={pending.candidates}
        truncated={pending.truncated}
      />
    </div>
  );
}
