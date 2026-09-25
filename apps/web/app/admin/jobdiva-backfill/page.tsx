"use client";

// Admin tool: repair the JobDiva profiles Launch PAIR created blank (before the
// 2026-09-25 fix every such profile got an empty résumé, JobDiva's Auto_ email
// and no phone/address). Backend: POST /engage/jobdiva-blank-profile-backfill
// (services/jobdiva_profile_backfill.py). A dry run shows what each profile
// would get; "Apply" then repairs exactly the profiles that dry run listed.
// Reachable by URL only (/admin/jobdiva-backfill) -- a one-off admin tool.

import { useState } from "react";
import Link from "next/link";
import { ArrowLeft, FileCheck2, Play, ShieldAlert, Wrench } from "lucide-react";
import { api } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

type BackfillReport = {
  jobdiva_candidate_id: string;
  source?: string;
  status: string;
  upload_resume?: boolean;
  resume_sections?: string[];
  resume_chars?: number;
  fill_fields?: string[];
  social_links?: string[];
  refused_fields?: string[];
  email_held_by_another_profile?: boolean;
  note?: string;
};

type BackfillResponse = {
  dry_run: boolean;
  processed: number;
  statuses: Record<string, number>;
  likely_duplicates: string[];
  reports: BackfillReport[];
};

const FIELD_LABELS: Record<string, string> = {
  firstName: "first name",
  lastName: "last name",
  email: "email",
  alternateemail: "alternate email",
  phones: "phone",
  city: "city",
  state: "state",
  zipCode: "zip",
  countryid: "country",
};

function hasWork(report: BackfillReport): boolean {
  return (
    report.status === "planned" &&
    Boolean(report.upload_resume || report.fill_fields?.length || report.social_links?.length)
  );
}

export default function AdminJobDivaBackfillPage() {
  const { isAdmin, isLoading: isRoleLoading, email, role } = useUserRole();
  const [limit, setLimit] = useState(25);
  const [jobId, setJobId] = useState("");
  const [running, setRunning] = useState<"dry" | "apply" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [plan, setPlan] = useState<BackfillResponse | null>(null);
  const [applied, setApplied] = useState<BackfillResponse | null>(null);

  const planned = (plan?.reports || []).filter(hasWork);

  const runDry = async () => {
    setRunning("dry");
    setError(null);
    setApplied(null);
    try {
      const res = await api.engagement.jobdivaBlankProfileBackfill({
        dry_run: true,
        limit,
        job_id: jobId.trim() || undefined,
      });
      setPlan(res as BackfillResponse);
    } catch (err: any) {
      setError(err?.message || "Dry run failed.");
    } finally {
      setRunning(null);
    }
  };

  const runApply = async () => {
    const ids = planned.map((r) => r.jobdiva_candidate_id);
    if (!ids.length) return;
    if (!window.confirm(`Repair ${ids.length} JobDiva profile(s) listed in this dry run? This writes to JobDiva.`)) {
      return;
    }
    setRunning("apply");
    setError(null);
    try {
      const res = await api.engagement.jobdivaBlankProfileBackfill({
        dry_run: false,
        limit: ids.length,
        jobdiva_ids: ids,
      });
      setApplied(res as BackfillResponse);
    } catch (err: any) {
      setError(err?.message || "Apply failed.");
    } finally {
      setRunning(null);
    }
  };

  if (isRoleLoading) {
    return (
      <div className="flex h-[80vh] w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-[3px] border-primary border-t-transparent" />
          <p className="text-[13px] font-medium text-slate-500">Verifying administrative access...</p>
        </div>
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <div className="flex h-[80vh] w-full items-center justify-center p-6">
        <Card className="max-w-md w-full text-center p-8 border-slate-200 bg-white shadow-sm rounded-xl">
          <div className="mx-auto w-12 h-12 rounded-full bg-red-50 border border-red-100 flex items-center justify-center mb-4 text-red-600">
            <ShieldAlert className="w-6 h-6" />
          </div>
          <h1 className="text-[20px] font-bold text-slate-900 mb-2">Access Restricted</h1>
          <p className="text-slate-500 text-[13px] mb-6 leading-relaxed">
            You are signed in as <span className="font-semibold text-slate-800">{email || "a Recruiter"}</span> with the <span className="uppercase font-semibold text-[11px] bg-slate-100 px-2 py-0.5 rounded text-slate-700">{role.replace("_", " ")}</span> role. The JobDiva profile backfill is available to Administrators only.
          </p>
          <Link href="/">
            <Button className="w-full gap-2 bg-slate-900 hover:bg-slate-800 text-white rounded-lg h-10 font-semibold text-[13px]">
              <ArrowLeft className="w-4 h-4" />
              Return to Jobs Dashboard
            </Button>
          </Link>
        </Card>
      </div>
    );
  }

  const results = applied || plan;

  return (
    <div className="space-y-6 max-w-[1100px] mx-auto pb-10">
      <div className="flex items-center gap-3 mt-2">
        <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">JobDiva Profile Backfill</h1>
      </div>

      <p className="text-[13px] text-slate-500 leading-relaxed">
        Before the fix, every JobDiva profile Launch PAIR created got an empty résumé, JobDiva&apos;s placeholder email
        and no phone or address. This uploads the résumé PAIR can build from the person&apos;s LinkedIn data and fills
        only fields that are still blank (email, alternate email, phone, city / state / country, LinkedIn and other
        links). Nothing entered in JobDiva is overwritten, and people sourced from JobDiva are never touched. Run a dry
        run first; <span className="font-semibold">Apply</span> repairs exactly the profiles it lists.
      </p>

      <Card className="p-5 border-slate-200 bg-white shadow-sm rounded-xl flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-[12px] font-semibold text-slate-600">
          Profiles per run (max 50)
          <input
            type="number"
            min={1}
            max={50}
            value={limit}
            onChange={(e) => setLimit(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
            className="h-10 w-32 rounded-lg border border-slate-200 px-3 text-[13px] font-normal text-slate-900"
          />
        </label>
        <label className="flex flex-col gap-1 text-[12px] font-semibold text-slate-600">
          Job (optional)
          <input
            type="text"
            placeholder="e.g. 26-15314"
            value={jobId}
            onChange={(e) => setJobId(e.target.value)}
            className="h-10 w-48 rounded-lg border border-slate-200 px-3 text-[13px] font-normal text-slate-900"
          />
        </label>
        <Button
          onClick={runDry}
          disabled={running !== null}
          className="h-10 gap-2 rounded-lg bg-slate-900 text-white text-[13px] font-semibold hover:bg-slate-800"
        >
          <Play className="w-4 h-4" />
          {running === "dry" ? "Running dry run…" : "Dry run"}
        </Button>
        <Button
          onClick={runApply}
          disabled={running !== null || planned.length === 0 || applied !== null}
          variant="outline"
          className="h-10 gap-2 rounded-lg border-slate-300 text-[13px] font-semibold"
        >
          <Wrench className="w-4 h-4" />
          {running === "apply" ? "Repairing…" : `Apply to ${planned.length} profile(s)`}
        </Button>
      </Card>

      {error && <Card className="p-4 border-red-200 bg-red-50 text-[13px] text-red-700 rounded-xl">{error}</Card>}

      {results && (
        <Card className="border-slate-200 bg-white shadow-sm rounded-xl overflow-hidden">
          <div className="px-5 py-4 border-b border-slate-100 flex flex-wrap items-center gap-3 text-[13px]">
            <FileCheck2 className="w-4 h-4 text-slate-500" />
            <span className="font-semibold text-slate-900">
              {results.dry_run ? "Dry run" : "Applied"}: {results.processed} profile(s)
            </span>
            {Object.entries(results.statuses || {}).map(([status, count]) => (
              <span key={status} className="rounded-md bg-slate-100 px-2 py-0.5 text-[12px] text-slate-600">
                {status.replace(/_/g, " ")}: {count}
              </span>
            ))}
            {results.likely_duplicates?.length > 0 && (
              <span className="rounded-md bg-amber-50 px-2 py-0.5 text-[12px] text-amber-800 ring-1 ring-amber-200">
                likely duplicates to merge: {results.likely_duplicates.join(", ")}
              </span>
            )}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12.5px]">
              <thead className="bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-semibold">JobDiva ID</th>
                  <th className="px-4 py-2 font-semibold">Source</th>
                  <th className="px-4 py-2 font-semibold">Status</th>
                  <th className="px-4 py-2 font-semibold">Résumé upload</th>
                  <th className="px-4 py-2 font-semibold">Fields to fill</th>
                  <th className="px-4 py-2 font-semibold">Links</th>
                  <th className="px-4 py-2 font-semibold">Note</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {results.reports.map((r) => (
                  <tr key={r.jobdiva_candidate_id} className="align-top">
                    <td className="px-4 py-2 font-mono text-slate-800">{r.jobdiva_candidate_id}</td>
                    <td className="px-4 py-2 text-slate-600">{r.source || "—"}</td>
                    <td className="px-4 py-2 text-slate-800">{r.status.replace(/_/g, " ")}</td>
                    <td className="px-4 py-2 text-slate-600">
                      {r.upload_resume
                        ? `yes (${(r.resume_sections || []).join(", ") || "résumé"}, ${r.resume_chars ?? 0} chars)`
                        : "—"}
                    </td>
                    <td className="px-4 py-2 text-slate-600">
                      {(r.fill_fields || []).map((f) => FIELD_LABELS[f] || f).join(", ") || "—"}
                    </td>
                    <td className="px-4 py-2 text-slate-600">{(r.social_links || []).join(", ") || "—"}</td>
                    <td className="px-4 py-2 text-slate-600">
                      {r.email_held_by_another_profile
                        ? "Email belongs to another JobDiva record — likely duplicate; saved as alternate email"
                        : r.refused_fields?.length
                          ? `JobDiva refused: ${r.refused_fields.join(", ")}`
                          : r.note || ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
