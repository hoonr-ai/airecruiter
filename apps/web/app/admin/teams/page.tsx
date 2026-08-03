"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  UsersRound,
  Plus,
  Pencil,
  Trash2,
  ShieldAlert,
  ArrowLeft,
  AlertTriangle,
  RefreshCw,
  Crown,
  LayoutDashboard,
} from "lucide-react";
import { api } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export interface Team {
  id: string;
  name: string;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  lead_emails: string[];
  member_emails: string[];
}

interface TeamFormState {
  name: string;
  lead_emails: string;
  member_emails: string;
}

const EMPTY_FORM: TeamFormState = { name: "", lead_emails: "", member_emails: "" };

/** ISO datetime → "Feb 24, 2026"; null/invalid → "—". */
const formatDate = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};

export default function AdminTeamsPage() {
  const { isAdmin, isLoading: isRoleLoading, email, role } = useUserRole();
  const [teams, setTeams] = useState<Team[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Add/Edit modal state
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingTeam, setEditingTeam] = useState<Team | null>(null);
  const [form, setForm] = useState<TeamFormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  // Delete confirm state
  const [deletingTeam, setDeletingTeam] = useState<Team | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const fetchTeams = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await api.teams.list();
      if (res && res.status === "success" && res.data?.teams) {
        setTeams(res.data.teams as Team[]);
      } else {
        setError(res?.message || "Failed to load teams.");
      }
    } catch (err: any) {
      console.error("Error loading teams:", err);
      setError(err?.message || "Access denied or server error loading teams.");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isRoleLoading && isAdmin) {
      fetchTeams();
    }
  }, [isRoleLoading, isAdmin, fetchTeams]);

  const openAddModal = () => {
    setEditingTeam(null);
    setForm(EMPTY_FORM);
    setFormError(null);
    setIsModalOpen(true);
  };

  const openEditModal = (team: Team) => {
    setEditingTeam(team);
    setForm({
      name: team.name,
      lead_emails: team.lead_emails.join(", "),
      member_emails: team.member_emails.join(", "),
    });
    setFormError(null);
    setIsModalOpen(true);
  };

  const handleSave = async () => {
    if (!form.name.trim()) {
      setFormError("Team name is required.");
      return;
    }
    if (!form.lead_emails.trim()) {
      setFormError("Add at least one team lead email.");
      return;
    }
    setIsSaving(true);
    setFormError(null);
    try {
      const body = {
        name: form.name.trim(),
        lead_emails: form.lead_emails,
        member_emails: form.member_emails,
      };
      const res = editingTeam
        ? await api.teams.update(editingTeam.id, body)
        : await api.teams.create(body);
      if (res && res.status === "success") {
        setIsModalOpen(false);
        await fetchTeams();
      } else {
        setFormError(res?.message || "Failed to save the team.");
      }
    } catch (err: any) {
      // Surface the backend's validation detail (duplicate name, email
      // already in another team, malformed email...) instead of a generic
      // failure — the raw error looks like: 400 /api/v1/teams: {"detail": "..."}
      const raw = err?.message || "";
      const match = raw.match(/"detail"\s*:\s*"((?:[^"\\]|\\.)*)"/);
      setFormError(match ? match[1] : raw || "Failed to save the team.");
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!deletingTeam) return;
    setIsDeleting(true);
    try {
      await api.teams.remove(deletingTeam.id);
      setDeletingTeam(null);
      await fetchTeams();
    } catch (err: any) {
      console.error("Error deleting team:", err);
      setError(err?.message || "Failed to delete the team.");
      setDeletingTeam(null);
    } finally {
      setIsDeleting(false);
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
            You are signed in as <span className="font-semibold text-slate-800">{email || "a Recruiter"}</span> with the <span className="uppercase font-semibold text-[11px] bg-slate-100 px-2 py-0.5 rounded text-slate-700">{role.replace("_", " ")}</span> role. Team management is restricted to Administrators only.
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
    <div className="space-y-6 max-w-[1240px] mx-auto pb-10">
      {/* Page Header */}
      <div className="flex items-center justify-between mt-2">
        <div className="flex items-center gap-3">
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Teams</h1>
          <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
            {teams.length} {teams.length === 1 ? "Team" : "Teams"}
          </span>
        </div>

        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            onClick={fetchTeams}
            disabled={isLoading}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <RefreshCw className={`h-4 w-4 text-slate-500 ${isLoading ? "animate-spin text-primary" : ""}`} />
            Refresh
          </Button>
          <Button
            onClick={openAddModal}
            className="flex items-center gap-2 h-10 px-4 font-semibold text-[13px] rounded-lg shadow-sm"
          >
            <Plus className="h-4 w-4" />
            Add Team
          </Button>
        </div>
      </div>

      {error ? (
        <div className="flex items-center justify-between rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-red-800">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-red-600" />
            <span>{error}</span>
          </div>
          <button
            type="button"
            className="font-semibold underline decoration-red-400 underline-offset-2 hover:text-red-900"
            onClick={fetchTeams}
          >
            Retry
          </button>
        </div>
      ) : null}

      {/* Teams table */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd]">
          <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
            <UsersRound className="w-4 h-4 text-indigo-600" />
            Recruiting Teams
          </h2>
          <p className="text-[12px] text-slate-500 mt-0.5">
            Team leads see a team-scoped analytics dashboard and their team&apos;s jobs. Each person can belong to only one team.
          </p>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 font-bold text-slate-500 text-[12.5px]">
                <th className="py-3 px-6">Team</th>
                <th className="py-3 px-6">Team Leads</th>
                <th className="py-3 px-6">Members</th>
                <th className="py-3 px-6 text-center">Size</th>
                <th className="py-3 px-6">Created</th>
                <th className="py-3 px-6 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-[13px]">
              {isLoading ? (
                [1, 2, 3].map((i) => (
                  <tr key={i}>
                    <td className="py-4 px-6"><div className="h-4 w-32 bg-slate-100 animate-pulse rounded" /></td>
                    <td className="py-4 px-6"><div className="h-4 w-48 bg-slate-100 animate-pulse rounded" /></td>
                    <td className="py-4 px-6"><div className="h-4 w-56 bg-slate-100 animate-pulse rounded" /></td>
                    <td className="py-4 px-6 text-center"><div className="h-4 w-8 bg-slate-100 animate-pulse rounded mx-auto" /></td>
                    <td className="py-4 px-6"><div className="h-4 w-20 bg-slate-100 animate-pulse rounded" /></td>
                    <td className="py-4 px-6 text-right"><div className="h-8 w-24 bg-slate-100 animate-pulse rounded ml-auto" /></td>
                  </tr>
                ))
              ) : teams.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-14 text-center text-slate-400 text-[13px]">
                    <UsersRound className="w-8 h-8 mx-auto mb-3 text-slate-300" />
                    No teams yet. Click <span className="font-semibold text-slate-600">Add Team</span> to create the first one.
                  </td>
                </tr>
              ) : (
                teams.map((team) => (
                  <tr key={team.id} className="hover:bg-[#f6f8fb] transition-colors">
                    <td className="py-3.5 px-6">
                      <div className="font-semibold text-slate-800">{team.name}</div>
                      <Link
                        href={`/admin/analytics?team=${encodeURIComponent(team.id)}`}
                        className="inline-flex items-center gap-1 text-[12px] font-semibold text-primary hover:underline mt-0.5"
                      >
                        <LayoutDashboard className="w-3 h-3" />
                        View team analytics
                      </Link>
                    </td>
                    <td className="py-3.5 px-6">
                      <div className="flex flex-wrap gap-1.5 max-w-[280px]">
                        {team.lead_emails.map((lead) => (
                          <span
                            key={lead}
                            className="inline-flex items-center gap-1 rounded-full bg-indigo-50 border border-indigo-200 px-2 py-0.5 text-[11px] font-semibold text-indigo-700"
                          >
                            <Crown className="w-3 h-3" />
                            {lead}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="py-3.5 px-6">
                      <div className="flex flex-wrap gap-1.5 max-w-[320px]">
                        {team.member_emails.length === 0 ? (
                          <span className="text-slate-300">—</span>
                        ) : (
                          team.member_emails.map((member) => (
                            <span
                              key={member}
                              className="inline-flex items-center rounded-full bg-slate-100 border border-slate-200 px-2 py-0.5 text-[11px] font-medium text-slate-600"
                            >
                              {member}
                            </span>
                          ))
                        )}
                      </div>
                    </td>
                    <td className="py-3.5 px-6 text-center font-bold text-slate-800">
                      {team.lead_emails.length + team.member_emails.length}
                    </td>
                    <td className="py-3.5 px-6 text-slate-600 whitespace-nowrap">{formatDate(team.created_at)}</td>
                    <td className="py-3.5 px-6 text-right whitespace-nowrap">
                      <Button
                        variant="outline"
                        onClick={() => openEditModal(team)}
                        className="h-8 px-3 mr-2 border-slate-200 text-slate-600 font-semibold text-[12px] rounded-lg bg-white hover:bg-slate-50"
                      >
                        <Pencil className="w-3.5 h-3.5 mr-1" />
                        Edit
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => setDeletingTeam(team)}
                        className="h-8 px-3 border-rose-200 text-rose-600 font-semibold text-[12px] rounded-lg bg-white hover:bg-rose-50"
                      >
                        <Trash2 className="w-3.5 h-3.5 mr-1" />
                        Delete
                      </Button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Add / Edit Team modal */}
      <Dialog open={isModalOpen} onOpenChange={(open) => !isSaving && setIsModalOpen(open)}>
        <DialogContent className="sm:max-w-[520px] bg-white">
          <DialogHeader>
            <DialogTitle className="text-slate-900">
              {editingTeam ? `Edit Team — ${editingTeam.name}` : "Add Team"}
            </DialogTitle>
            <DialogDescription className="text-slate-500 text-[13px]">
              Assign one or more team leads and members using comma-separated email addresses. Each person can belong to only one team.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-1">
            <div className="space-y-1.5">
              <Label htmlFor="team-name" className="text-[13px] font-semibold text-slate-700">
                Team Name
              </Label>
              <Input
                id="team-name"
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                placeholder="e.g. East Coast Delivery"
                className="h-10 text-[13px]"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="team-leads" className="text-[13px] font-semibold text-slate-700">
                Team Lead Email(s)
              </Label>
              <Textarea
                id="team-leads"
                value={form.lead_emails}
                onChange={(e) => setForm((f) => ({ ...f, lead_emails: e.target.value }))}
                placeholder="lead1@example.com, lead2@example.com"
                className="min-h-[64px] text-[13px]"
              />
              <p className="text-[11.5px] text-slate-400">
                Leads see the analytics dashboard scoped to this team. Admins added here keep their full admin view.
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="team-members" className="text-[13px] font-semibold text-slate-700">
                Team Member Email(s)
              </Label>
              <Textarea
                id="team-members"
                value={form.member_emails}
                onChange={(e) => setForm((f) => ({ ...f, member_emails: e.target.value }))}
                placeholder="recruiter1@example.com, recruiter2@example.com"
                className="min-h-[88px] text-[13px]"
              />
              <p className="text-[11.5px] text-slate-400">
                Members keep working exactly as before — their jobs simply roll up into the team&apos;s analytics.
              </p>
            </div>

            {formError && (
              <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-[12.5px] text-red-800">
                <AlertTriangle className="h-4 w-4 text-red-600 mt-0.5 shrink-0" />
                <span>{formError}</span>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setIsModalOpen(false)}
              disabled={isSaving}
              className="h-10 px-4 border-slate-200 text-slate-600 font-semibold text-[13px] rounded-lg"
            >
              Cancel
            </Button>
            <Button
              onClick={handleSave}
              disabled={isSaving}
              className="h-10 px-4 font-semibold text-[13px] rounded-lg"
            >
              {isSaving ? "Saving..." : editingTeam ? "Save Changes" : "Create Team"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog open={!!deletingTeam} onOpenChange={(open) => !isDeleting && !open && setDeletingTeam(null)}>
        <DialogContent className="sm:max-w-[440px] bg-white">
          <DialogHeader>
            <DialogTitle className="text-slate-900">Delete Team</DialogTitle>
            <DialogDescription className="text-slate-500 text-[13px]">
              Delete <span className="font-semibold text-slate-800">{deletingTeam?.name}</span>? Its leads and
              members go back to standard recruiter access. Jobs and analytics data are not affected.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeletingTeam(null)}
              disabled={isDeleting}
              className="h-10 px-4 border-slate-200 text-slate-600 font-semibold text-[13px] rounded-lg"
            >
              Cancel
            </Button>
            <Button
              onClick={handleDelete}
              disabled={isDeleting}
              className="h-10 px-4 font-semibold text-[13px] rounded-lg bg-rose-600 hover:bg-rose-700 text-white"
            >
              {isDeleting ? "Deleting..." : "Delete Team"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
