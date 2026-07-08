"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  Plus,
  Briefcase,
  Mail,
  ShieldCheck,
  Pencil,
  AlertTriangle,
  Rocket,
  ArrowRight,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { CampaignForm } from "@/components/campaigns/CampaignForm";
import { cn } from "@/lib/utils";
import {
  Campaign,
  CampaignCreatePayload,
  addJobToCampaign,
  getCampaign,
  updateCampaign,
} from "@/lib/campaigns";

export default function CampaignDetailPage() {
  const params = useParams<{ campaignId: string }>();
  const router = useRouter();
  const campaignId = params.campaignId;

  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);

  const [editOpen, setEditOpen] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const [addOpen, setAddOpen] = useState(false);
  const [addMode, setAddMode] = useState<"jobdiva" | "external">("jobdiva");
  const [jobdivaId, setJobdivaId] = useState("");
  const [extTitle, setExtTitle] = useState("");
  const [extDescription, setExtDescription] = useState("");
  const [isAdding, setIsAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  const load = useCallback(async () => {
    setIsLoading(true);
    setLoadFailed(false);
    try {
      setCampaign(await getCampaign(campaignId));
    } catch (e) {
      console.error("Failed to load campaign", e);
      setLoadFailed(true);
    } finally {
      setIsLoading(false);
    }
  }, [campaignId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3500);
    return () => clearTimeout(t);
  }, [toast]);

  const handleEdit = async (payload: CampaignCreatePayload) => {
    setIsSaving(true);
    try {
      await updateCampaign(campaignId, payload);
      setEditOpen(false);
      await load();
      setToast({ message: "Campaign updated", type: "success" });
    } catch (e) {
      console.error("Failed to update campaign", e);
      setToast({ message: "Couldn't update the campaign.", type: "error" });
    } finally {
      setIsSaving(false);
    }
  };

  const resetAddForm = () => {
    setJobdivaId("");
    setExtTitle("");
    setExtDescription("");
    setAddError(null);
    setAddMode("jobdiva");
  };

  const handleAddJob = async () => {
    setAddError(null);
    if (addMode === "jobdiva" && !jobdivaId.trim()) {
      setAddError("Enter a JobDiva Job ID");
      return;
    }
    if (addMode === "external" && (!extTitle.trim() || !extDescription.trim())) {
      setAddError("Title and description are required for an external requirement");
      return;
    }
    setIsAdding(true);
    try {
      const { ref } = await addJobToCampaign(
        campaignId,
        addMode === "jobdiva"
          ? { jobdiva_id: jobdivaId.trim() }
          : { title: extTitle.trim(), description: extDescription.trim() },
      );
      setAddOpen(false);
      resetAddForm();
      // Auto-prep done (common props + JD/rubric/questions template inherited).
      // Hand off to the existing jobs wizard's Source step for candidate review
      // + manual Launch PAIR.
      router.push(`/jobs/new?jobId=${encodeURIComponent(ref)}&mode=source&step=5`);
    } catch (e) {
      console.error("Failed to add job", e);
      setAddError("Couldn't add the job. Try again.");
      setIsAdding(false);
    }
  };

  if (isLoading) {
    return (
      <div className="max-w-6xl mx-auto">
        <Skeleton className="h-6 w-32 mb-6" />
        <Skeleton className="h-40 rounded-xl mb-6" />
        <Skeleton className="h-64 rounded-xl" />
      </div>
    );
  }

  if (loadFailed || !campaign) {
    return (
      <div className="max-w-6xl mx-auto flex flex-col items-center justify-center py-20 text-center">
        <AlertTriangle className="h-10 w-10 text-amber-500 mb-3" />
        <p className="text-slate-700 font-medium">Couldn&apos;t load this campaign</p>
        <div className="flex gap-2 mt-4">
          <Button variant="outline" onClick={() => router.push("/campaigns")}>
            Back to Campaigns
          </Button>
          <Button onClick={load}>Retry</Button>
        </div>
      </div>
    );
  }

  const jobs = campaign.jobs ?? [];
  const launchedCount = jobs.filter((j) => j.pair_launched_at).length;
  const openSource = (j: (typeof jobs)[number]) =>
    router.push(`/jobs/new?jobId=${encodeURIComponent(j.jobdiva_id || j.job_id)}&mode=source&step=5`);

  return (
    <div className="max-w-6xl mx-auto">
      <Link
        href="/campaigns"
        className="inline-flex items-center text-sm text-slate-500 hover:text-slate-800 mb-4"
      >
        <ArrowLeft className="h-4 w-4 mr-1.5" />
        Campaigns
      </Link>

      {/* Header */}
      <div className="bg-white border border-slate-200 rounded-xl p-6 mb-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-semibold text-slate-900 font-outfit">{campaign.name}</h1>
              {campaign.status !== "active" && (
                <Badge variant="outline" className="capitalize">
                  {campaign.status}
                </Badge>
              )}
            </div>
            {campaign.customer_name && (
              <p className="text-sm text-slate-500 mt-1">{campaign.customer_name}</p>
            )}
          </div>
          <Button variant="outline" onClick={() => setEditOpen(true)}>
            <Pencil className="h-4 w-4 mr-1.5" />
            Edit
          </Button>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6">
          <Stat icon={<ShieldCheck className="h-4 w-4" />} label="Screening">
            {campaign.screening_level}
          </Stat>
          <Stat icon={<Briefcase className="h-4 w-4" />} label="Employment">
            {campaign.selected_employment_types?.length
              ? campaign.selected_employment_types.join(", ")
              : "—"}
          </Stat>
          <Stat icon={<Mail className="h-4 w-4" />} label="Recruiters">
            {campaign.recruiter_emails?.length ?? 0}
          </Stat>
          <Stat icon={<Briefcase className="h-4 w-4" />} label="Job Boards">
            {campaign.selected_job_boards?.length
              ? campaign.selected_job_boards.join(", ")
              : "—"}
          </Stat>
        </div>

        {campaign.bot_introduction && (
          <div className="mt-5 border-t border-slate-100 pt-4">
            <p className="text-xs font-medium text-slate-400 uppercase tracking-wide mb-1">
              Bot Introduction
            </p>
            <p className="text-sm text-slate-600 whitespace-pre-wrap">{campaign.bot_introduction}</p>
          </div>
        )}
      </div>

      {/* Jobs */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold text-slate-900">
          Jobs <span className="text-slate-400 font-normal">({jobs.length})</span>
          {jobs.length > 0 && (
            <span className="ml-3 text-sm font-normal text-emerald-600">{launchedCount}/{jobs.length} launched</span>
          )}
        </h2>
        <Button onClick={() => setAddOpen(true)}>
          <Plus className="h-4 w-4 mr-1.5" />
          Add Job
        </Button>
      </div>

      {jobs.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center border border-dashed border-slate-200 rounded-xl bg-white">
          <Briefcase className="h-9 w-9 text-slate-300 mb-3" />
          <p className="text-slate-700 font-medium">No jobs in this campaign yet</p>
          <p className="text-sm text-slate-500 mt-1">
            Add a job — it inherits this campaign&apos;s common settings.
          </p>
        </div>
      ) : (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Job</TableHead>
                <TableHead>Customer</TableHead>
                <TableHead>Screening</TableHead>
                <TableHead>Launch</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((j) => (
                <TableRow key={j.job_id} className="cursor-pointer" onClick={() => openSource(j)}>
                  <TableCell className="font-medium text-slate-800">
                    {j.enhanced_title || j.title || j.jobdiva_id || j.job_id}
                    {j.jobdiva_id && (
                      <span className="text-slate-400 font-normal ml-2">{j.jobdiva_id}</span>
                    )}
                  </TableCell>
                  <TableCell className="text-slate-600">{j.customer_name || "—"}</TableCell>
                  <TableCell className="text-slate-600">{j.screening_level || "—"}</TableCell>
                  <TableCell>
                    {j.pair_launched_at ? (
                      <Badge className="bg-emerald-100 text-emerald-700 gap-1">
                        <Rocket className="h-3 w-3" />
                        Launched
                        {typeof j.candidates_launched === "number" && j.candidates_launched > 0 && (
                          <span className="font-normal">· {j.candidates_launched}</span>
                        )}
                      </Badge>
                    ) : (
                      <Badge variant="secondary">Not launched</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        openSource(j);
                      }}
                      className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
                    >
                      {j.pair_launched_at ? "Review" : "Review & Launch"}
                      <ArrowRight className="h-3.5 w-3.5" />
                    </button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* Edit dialog */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-lg max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Edit Campaign</DialogTitle>
            <DialogDescription>
              Changes apply to jobs added from now on — existing jobs keep the values they were
              created with.
            </DialogDescription>
          </DialogHeader>
          <CampaignForm
            initial={campaign}
            submitting={isSaving}
            submitLabel="Save Changes"
            onSubmit={handleEdit}
            onCancel={() => setEditOpen(false)}
          />
        </DialogContent>
      </Dialog>

      {/* Add-job dialog */}
      <Dialog
        open={addOpen}
        onOpenChange={(o) => {
          setAddOpen(o);
          if (!o) resetAddForm();
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Add Job to Campaign</DialogTitle>
            <DialogDescription>
              The job inherits this campaign&apos;s common settings + JD / rubric / screening
              template, then opens the sourcing view for review and launch.
            </DialogDescription>
          </DialogHeader>

          <div className="flex gap-2">
            {(["jobdiva", "external"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setAddMode(m)}
                className={cn(
                  "flex-1 px-3 py-2 rounded-lg border text-sm font-medium transition-colors",
                  addMode === m
                    ? "bg-primary text-white border-primary"
                    : "bg-white text-slate-600 border-slate-200 hover:border-slate-300",
                )}
              >
                {m === "jobdiva" ? "JobDiva Requirement" : "External Requirement"}
              </button>
            ))}
          </div>

          {addMode === "jobdiva" ? (
            <div className="space-y-1.5 mt-2">
              <Label htmlFor="add-jobdiva-id">JobDiva Job ID</Label>
              <Input
                id="add-jobdiva-id"
                value={jobdivaId}
                onChange={(e) => setJobdivaId(e.target.value)}
                placeholder="e.g. 26-08025"
              />
            </div>
          ) : (
            <div className="space-y-3 mt-2">
              <div className="space-y-1.5">
                <Label htmlFor="add-ext-title">Job Title</Label>
                <Input
                  id="add-ext-title"
                  value={extTitle}
                  onChange={(e) => setExtTitle(e.target.value)}
                  placeholder="e.g. Senior Java Engineer"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="add-ext-desc">Job Description</Label>
                <Textarea
                  id="add-ext-desc"
                  value={extDescription}
                  onChange={(e) => setExtDescription(e.target.value)}
                  rows={4}
                  placeholder="Paste the job description"
                />
              </div>
            </div>
          )}

          {addError && <p className="text-xs text-destructive">{addError}</p>}

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={() => setAddOpen(false)} disabled={isAdding}>
              Cancel
            </Button>
            <Button onClick={handleAddJob} disabled={isAdding}>
              {isAdding ? "Adding…" : "Add Job"}
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

function Stat({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wide">
        {icon}
        {label}
      </div>
      <p className="text-sm text-slate-800 mt-1 font-medium">{children}</p>
    </div>
  );
}
