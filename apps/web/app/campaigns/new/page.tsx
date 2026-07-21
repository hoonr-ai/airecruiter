"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { CampaignForm } from "@/components/campaigns/CampaignForm";
import { CampaignCreatePayload, createCampaign } from "@/lib/campaigns";

export default function NewCampaignPage() {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ message: string; type: "error" | "success" } | null>(null);

  const flash = (message: string, type: "error" | "success" = "error") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3500);
  };

  const handleCreate = async (payload: CampaignCreatePayload) => {
    setSaving(true);
    try {
      const { campaign_id } = await createCampaign(payload);
      router.push(`/campaigns/${campaign_id}`);
    } catch {
      flash("Couldn't create the campaign. Try again.");
      setSaving(false);
    }
  };

  return (
    <div className="p-8 max-w-7xl mx-auto pb-24 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <Link
        href="/campaigns"
        className="inline-flex items-center text-sm text-slate-500 hover:text-slate-800 mb-4"
      >
        <ArrowLeft className="h-4 w-4 mr-1.5" /> Campaigns
      </Link>

      <h1 className="text-2xl font-semibold text-slate-900 font-outfit mb-1">New Campaign</h1>
      <p className="text-sm text-slate-500 mb-6">
        Define shared operational and administrative settings. When you add jobs to this campaign, each job inherits these settings while generating its own role-specific description, skills, and technical screening questions.
      </p>

      <div className="bg-white border border-slate-200 rounded-xl p-6">
        <CampaignForm
          submitting={saving}
          submitLabel="Create Campaign"
          onSubmit={handleCreate}
          onCancel={() => router.push("/campaigns")}
        />
      </div>

      {toast && (
        <div className={`fixed bottom-20 right-6 px-4 py-3 rounded-lg shadow-lg text-sm text-white ${toast.type === "success" ? "bg-emerald-600" : "bg-red-600"}`}>
          {toast.message}
        </div>
      )}
    </div>
  );
}
