"use client";

import React from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { JobLiveReportPanel } from "@/components/jobs/JobLiveReportPanel";

export default function JobLiveReportPage() {
  const params = useParams();
  const rawJobId = params?.jobId as string;

  return (
    <div className="flex flex-col gap-4 p-6 max-w-7xl mx-auto w-full">
      <div className="flex items-center gap-3 border-b border-slate-200 pb-3">
        <Link
          href={`/jobs/${rawJobId}/rankings`}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-slate-700 bg-white hover:bg-slate-100 border border-slate-200 rounded-lg shadow-2xs transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          <span>Back to Rankings</span>
        </Link>
        <span className="text-xs text-slate-400">/</span>
        <span className="text-xs font-medium text-slate-600">Dedicated Job Live Telemetry</span>
      </div>

      <JobLiveReportPanel jobId={rawJobId} />
    </div>
  );
}
