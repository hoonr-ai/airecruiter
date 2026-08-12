"use client";

// Read-only admin view of the no-contact company list.
//
// Candidates currently or last employed by any of these companies are shown
// greyed-out on Step 5 (no scoring, no actions, never saved/launched). The
// list itself is code-managed (apps/api/core/sourcing_config.py
// NO_CONTACT_COMPANIES) — there is deliberately no edit UI at this point;
// adding/removing a company is a code change.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Building2, RefreshCw, ShieldAlert, ShieldOff } from "lucide-react";
import { api } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export default function AdminNoContactPage() {
  const { isAdmin, isLoading: isRoleLoading, email, role } = useUserRole();
  const [companies, setCompanies] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchCompanies = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await api.noContact.companies();
      setCompanies(Array.isArray(res?.companies) ? res.companies : []);
    } catch (err: any) {
      console.error("Error loading no-contact companies:", err);
      setError(err?.message || "Failed to load the no-contact list.");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isRoleLoading && isAdmin) {
      fetchCompanies();
    }
  }, [isRoleLoading, isAdmin, fetchCompanies]);

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
            You are signed in as <span className="font-semibold text-slate-800">{email || "a Recruiter"}</span> with the <span className="uppercase font-semibold text-[11px] bg-slate-100 px-2 py-0.5 rounded text-slate-700">{role.replace("_", " ")}</span> role. The No Contact List is visible to Administrators only.
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

  return (
    <div className="space-y-6 max-w-[860px] mx-auto pb-10">
      {/* Page Header */}
      <div className="flex items-center justify-between mt-2">
        <div className="flex items-center gap-3">
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">No Contact List</h1>
          <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
            {companies.length} {companies.length === 1 ? "Company" : "Companies"}
          </span>
        </div>
        <Button
          variant="outline"
          onClick={fetchCompanies}
          disabled={isLoading}
          className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
        >
          <RefreshCw className={`h-4 w-4 text-slate-500 ${isLoading ? "animate-spin text-primary" : ""}`} />
          Refresh
        </Button>
      </div>

      <p className="text-[13px] text-slate-500 leading-relaxed">
        Candidates whose current or most recent employer matches a company below are still shown in Step 5
        search results, but greyed out — they are never scored, never saved, and no outreach action can be
        taken on them. This list is read-only here; adding or removing a company is done through a code
        change for now.
      </p>

      {error && (
        <Card className="p-4 border-red-200 bg-red-50 text-[13px] text-red-700 rounded-xl">{error}</Card>
      )}

      <Card className="border-slate-200 bg-white shadow-sm rounded-xl divide-y divide-slate-100">
        {isLoading ? (
          <div className="p-8 text-center text-[13px] text-slate-500">Loading no-contact companies…</div>
        ) : companies.length === 0 && !error ? (
          <div className="p-8 text-center text-[13px] text-slate-500">
            The no-contact list is currently empty.
          </div>
        ) : (
          companies.map((company) => (
            <div key={company} className="flex items-center gap-3 px-5 py-4">
              <div className="w-9 h-9 rounded-lg bg-slate-100 border border-slate-200 flex items-center justify-center text-slate-500">
                <Building2 className="w-4.5 h-4.5" />
              </div>
              <div className="flex-1">
                <p className="text-[14px] font-semibold text-slate-900">{company}</p>
                <p className="text-[12px] text-slate-500">
                  Loose match — catches variants like “{company} Inc.” or subsidiaries carrying the name.
                </p>
              </div>
              <span
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-extrabold uppercase tracking-wider text-slate-500"
                title="Candidates from this company are shown greyed-out and cannot be contacted"
              >
                <ShieldOff className="w-3 h-3" />
                No Contact
              </span>
            </div>
          ))
        )}
      </Card>
    </div>
  );
}
