import Link from "next/link";
import { ArrowLeft, Ban } from "lucide-react";

export default function CandidatesPageDisabled() {
  return (
    <div className="space-y-6 max-w-[900px] mx-auto pb-10">
      <div className="flex items-center gap-4 mt-2">
        <Link
          href="/"
          className="rounded-full h-10 w-10 inline-flex items-center justify-center hover:bg-slate-100 transition-colors"
          aria-label="Back to jobs"
        >
          <ArrowLeft className="h-5 w-5 text-slate-400" />
        </Link>
        <div>
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Master Candidate Pool</h1>
          <p className="text-slate-500 text-[14px]">Temporarily disabled for this phase.</p>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-slate-200 shadow-[0_2px_10px_-4px_rgba(0,0,0,0.1)] p-8">
        <div className="flex items-start gap-4">
          <div className="w-10 h-10 rounded-full bg-slate-100 flex items-center justify-center shrink-0">
            <Ban className="w-5 h-5 text-slate-500" />
          </div>
          <div className="space-y-2">
            <h2 className="text-[18px] font-bold text-slate-900">Candidates tab is disabled</h2>
            <p className="text-slate-600 text-[14px] leading-relaxed">
              Please use candidate ranking lists inside each job (for example: <span className="font-semibold">Jobs → job → rankings</span>).
              Standalone candidate-pool calls are intentionally turned off.
            </p>
            <Link
              href="/"
              className="inline-flex items-center mt-2 px-4 h-9 rounded-lg bg-[#6366f1] text-white text-[13px] font-semibold hover:bg-[#4f46e5] transition-colors"
            >
              Go to Jobs
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
