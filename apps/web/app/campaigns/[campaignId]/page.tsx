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
  ChevronRight,
  Trash2,
  MoreVertical,
  Unlink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { CampaignForm } from "@/components/campaigns/CampaignForm";
import { CampaignTemplateCard } from "@/components/campaigns/CampaignTemplateCard";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import {
  Campaign,
  CampaignChildJob,
  CampaignCreatePayload,
  addJobToCampaign,
  bulkAddJobsToCampaign,
  getCampaign,
  updateCampaign,
  deleteCampaign,
  removeJobFromCampaign,
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
  const [jobdivaIds, setJobdivaIds] = useState("");
  const [extTitle, setExtTitle] = useState("");
  const [extDescription, setExtDescription] = useState("");
  const [isAdding, setIsAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [isDeletingCampaign, setIsDeletingCampaign] = useState(false);
  const [jobToRemove, setJobToRemove] = useState<{ id: string; title: string; action: "detach" | "delete" } | null>(null);
  const [isRemovingJob, setIsRemovingJob] = useState(false);

  const handleDeleteCampaign = async () => {
    setIsDeletingCampaign(true);
    try {
      await deleteCampaign(campaignId);
      setToast({ message: "Campaign archived successfully", type: "success" });
      router.push("/campaigns");
    } catch (e) {
      console.error("Failed to delete campaign", e);
      setToast({ message: "Couldn't delete the campaign.", type: "error" });
      setIsDeletingCampaign(false);
      setDeleteConfirmOpen(false);
    }
  };

  const handleConfirmRemoveJob = async () => {
    if (!jobToRemove) return;
    setIsRemovingJob(true);
    try {
      await removeJobFromCampaign(campaignId, jobToRemove.id, jobToRemove.action);
      setJobToRemove(null);
      await load();
      setToast({
        message: jobToRemove.action === "detach" ? "Job detached from campaign" : "Requirement deleted",
        type: "success",
      });
    } catch (e) {
      console.error("Failed to remove job from campaign", e);
      setToast({ message: "Couldn't remove job from campaign.", type: "error" });
    } finally {
      setIsRemovingJob(false);
    }
  };

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
    const t = setTimeout(() => setToast(null), 4000);
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
    setJobdivaIds("");
    setExtTitle("");
    setExtDescription("");
    setAddError(null);
    setAddMode("jobdiva");
  };

  const handleAdd = async () => {
    setAddError(null);
    if (addMode === "jobdiva") {
      const ids = jobdivaIds
        .split(/[,\n]/)
        .map((s) => s.trim())
        .filter(Boolean);
      if (ids.length === 0) {
        setAddError("Enter at least one JobDiva Job ID");
        return;
      }
      setIsAdding(true);
      try {
        const r = await bulkAddJobsToCampaign(campaignId, ids);
        setAddOpen(false);
        resetAddForm();
        await load();
        const failed = r.requested - r.added;
        setToast({
          message:
            `Added ${r.added} of ${r.requested} job${r.requested === 1 ? "" : "s"}` +
            (r.fetched_from_jobdiva ? ` · ${r.fetched_from_jobdiva} fetched from JobDiva` : "") +
            (failed ? ` · ${failed} failed` : ""),
          type: failed ? "error" : "success",
        });
      } catch (e) {
        console.error("Failed to add jobs", e);
        setAddError("Couldn't add the jobs. Try again.");
      } finally {
        setIsAdding(false);
      }
    } else {
      if (!extTitle.trim() || !extDescription.trim()) {
        setAddError("Title and description are required for an external requirement");
        return;
      }
      setIsAdding(true);
      try {
        await addJobToCampaign(campaignId, {
          title: extTitle.trim(),
          description: extDescription.trim(),
        });
        setAddOpen(false);
        resetAddForm();
        await load();
        setToast({ message: "Job added to campaign", type: "success" });
      } catch (e) {
        console.error("Failed to add job", e);
        setAddError("Couldn't add the job. Try again.");
      } finally {
        setIsAdding(false);
      }
    }
  };

  const toggleExpand = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

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
  const openSource = (j: CampaignChildJob) =>
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
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={() => setEditOpen(true)}>
              <Pencil className="h-4 w-4 mr-1.5" />
              Edit
            </Button>
            <Button
              variant="outline"
              className="text-red-600 hover:text-red-700 hover:bg-red-50 border-red-200"
              onClick={() => setDeleteConfirmOpen(true)}
            >
              <Trash2 className="h-4 w-4 mr-1.5" />
              Delete Campaign
            </Button>
          </div>
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
            {campaign.selected_job_boards?.length ? campaign.selected_job_boards.join(", ") : "—"}
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

      <CampaignTemplateCard campaign={campaign} />

      {/* Jobs */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold text-slate-900">
          Jobs <span className="text-slate-400 font-normal">({jobs.length})</span>
          {jobs.length > 0 && (
            <span className="ml-3 text-sm font-normal text-emerald-600">
              {launchedCount}/{jobs.length} launched
            </span>
          )}
        </h2>
        <Button onClick={() => setAddOpen(true)}>
          <Plus className="h-4 w-4 mr-1.5" />
          Add Jobs
        </Button>
      </div>

      {jobs.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center border border-dashed border-slate-200 rounded-xl bg-white">
          <Briefcase className="h-9 w-9 text-slate-300 mb-3" />
          <p className="text-slate-700 font-medium">No jobs in this campaign yet</p>
          <p className="text-sm text-slate-500 mt-1">
            Add jobs by JobDiva ID — they inherit this campaign&apos;s settings + template.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {jobs.map((j) => {
            const isOpen = expanded.has(j.job_id);
            const location = [j.city, j.state].filter(Boolean).join(", ");
            return (
              <div key={j.job_id} className="bg-white border border-slate-200 rounded-xl overflow-hidden">
                <div
                  role="button"
                  tabIndex={0}
                  onClick={() => toggleExpand(j.job_id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      toggleExpand(j.job_id);
                    }
                  }}
                  className="w-full flex items-center gap-3 p-4 text-left cursor-pointer hover:bg-slate-50 transition-colors"
                >
                  <ChevronRight
                    className={cn("h-4 w-4 text-slate-400 shrink-0 transition-transform", isOpen && "rotate-90")}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-slate-800 truncate">
                      {j.enhanced_title || j.title || j.jobdiva_id || j.job_id}
                      {j.jobdiva_id && (
                        <span className="text-slate-400 font-normal ml-2">{j.jobdiva_id}</span>
                      )}
                    </div>
                    {j.customer_name && (
                      <div className="text-sm text-slate-500 truncate">{j.customer_name}</div>
                    )}
                  </div>
                  <LaunchBadge job={j} />
                  <div className="flex items-center gap-2 shrink-0">
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
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <button
                          type="button"
                          onClick={(e) => e.stopPropagation()}
                          className="p-1.5 hover:bg-slate-100 rounded text-slate-400 hover:text-slate-600 transition-colors"
                          title="Job actions"
                        >
                          <MoreVertical className="h-4 w-4" />
                        </button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem
                          onClick={(e) => {
                            e.stopPropagation();
                            setJobToRemove({
                              id: j.job_id,
                              title: j.enhanced_title || j.title || j.jobdiva_id || j.job_id,
                              action: "detach",
                            });
                          }}
                        >
                          <Unlink className="h-4 w-4 mr-2" />
                          Detach from Campaign
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          className="text-red-600 focus:text-red-600"
                          onClick={(e) => {
                            e.stopPropagation();
                            setJobToRemove({
                              id: j.job_id,
                              title: j.enhanced_title || j.title || j.jobdiva_id || j.job_id,
                              action: "delete",
                            });
                          }}
                        >
                          <Trash2 className="h-4 w-4 mr-2" />
                          Delete Requirement
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>

                {isOpen && (
                  <div className="border-t border-slate-100 p-4 grid grid-cols-2 md:grid-cols-3 gap-4">
                    <Field label="Location">
                      {location || "—"}
                      {j.location_type ? ` · ${j.location_type}` : ""}
                    </Field>
                    <Field label="Employment">{j.employment_type || "—"}</Field>
                    <Field label="Screening">{j.screening_level || "—"}</Field>
                    <Field label="Pay Rate">{j.pay_rate || "—"}</Field>
                    <Field label="Openings">{j.openings ? String(j.openings) : "—"}</Field>
                    <Field label="Candidates">
                      {(j.candidates_sourced ?? 0)} sourced · {(j.candidates_launched ?? 0)} launched
                    </Field>
                  </div>
                )}
              </div>
            );
          })}
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

      {/* Add-jobs dialog */}
      <Dialog
        open={addOpen}
        onOpenChange={(o: boolean) => {
          setAddOpen(o);
          if (!o) resetAddForm();
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Add Jobs to Campaign</DialogTitle>
            <DialogDescription>
              Each job inherits this campaign&apos;s common settings + JD / rubric / screening
              template. Launch each from its card.
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
                {m === "jobdiva" ? "JobDiva Requirements" : "External Requirement"}
              </button>
            ))}
          </div>

          {addMode === "jobdiva" ? (
            <div className="space-y-1.5 mt-2">
              <Label htmlFor="add-jobdiva-ids">JobDiva Job IDs</Label>
              <Textarea
                id="add-jobdiva-ids"
                value={jobdivaIds}
                onChange={(e) => setJobdivaIds(e.target.value)}
                rows={3}
                placeholder="Comma-separated, e.g. 26-08025, 26-09001, 26-09002"
              />
              <p className="text-xs text-slate-400">
                We&apos;ll fetch each job&apos;s details from JobDiva and add them under this campaign.
              </p>
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
            <Button onClick={handleAdd} disabled={isAdding}>
              {isAdding ? "Adding…" : addMode === "jobdiva" ? "Add Jobs" : "Add Job"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Delete campaign confirmation dialog */}
      <Dialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete Campaign</DialogTitle>
            <DialogDescription>
              Are you sure you want to archive this campaign? The child jobs and candidate history will
              remain accessible in your main jobs portfolio.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setDeleteConfirmOpen(false)} disabled={isDeletingCampaign}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDeleteCampaign}
              disabled={isDeletingCampaign}
              className="bg-red-600 hover:bg-red-700 text-white"
            >
              {isDeletingCampaign ? "Deleting..." : "Delete Campaign"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Remove job confirmation dialog */}
      <Dialog
        open={!!jobToRemove}
        onOpenChange={(open) => {
          if (!open) setJobToRemove(null);
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {jobToRemove?.action === "detach" ? "Detach Job from Campaign" : "Delete Requirement"}
            </DialogTitle>
            <DialogDescription>
              {jobToRemove?.action === "detach" ? (
                <>
                  Are you sure you want to detach <strong>{jobToRemove?.title}</strong> from this campaign?
                  It will return to being a standalone job in your jobs portfolio without losing candidate data.
                </>
              ) : (
                <>
                  Are you sure you want to permanently delete requirement <strong>{jobToRemove?.title}</strong>?
                  This action removes the job from monitoring.
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setJobToRemove(null)} disabled={isRemovingJob}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleConfirmRemoveJob}
              disabled={isRemovingJob}
              className="bg-red-600 hover:bg-red-700 text-white"
            >
              {isRemovingJob
                ? "Removing..."
                : jobToRemove?.action === "detach"
                ? "Detach Job"
                : "Delete Requirement"}
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

function LaunchBadge({ job }: { job: CampaignChildJob }) {
  if (job.pair_launched_at) {
    return (
      <Badge className="bg-emerald-100 text-emerald-700 gap-1 shrink-0">
        <Rocket className="h-3 w-3" />
        Launched
        {typeof job.candidates_launched === "number" && job.candidates_launched > 0 && (
          <span className="font-normal">· {job.candidates_launched}</span>
        )}
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="shrink-0">
      Not launched
    </Badge>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs font-medium text-slate-400 uppercase tracking-wide">{label}</div>
      <p className="text-sm text-slate-800 mt-0.5">{children}</p>
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
