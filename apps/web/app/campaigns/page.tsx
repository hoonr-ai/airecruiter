"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Plus, Megaphone, Briefcase, AlertTriangle, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Campaign, listCampaigns, deleteCampaign, formatScreeningLevel } from "@/lib/campaigns";

export default function CampaignsPage() {
  const router = useRouter();
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  const [campaignToDelete, setCampaignToDelete] = useState<{ id: string; name: string } | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const handleDeleteCampaign = async () => {
    if (!campaignToDelete) return;
    setIsDeleting(true);
    try {
      await deleteCampaign(campaignToDelete.id);
      setCampaignToDelete(null);
      await load();
      setToast({ message: "Campaign archived successfully", type: "success" });
    } catch (e) {
      console.error("Failed to delete campaign", e);
      setToast({ message: "Couldn't delete the campaign.", type: "error" });
    } finally {
      setIsDeleting(false);
    }
  };

  const load = useCallback(async () => {
    setIsLoading(true);
    setLoadFailed(false);
    try {
      setCampaigns(await listCampaigns());
    } catch (e) {
      console.error("Failed to load campaigns", e);
      setLoadFailed(true);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3500);
    return () => clearTimeout(t);
  }, [toast]);

  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex items-start justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 font-outfit">Campaigns</h1>
          <p className="text-sm text-slate-500 mt-1">
            Group related jobs under shared settings and a reusable template — then add jobs in one step.
          </p>
        </div>
        <Button onClick={() => router.push("/campaigns/new")}>
          <Plus className="h-4 w-4 mr-1.5" />
          New Campaign
        </Button>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-40 rounded-xl" />
          ))}
        </div>
      ) : loadFailed ? (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <AlertTriangle className="h-10 w-10 text-amber-500 mb-3" />
          <p className="text-slate-700 font-medium">Couldn&apos;t load campaigns</p>
          <p className="text-sm text-slate-500 mt-1">The campaigns service didn&apos;t respond.</p>
          <Button variant="outline" className="mt-4" onClick={load}>
            Retry
          </Button>
        </div>
      ) : campaigns.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center border border-dashed border-slate-200 rounded-xl bg-white">
          <Megaphone className="h-10 w-10 text-slate-300 mb-3" />
          <p className="text-slate-700 font-medium">No campaigns yet</p>
          <p className="text-sm text-slate-500 mt-1 max-w-sm">
            Create a campaign to hold the common settings for a batch of jobs and automate the repetitive prep.
          </p>
          <Button className="mt-4" onClick={() => router.push("/campaigns/new")}>
            <Plus className="h-4 w-4 mr-1.5" />
            New Campaign
          </Button>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {campaigns.map((c) => (
            <div
              key={c.campaign_id}
              onClick={() => router.push(`/campaigns/${c.campaign_id}`)}
              className="text-left bg-white border border-slate-200 rounded-xl p-5 hover:border-primary/40 hover:shadow-md transition-all cursor-pointer relative group"
            >
              <div className="flex items-start justify-between gap-3">
                <h3 className="font-semibold text-slate-900 line-clamp-2">{c.name}</h3>
                <div className="flex items-center gap-1.5 shrink-0">
                  <Badge variant="secondary">{formatScreeningLevel(c.screening_level)}</Badge>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setCampaignToDelete({ id: c.campaign_id, name: c.name });
                    }}
                    className="p-1.5 hover:bg-red-50 text-slate-400 hover:text-red-600 rounded transition-colors"
                    title="Delete Campaign"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
              {c.customer_name && <p className="text-sm text-slate-500 mt-1">{c.customer_name}</p>}
              <div className="flex items-center gap-2 mt-4 text-sm text-slate-600">
                <Briefcase className="h-4 w-4 text-slate-400" />
                {c.job_count ?? 0} {(c.job_count ?? 0) === 1 ? "job" : "jobs"}
              </div>
              {c.selected_employment_types?.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-3">
                  {c.selected_employment_types.map((t) => (
                    <span key={t} className="text-xs bg-slate-100 text-slate-600 rounded px-2 py-0.5">
                      {t}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Delete confirmation dialog */}
      <Dialog
        open={!!campaignToDelete}
        onOpenChange={(open) => {
          if (!open) setCampaignToDelete(null);
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete Campaign</DialogTitle>
            <DialogDescription>
              Are you sure you want to archive <strong>{campaignToDelete?.name}</strong>? The child jobs and
              candidate history will remain accessible in your main jobs portfolio.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setCampaignToDelete(null)} disabled={isDeleting}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDeleteCampaign}
              disabled={isDeleting}
              className="bg-red-600 hover:bg-red-700 text-white"
            >
              {isDeleting ? "Deleting..." : "Delete Campaign"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {toast && (
        <div
          className={`fixed bottom-6 right-6 px-4 py-3 rounded-lg shadow-lg text-sm text-white ${
            toast.type === "success" ? "bg-emerald-600" : "bg-red-600"
          }`}
        >
          {toast.message}
        </div>
      )}
    </div>
  );
}
