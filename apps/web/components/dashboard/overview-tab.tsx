"use client";

import { useState, type ComponentType } from "react";
import {
  ArrowRightLeft,
  CheckCircle2,
  Flag,
  Handshake,
  Percent,
  PieChart,
  Rocket,
  Send,
  Target,
  Users,
} from "lucide-react";
import { countChange, formatCount, formatRate, formatShortDay, rateChange, type Change } from "@/lib/dashboard";
import { KpiCard, TONES, TrendModal, type Tone, type TrendColumn } from "./kpi-card";
import type { Metric, OverviewData, OverviewMetricKey } from "./types";

type CardSpec = {
  key: OverviewMetricKey;
  label: string;
  sub?: string;
  icon: ComponentType<{ className?: string; style?: React.CSSProperties }>;
  tone: Tone;
  split?: boolean;
  note: string;
};

const PIPELINE: CardSpec[] = [
  {
    key: "net_openings",
    label: "Net Openings",
    sub: "excl. Cancelled / On Hold / Declined / Ignored",
    icon: Rocket,
    tone: "violet",
    note: "Openings on requirements added to PAIR each week.",
  },
  {
    key: "pair_volume",
    label: "PAIR Volume as a % of Total",
    sub: "of all openings received (JobDiva)",
    icon: Target,
    tone: "purple",
    note: "Share of the openings Pyramid received each week that were launched with PAIR.",
  },
  {
    key: "candidates_launched",
    label: "PAIR Candidates Launched",
    icon: Users,
    tone: "violet",
    note: "Candidates first launched on a requirement each week.",
  },
  {
    key: "candidates_passed",
    label: "PAIR Candidates Passed",
    icon: CheckCircle2,
    tone: "emerald",
    note: "Candidates who reached an Engage Pass each week.",
  },
];

const OUTCOMES: CardSpec[] = [
  {
    key: "pair_submissions",
    label: "PAIR Submissions",
    icon: Send,
    tone: "sky",
    split: true,
    note: "External submissions of PAIR candidates each week (JobDiva).",
  },
  {
    key: "pair_interviews",
    label: "PAIR Interviews",
    icon: Handshake,
    tone: "blue",
    split: true,
    note: "Client interviews of PAIR candidates each week (JobDiva).",
  },
  {
    key: "pair_starts",
    label: "PAIR Starts",
    icon: Flag,
    tone: "emerald",
    split: true,
    note: "Starts of PAIR candidates, by start date (JobDiva).",
  },
  {
    key: "fill_ratio",
    label: "PAIR Fill Ratio",
    sub: "starts ÷ net openings",
    icon: PieChart,
    tone: "teal",
    note: "PAIR Starts ÷ Net Openings each week.",
  },
  {
    key: "submit_to_start",
    label: "PAIR Submit → Start",
    sub: "starts ÷ submissions",
    icon: ArrowRightLeft,
    tone: "teal",
    note: "PAIR Starts ÷ PAIR Submissions each week.",
  },
];

const NON_PAIR: CardSpec[] = [
  {
    key: "non_pair_submissions",
    label: "Non-PAIR Submissions on Reqs Assigned to PAIR",
    icon: Send,
    tone: "purple",
    note: "External submissions on PAIR requirements of candidates PAIR did not screen.",
  },
  {
    key: "non_pair_starts",
    label: "Non-PAIR Starts on Reqs Assigned to PAIR",
    icon: Flag,
    tone: "purple",
    note: "Starts from those submissions, by start date.",
  },
  {
    key: "pair_share",
    label: "PAIR Share of All Subs",
    sub: "PAIR ÷ (PAIR + Non-PAIR)",
    icon: Percent,
    tone: "sky",
    note: "PAIR Submissions ÷ all external submissions counted above, each week.",
  },
];

export function formatMetric(metric: Metric | undefined, value: number | null | undefined): string {
  if (!metric) return formatCount(null);
  return metric.kind === "ratio" ? formatRate(value) : formatCount(value);
}

export function metricChange(metric: Metric | undefined): Change {
  if (!metric) return null;
  return metric.kind === "ratio" ? rateChange(metric.value, metric.previous) : countChange(metric.value, metric.previous);
}

function num(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function SplitLine({ metric }: { metric: Metric }) {
  const b = metric.breakdown ?? {};
  return (
    <span>
      <span className="whitespace-nowrap text-cyan-700">{formatCount(num(b.direct))} Direct</span>
      <span className="text-slate-300"> · </span>
      <span className="whitespace-nowrap text-purple-600">{formatCount(num(b.cross))} Cross-Sub</span>
    </span>
  );
}

function subFor(spec: CardSpec, metric: Metric | undefined): string | undefined {
  if (!metric) return spec.sub;
  if (spec.key === "net_openings") {
    const reqs = num(metric.breakdown?.reqs);
    return `${formatCount(reqs)} ${reqs === 1 ? "requirement" : "requirements"} · ${spec.sub}`;
  }
  if (spec.key === "pair_volume" && metric.denominator) {
    return `${formatCount(metric.numerator)} of ${formatCount(metric.denominator)} openings`;
  }
  if (spec.key === "pair_submissions" && metric.breakdown && typeof metric.breakdown.recorded_in_pair === "number") {
    return `${formatCount(metric.breakdown.recorded_in_pair)} external Submits recorded in PAIR`;
  }
  if (metric.kind === "ratio" && metric.denominator) {
    return `${formatCount(metric.numerator)} of ${formatCount(metric.denominator)} · ${spec.sub ?? ""}`.replace(/ · $/, "");
  }
  return spec.sub;
}

export function OverviewTab({ data, loading }: { data: OverviewData | null; loading: boolean }) {
  const [open, setOpen] = useState<CardSpec | null>(null);
  const allTime = data ? data.range === null : false;
  const weeks = data?.weeks ?? [];

  const renderRow = (title: string, icon: ComponentType<{ className?: string }>, specs: CardSpec[]) => {
    const Icon = icon;
    return (
      <section>
        <h3 className="mb-2.5 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-slate-500">
          <Icon className="h-3.5 w-3.5" />
          {title}
        </h3>
        <div
          className={`grid grid-cols-1 gap-3 sm:grid-cols-2 ${
            specs.length >= 5 ? "lg:grid-cols-3 xl:grid-cols-5" : specs.length === 4 ? "lg:grid-cols-4" : "lg:grid-cols-3"
          }`}
        >
          {specs.map((spec) => {
            const metric = data?.metrics?.[spec.key];
            return (
              <KpiCard
                key={spec.key}
                icon={spec.icon}
                tone={spec.tone}
                label={spec.label}
                value={formatMetric(metric, metric?.value)}
                sub={subFor(spec, metric)}
                breakdown={spec.split && metric ? <SplitLine metric={metric} /> : undefined}
                change={metricChange(metric)}
                allTime={allTime}
                weekly={metric?.weekly ?? []}
                unavailable={metric?.unavailable}
                footnote={metric?.partial_from ? `JobDiva data from ${formatShortDay(metric.partial_from)}` : undefined}
                loading={loading && !data}
                onOpen={() => setOpen(spec)}
              />
            );
          })}
        </div>
      </section>
    );
  };

  const openMetric = open ? data?.metrics?.[open.key] : undefined;
  const columns: TrendColumn[] = [];
  if (open?.split && openMetric?.breakdown) {
    const direct = openMetric.breakdown.weekly_direct;
    const cross = openMetric.breakdown.weekly_cross;
    if (Array.isArray(direct)) columns.push({ label: "Direct", values: direct });
    if (Array.isArray(cross)) columns.push({ label: "Cross-Sub", values: cross });
  }

  return (
    <div className="space-y-6">
      {renderRow("PAIR Pipeline", Rocket, PIPELINE)}
      {renderRow("PAIR Outcomes", Flag, OUTCOMES)}
      {renderRow("Non-PAIR Comparison", Users, NON_PAIR)}
      {open && openMetric && (
        <TrendModal
          open
          onOpenChange={(value) => !value && setOpen(null)}
          title={open.label}
          note={open.note}
          weeks={weeks}
          values={openMetric.weekly}
          color={TONES[open.tone].color}
          format={(value) => formatMetric(openMetric, value)}
          columns={columns}
        />
      )}
    </div>
  );
}
