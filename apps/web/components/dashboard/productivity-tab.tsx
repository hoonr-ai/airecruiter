"use client";

import { useState } from "react";
import Link from "next/link";
import { Briefcase, Flag, Send, TriangleAlert, UsersRound } from "lucide-react";
import { countChange, formatAverage, formatCount, formatShortDay } from "@/lib/dashboard";
import { KpiCard, TONES, TrendModal, type Tone, type TrendColumn } from "./kpi-card";
import type { Metric, ProductivityData } from "./types";

type Key = "reqs_assigned" | "client_subs" | "starts";

const CARDS: { key: Key; label: string; sub: string; icon: typeof Briefcase; tone: Tone; note: string }[] = [
  {
    key: "reqs_assigned",
    label: "Reqs Assigned",
    sub: "Avg open reqs per recruiter · per week",
    icon: Briefcase,
    tone: "violet",
    note: "Open requirements the team is tagged on in JobDiva, per recruiter, each week.",
  },
  {
    key: "client_subs",
    label: "Client Subs",
    sub: "Avg external submissions per recruiter · per week",
    icon: Send,
    tone: "sky",
    note: "External submissions made by the team's recruiters, per recruiter, each week.",
  },
  {
    key: "starts",
    label: "Starts",
    sub: "Avg starts per recruiter · per week",
    icon: Flag,
    tone: "emerald",
    note: "Starts (by start date) on the team's submissions, per recruiter, each week.",
  },
];

function split(metric: Metric | undefined) {
  const b = metric?.breakdown;
  if (!b || typeof b.pair !== "number" || typeof b.non_pair !== "number") return undefined;
  return (
    <span>
      <span className="whitespace-nowrap text-cyan-700">{formatAverage(b.pair)} PAIR</span>
      <span className="text-slate-300"> · </span>
      <span className="whitespace-nowrap text-purple-600">{formatAverage(b.non_pair)} Non-PAIR</span>
    </span>
  );
}

export function ProductivityTab({ data, loading }: { data: ProductivityData | null; loading: boolean }) {
  const [open, setOpen] = useState<(typeof CARDS)[number] | null>(null);

  if (data?.unavailable) {
    return (
      <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-5 text-[13px] text-amber-900">
        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
        <div>
          <p className="font-semibold">Productivity needs the recruiter → Recruiting Manager mapping.</p>
          <p className="mt-1">{data.unavailable}</p>
          {data.teams_configured === 0 && (
            <Link href="/admin/teams" className="mt-2 inline-block font-semibold text-primary hover:underline">
              Set up teams →
            </Link>
          )}
        </div>
      </div>
    );
  }

  const weeks = data?.weeks ?? [];
  const openMetric = open ? data?.metrics?.[open.key] : undefined;
  const columns: TrendColumn[] = [];
  return (
    <div className="space-y-4">
      <div className="flex items-start gap-2 text-[12.5px] text-slate-500">
        <UsersRound className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
        {data ? (
          <span>
            Averaged over <span className="font-semibold text-slate-700">{formatCount(data.recruiters ?? 0)} recruiters</span>
            {data.team_scope ? (
              <>
                {" "}
                on <span className="font-semibold text-slate-700">{data.team_scope.team_name}</span>
              </>
            ) : (
              " across all teams"
            )}
            . Weekly averages per recruiter; there is no per-recruiter view because JobDiva assigns requirements to whole teams.
          </span>
        ) : (
          <span>Loading…</span>
        )}
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {CARDS.map((card) => {
          const metric = data?.metrics?.[card.key];
          return (
            <KpiCard
              key={card.key}
              icon={card.icon}
              tone={card.tone}
              label={card.label}
              value={formatAverage(metric?.value)}
              sub={
                card.key === "reqs_assigned" && typeof metric?.breakdown?.reqs === "number"
                  ? `${card.sub} · ${formatCount(metric.breakdown.reqs)} reqs open in the period`
                  : card.sub
              }
              breakdown={split(metric)}
              change={countChange(metric?.value ?? null, metric?.previous ?? null)}
              allTime={data ? data.range === null : false}
              weekly={metric?.weekly ?? []}
              footnote={metric?.partial_from ? `JobDiva data from ${formatShortDay(metric.partial_from)}` : undefined}
              loading={loading && !data}
              onOpen={() => setOpen(card)}
            />
          );
        })}
      </div>
      {data?.unmapped_emails && data.unmapped_emails.length > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-[12.5px] text-amber-900">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          <span>
            {data.unmapped_emails.length === 1 ? "This team email is" : "These team emails are"} not in JobDiva&apos;s user
            directory, so {data.unmapped_emails.length === 1 ? "its" : "their"} submittals and tagged requirements are not
            counted: <span className="font-semibold">{data.unmapped_emails.join(", ")}</span>. Use the address the person
            signs in to JobDiva with on the Teams page.
          </span>
        </div>
      )}
      {open && openMetric && (
        <TrendModal
          open
          onOpenChange={(value) => !value && setOpen(null)}
          title={open.label}
          note={open.note}
          weeks={weeks}
          values={openMetric.weekly}
          color={TONES[open.tone].color}
          format={formatAverage}
          columns={columns}
        />
      )}
    </div>
  );
}
