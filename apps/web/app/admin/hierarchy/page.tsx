"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  Network,
  Upload,
  Download,
  RefreshCw,
  ShieldAlert,
  ArrowLeft,
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  ChevronDown,
  Search,
  MailX,
  Info,
  FileSpreadsheet,
} from "lucide-react";
import {
  api,
  type OrgHierarchyLevel,
  type OrgHierarchyMember,
  type OrgHierarchyOverview,
  type OrgImportPreview,
} from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type OrgMember = OrgHierarchyMember;
type OrgLevel = OrgHierarchyLevel;
type OrgOverview = OrgHierarchyOverview;
type ImportPreview = OrgImportPreview;

// A Regional/Vertical Head the sheet only names (no row of their own): which
// level are they? The sheet does not say, so the admin chooses.
const HEAD_ROLE_OPTIONS = [
  { value: "delivery_director", label: "Delivery Director" },
  { value: "avp", label: "AVP" },
  { value: "delivery_manager", label: "Delivery Manager" },
];

const ROLE_STYLE: Record<string, string> = {
  avp: "bg-violet-50 text-violet-700 ring-violet-200",
  delivery_director: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  delivery_manager: "bg-sky-50 text-sky-700 ring-sky-200",
  resource_manager: "bg-teal-50 text-teal-700 ring-teal-200",
  recruiter: "bg-slate-100 text-slate-600 ring-slate-200",
};

const NEEDS_EMAIL_PREVIEW_ROWS = 12;

/** "400 /path: {"detail": ...}" → the backend's own message. */
function errorMessage(err: unknown, fallback: string): string {
  const raw = err instanceof Error ? err.message : "";
  const brace = raw.indexOf("{");
  if (brace >= 0) {
    try {
      const detail: unknown = (JSON.parse(raw.slice(brace)) as { detail?: unknown }).detail;
      if (typeof detail === "string") return detail;
      if (detail && typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string") {
        return (detail as { message: string }).message;
      }
    } catch {
      // fall through to the raw text
    }
  }
  return raw || fallback;
}

/** The server's timestamps are UTC without a zone suffix. */
const formatDate = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "2-digit",
    day: "2-digit",
    year: "numeric",
  }).format(d);
};

function RoleBadge({ role, label }: { role: string; label: string }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ring-inset ${
        ROLE_STYLE[role] ?? ROLE_STYLE.recruiter
      }`}
    >
      {label}
    </span>
  );
}

function sortMembers(a: OrgMember, b: OrgMember, rank: Record<string, number>): number {
  return (
    (rank[b.role] ?? 0) - (rank[a.role] ?? 0) ||
    b.total_reports - a.total_reports ||
    a.name.localeCompare(b.name)
  );
}

function TreeNode({
  member,
  depth,
  childrenOf,
  expanded,
  onToggle,
  visible,
}: {
  member: OrgMember;
  depth: number;
  childrenOf: Map<number, OrgMember[]>;
  expanded: Set<number>;
  onToggle: (id: number) => void;
  visible: Set<number> | null;
}) {
  const kids = (childrenOf.get(member.id) ?? []).filter((k) => !visible || visible.has(k.id));
  const isOpen = expanded.has(member.id);
  return (
    <>
      <div
        className="flex items-center gap-2 border-b border-slate-100 py-1.5 pr-4 text-[13px] hover:bg-slate-50"
        style={{ paddingLeft: 12 + depth * 22 }}
      >
        {kids.length > 0 ? (
          <button
            type="button"
            onClick={() => onToggle(member.id)}
            className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-slate-400 hover:bg-slate-200 hover:text-slate-700"
            aria-label={isOpen ? `Collapse ${member.name}` : `Expand ${member.name}`}
          >
            {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </button>
        ) : (
          <span className="h-5 w-5 shrink-0" />
        )}
        <span className="min-w-0 flex-1 truncate">
          <span className="font-semibold text-slate-800">{member.name}</span>
          {member.email ? (
            <span className="ml-2 text-slate-400">{member.email}</span>
          ) : (
            <span
              className="ml-2 inline-flex items-center gap-1 rounded bg-amber-50 px-1.5 py-px text-[11px] font-semibold text-amber-700"
              title="In the tree, but has no email yet — they cannot sign in with a scope until one is added."
            >
              <MailX className="h-3 w-3" />
              no email yet
            </span>
          )}
        </span>
        {member.vertical && (
          <span className="hidden max-w-[200px] truncate text-[12px] text-slate-400 lg:inline">{member.vertical}</span>
        )}
        {member.total_reports > 0 && (
          <span className="shrink-0 text-[12px] text-slate-400">{member.total_reports} under</span>
        )}
        <RoleBadge role={member.role} label={member.role_label} />
      </div>
      {isOpen &&
        kids.map((kid) => (
          <TreeNode
            key={kid.id}
            member={kid}
            depth={depth + 1}
            childrenOf={childrenOf}
            expanded={expanded}
            onToggle={onToggle}
            visible={visible}
          />
        ))}
    </>
  );
}

function PreviewPanel({ preview, levels }: { preview: ImportPreview; levels: OrgLevel[] }) {
  const [showAllNeeds, setShowAllNeeds] = useState(false);
  const { summary } = preview;
  const needs = showAllNeeds ? summary.needs_email : summary.needs_email.slice(0, NEEDS_EMAIL_PREVIEW_ROWS);
  const errors = preview.issues.filter((i) => i.severity === "error");
  const warnings = preview.issues.filter((i) => i.severity === "warning");
  const infos = preview.issues.filter((i) => i.severity === "info");
  const coverage = preview.coverage;
  const lowCoverage = coverage && coverage.people_with_email > 0 && coverage.assigned_to_a_job / coverage.people_with_email < 0.5;

  return (
    <div className="space-y-4">
      {errors.length > 0 && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-red-800">
          <div className="mb-1 flex items-center gap-2 font-semibold">
            <AlertTriangle className="h-4 w-4 text-red-600" />
            This file cannot be imported
          </div>
          <ul className="list-disc space-y-1 pl-6">
            {errors.map((i, n) => (
              <li key={n}>{i.message}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">People</div>
          <div className="text-[22px] font-bold text-slate-900">{summary.people}</div>
          <div className="text-[12px] text-slate-400">from {summary.rows} rows</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">With email</div>
          <div className="text-[22px] font-bold text-slate-900">{summary.with_email}</div>
          <div className="text-[12px] text-slate-400">can sign in with a scope</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Need an email</div>
          <div className={`text-[22px] font-bold ${summary.without_email ? "text-amber-600" : "text-slate-900"}`}>
            {summary.without_email}
          </div>
          <div className="text-[12px] text-slate-400">named, no email in the sheet</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Changes</div>
          {preview.diff ? (
            <>
              <div className="text-[22px] font-bold text-slate-900">
                +{preview.diff.emails_added} / −{preview.diff.emails_removed}
              </div>
              <div className="text-[12px] text-slate-400">emails vs today&apos;s tree</div>
            </>
          ) : (
            <div className="text-[13px] text-slate-500">No tree yet — first import</div>
          )}
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {levels.map((level) => (
          <span key={level.role} className="inline-flex items-center gap-1.5 text-[12px] text-slate-500">
            <RoleBadge role={level.role} label={level.label} />
            <span className="font-semibold text-slate-700">{summary.by_role[level.role] ?? 0}</span>
          </span>
        ))}
      </div>

      {coverage && (
        <div
          className={`flex items-start gap-2 rounded-lg border px-4 py-3 text-[13px] ${
            lowCoverage ? "border-amber-200 bg-amber-50 text-amber-900" : "border-slate-200 bg-slate-50 text-slate-700"
          }`}
        >
          {lowCoverage ? (
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          ) : (
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
          )}
          <span>
            <span className="font-semibold">{coverage.assigned_to_a_job}</span> of{" "}
            <span className="font-semibold">{coverage.people_with_email}</span> people with an email are assigned to at
            least one job today.
            {lowCoverage && " That is low — check these are the same emails JobDiva puts on jobs, or managers will see little."}
          </span>
        </div>
      )}

      {summary.top_level.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-white px-4 py-3 text-[13px]">
          <div className="mb-1.5 font-semibold text-slate-800">Nobody above these people</div>
          <div className="mb-2 text-[12px] text-slate-500">
            They see everyone beneath them and no one else. To place them under a Delivery Manager, Delivery Director or
            AVP, add that person to the sheet and set these people&apos;s Reporting Manager to them.
          </div>
          <div className="flex flex-wrap gap-2">
            {summary.top_level.map((t) => (
              <span key={`${t.name}-${t.email ?? ""}`} className="inline-flex items-center gap-1.5 rounded-md bg-slate-50 px-2 py-1 text-[12px]">
                <span className="font-semibold text-slate-700">{t.name}</span>
                <RoleBadge role={t.role} label={t.role_label} />
                <span className="text-slate-400">{t.total_reports} under</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {summary.needs_email.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <div className="border-b border-slate-200 bg-[#fcfdfd] px-4 py-3">
            <div className="flex items-center gap-2 text-[13px] font-semibold text-slate-800">
              <MailX className="h-4 w-4 text-amber-600" />
              {summary.needs_email.length} people the sheet names but gives no email for
            </div>
            <div className="mt-0.5 text-[12px] text-slate-500">
              They stay in the tree so their reports are grouped, but they see nothing until you add their email. Import,
              then use <span className="font-semibold">Download CSV</span> to get a file with a blank Email cell for each,
              fill them in, and upload it again.
            </div>
          </div>
          <table className="w-full text-left text-[13px]">
            <thead className="bg-slate-50 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-2">Name</th>
                <th className="px-4 py-2">Level</th>
                <th className="px-4 py-2">Vertical</th>
                <th className="px-4 py-2 text-right">Direct</th>
                <th className="px-4 py-2 text-right">Total under</th>
              </tr>
            </thead>
            <tbody>
              {needs.map((n) => (
                <tr key={`${n.name}-${n.role}`} className="border-t border-slate-100">
                  <td className="px-4 py-1.5 font-semibold text-slate-800">{n.name}</td>
                  <td className="px-4 py-1.5">
                    <RoleBadge role={n.role} label={n.role_label} />
                  </td>
                  <td className="px-4 py-1.5 text-slate-500">{n.vertical || "—"}</td>
                  <td className="px-4 py-1.5 text-right text-slate-600">{n.direct_reports}</td>
                  <td className="px-4 py-1.5 text-right text-slate-600">{n.total_reports}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {summary.needs_email.length > NEEDS_EMAIL_PREVIEW_ROWS && (
            <button
              type="button"
              onClick={() => setShowAllNeeds((v) => !v)}
              className="w-full border-t border-slate-100 py-2 text-[12.5px] font-semibold text-primary hover:bg-slate-50"
            >
              {showAllNeeds ? "Show fewer" : `Show all ${summary.needs_email.length}`}
            </button>
          )}
        </div>
      )}

      {summary.titles_treated_as_recruiter.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-white px-4 py-3 text-[13px]">
          <div className="mb-1 font-semibold text-slate-800">Treated as Recruiters</div>
          <div className="mb-2 text-[12px] text-slate-500">
            These Positions values are not a management level, so these people are Recruiters (they see only their own
            jobs). If any should be higher, write the level — Resource Manager, Delivery Manager, Delivery Director or
            AVP — in the Positions column.
          </div>
          <div className="flex flex-wrap gap-2">
            {summary.titles_treated_as_recruiter.map((t) => (
              <span key={t.title} className="rounded-md bg-slate-50 px-2 py-1 text-[12px] text-slate-600">
                {t.title} <span className="font-semibold text-slate-800">×{t.count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {(warnings.length > 0 || infos.length > 0) && (
        <div className="rounded-lg border border-slate-200 bg-white px-4 py-3 text-[13px]">
          <div className="mb-1 font-semibold text-slate-800">Notes ({warnings.length + infos.length})</div>
          <ul className="max-h-48 space-y-1 overflow-y-auto pr-2 text-slate-600">
            {[...warnings, ...infos].map((i, n) => (
              <li key={n} className="flex gap-2">
                {i.severity === "warning" ? (
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
                ) : (
                  <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />
                )}
                <span>
                  {i.row ? <span className="font-semibold text-slate-500">Row {i.row}: </span> : null}
                  {i.message}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {preview.diff && preview.diff.emails_removed > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-[13px] text-amber-900">
          <span className="font-semibold">{preview.diff.emails_removed}</span> people in today&apos;s tree are not in this
          file and will be removed
          {preview.diff.removed_sample.length > 0 && (
            <>
              {" "}
              (e.g. {preview.diff.removed_sample.slice(0, 5).join(", ")}
              {preview.diff.emails_removed > 5 ? ", …" : ""})
            </>
          )}
          .
        </div>
      )}
    </div>
  );
}

export default function AdminHierarchyPage() {
  const { isAdmin, isLoading: isRoleLoading, email, role, orgRoleLabel } = useUserRole();
  const [overview, setOverview] = useState<OrgOverview | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Import flow: file → preview (nothing changes) → confirm → apply.
  const fileInput = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [csvText, setCsvText] = useState<string>("");
  const [headRole, setHeadRole] = useState<string>("delivery_director");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [isApplying, setIsApplying] = useState(false);
  const [isExporting, setIsExporting] = useState(false);

  // Tree view
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const fetchOverview = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await api.orgHierarchy.get();
      if (res && res.status === "success" && res.data) {
        setOverview(res.data);
      } else {
        setError(res?.message || "Failed to load the hierarchy.");
      }
    } catch (err) {
      console.error("Error loading org hierarchy:", err);
      setError(errorMessage(err, "Access denied or server error loading the hierarchy."));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isRoleLoading && isAdmin) fetchOverview();
  }, [isRoleLoading, isAdmin, fetchOverview]);

  const rank = useMemo(() => {
    const out: Record<string, number> = {};
    for (const level of overview?.levels ?? []) out[level.role] = level.rank;
    return out;
  }, [overview]);

  const { roots, childrenOf, byId } = useMemo(() => {
    const members = overview?.members ?? [];
    const byId = new Map<number, OrgMember>(members.map((m) => [m.id, m]));
    const childrenOf = new Map<number, OrgMember[]>();
    const roots: OrgMember[] = [];
    for (const m of members) {
      if (m.reports_to_id !== null && byId.has(m.reports_to_id)) {
        const list = childrenOf.get(m.reports_to_id) ?? [];
        list.push(m);
        childrenOf.set(m.reports_to_id, list);
      } else {
        roots.push(m);
      }
    }
    const order = (a: OrgMember, b: OrgMember) => sortMembers(a, b, rank);
    roots.sort(order);
    childrenOf.forEach((list) => list.sort(order));
    return { roots, childrenOf, byId };
  }, [overview, rank]);

  // Search: show only matches and their ancestors, all opened.
  const needle = query.trim().toLowerCase();
  const visible = useMemo(() => {
    if (!needle) return null;
    const keep = new Set<number>();
    for (const m of overview?.members ?? []) {
      const hay = `${m.name} ${m.email ?? ""} ${m.vertical} ${m.role_label}`.toLowerCase();
      if (!hay.includes(needle)) continue;
      let cursor: OrgMember | undefined = m;
      while (cursor && !keep.has(cursor.id)) {
        keep.add(cursor.id);
        cursor = cursor.reports_to_id !== null ? byId.get(cursor.reports_to_id) : undefined;
      }
    }
    return keep;
  }, [needle, overview, byId]);

  const effectiveExpanded = useMemo(() => (visible ? new Set(visible) : expanded), [visible, expanded]);

  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const expandAll = () => setExpanded(new Set((overview?.members ?? []).filter((m) => m.direct_reports > 0).map((m) => m.id)));
  const collapseAll = () => setExpanded(new Set());

  const shownRoots = visible ? roots.filter((r) => visible.has(r.id)) : roots;

  const onFilePicked = async (file: File | null) => {
    setPreview(null);
    setImportError(null);
    setNotice(null);
    if (!file) {
      setFileName(null);
      setCsvText("");
      return;
    }
    try {
      const text = await file.text();
      setFileName(file.name);
      setCsvText(text);
    } catch (err) {
      setFileName(null);
      setCsvText("");
      setImportError(errorMessage(err, "Could not read that file."));
    }
  };

  const runPreview = async () => {
    if (!csvText) return;
    setIsPreviewing(true);
    setImportError(null);
    setNotice(null);
    try {
      const res = await api.orgHierarchy.import({ csv: csvText, dry_run: true, head_role: headRole });
      if (res && res.status === "success" && res.data) {
        setPreview(res.data);
      } else {
        setImportError(res?.message || "Could not preview the file.");
      }
    } catch (err) {
      setPreview(null);
      setImportError(errorMessage(err, "Could not preview the file."));
    } finally {
      setIsPreviewing(false);
    }
  };

  const applyImport = async () => {
    if (!csvText || !preview || preview.blocking) return;
    setIsApplying(true);
    setImportError(null);
    try {
      const res = await api.orgHierarchy.import({ csv: csvText, dry_run: false, head_role: headRole });
      if (res && res.status === "success" && res.data?.applied) {
        setConfirmOpen(false);
        setNotice(
          `Hierarchy replaced: ${res.data.summary.people} people (${res.data.summary.without_email} still need an email).`,
        );
        setPreview(null);
        setCsvText("");
        setFileName(null);
        if (fileInput.current) fileInput.current.value = "";
        await fetchOverview();
      } else {
        setConfirmOpen(false);
        setImportError(res?.message || "The import did not apply.");
      }
    } catch (err) {
      setConfirmOpen(false);
      setImportError(errorMessage(err, "The import failed; nothing was changed."));
    } finally {
      setIsApplying(false);
    }
  };

  const downloadCsv = async () => {
    setIsExporting(true);
    try {
      const res = await api.orgHierarchy.export();
      if (res && res.status === "success" && res.data?.csv !== undefined) {
        const blob = new Blob([res.data.csv], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = res.data.filename || "org-hierarchy.csv";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      } else {
        setError(res?.message || "Could not export the hierarchy.");
      }
    } catch (err) {
      setError(errorMessage(err, "Could not export the hierarchy."));
    } finally {
      setIsExporting(false);
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
        <Card className="w-full max-w-md rounded-xl border-slate-200 bg-white p-8 text-center shadow-sm">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full border border-red-100 bg-red-50 text-red-600">
            <ShieldAlert className="h-6 w-6" />
          </div>
          <h1 className="mb-2 text-[20px] font-bold text-slate-900">Access Restricted</h1>
          <p className="mb-6 text-[13px] leading-relaxed text-slate-500">
            You are signed in as <span className="font-semibold text-slate-800">{email || "a Recruiter"}</span> with the{" "}
            <span className="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-semibold uppercase text-slate-700">
              {orgRoleLabel ?? role.replace("_", " ")}
            </span>{" "}
            role. The org hierarchy is restricted to Administrators only.
          </p>
          <Link href="/">
            <Button className="h-10 w-full gap-2 rounded-lg bg-slate-900 text-[13px] font-semibold text-white hover:bg-slate-800">
              <ArrowLeft className="h-4 w-4" />
              Return to Jobs Dashboard
            </Button>
          </Link>
        </Card>
      </div>
    );
  }

  const counts = overview?.counts;

  return (
    <div className="mx-auto max-w-[1240px] space-y-6 pb-10">
      <div className="mt-2 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-[28px] font-bold tracking-tight text-slate-900">Org Hierarchy</h1>
          {counts && (
            <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
              {counts.total} {counts.total === 1 ? "person" : "people"}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            onClick={fetchOverview}
            disabled={isLoading}
            className="flex h-10 items-center gap-2 rounded-lg border-slate-200 bg-white px-4 text-[13px] font-semibold text-slate-700 shadow-sm hover:bg-slate-50"
          >
            <RefreshCw className={`h-4 w-4 text-slate-500 ${isLoading ? "animate-spin text-primary" : ""}`} />
            Refresh
          </Button>
          <Button
            variant="outline"
            onClick={downloadCsv}
            disabled={isExporting || !counts?.total}
            className="flex h-10 items-center gap-2 rounded-lg border-slate-200 bg-white px-4 text-[13px] font-semibold text-slate-700 shadow-sm hover:bg-slate-50"
          >
            <Download className="h-4 w-4 text-slate-500" />
            {isExporting ? "Preparing..." : "Download CSV"}
          </Button>
        </div>
      </div>

      <p className="text-[13px] leading-relaxed text-slate-500">
        Recruiter → Resource Manager → Delivery Manager → Delivery Director → AVP. Everyone sees their own jobs and, from
        Resource Manager up, the Dashboard, Analytics and Launch Report scoped to everyone beneath them — never sideways
        or upwards. The sheet below is the source of truth: each upload replaces the whole tree.
      </p>

      {error ? (
        <div className="flex items-center justify-between rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-red-800">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-red-600" />
            <span>{error}</span>
          </div>
          <button
            type="button"
            className="font-semibold underline decoration-red-400 underline-offset-2 hover:text-red-900"
            onClick={fetchOverview}
          >
            Retry
          </button>
        </div>
      ) : null}

      {notice ? (
        <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-[13px] text-emerald-800">
          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
          <span>{notice}</span>
        </div>
      ) : null}

      {/* Import */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="flex items-center justify-between border-b border-slate-200 bg-[#fcfdfd] px-6 py-4">
          <div className="flex items-center gap-2 text-[15px] font-semibold text-slate-900">
            <FileSpreadsheet className="h-4 w-4 text-slate-500" />
            Upload the mapping sheet
          </div>
          {overview?.last_import && (
            <span className="text-[12px] text-slate-400">
              Last replaced {formatDate(overview.last_import.imported_at)}
              {overview.last_import.imported_by ? ` by ${overview.last_import.imported_by}` : ""}
            </span>
          )}
        </div>
        <div className="space-y-4 px-6 py-5">
          <p className="text-[13px] leading-relaxed text-slate-500">
            Save the sheet as CSV (Excel: File → Save As → CSV). Columns: <span className="font-semibold">Email. ID, Name,
            Positions, Reporting Manager, Regional/Vertical Structure, Regional/Vertical Head</span>. Previewing changes
            nothing — you confirm before anything is replaced.
          </p>
          <div className="flex flex-wrap items-end gap-4">
            <div>
              <label className="mb-1 block text-[12px] font-semibold text-slate-600" htmlFor="org-file">
                Sheet (.csv)
              </label>
              <input
                id="org-file"
                ref={fileInput}
                type="file"
                accept=".csv,.tsv,.txt,text/csv,text/plain"
                onChange={(e) => onFilePicked(e.target.files?.[0] ?? null)}
                className="block w-[320px] cursor-pointer rounded-lg border border-slate-200 bg-white text-[13px] text-slate-600 file:mr-3 file:cursor-pointer file:border-0 file:bg-slate-100 file:px-3 file:py-2 file:text-[13px] file:font-semibold file:text-slate-700 hover:file:bg-slate-200"
              />
            </div>
            <div>
              <label className="mb-1 block text-[12px] font-semibold text-slate-600" htmlFor="org-head-role">
                A Regional/Vertical Head with no row of their own is a
              </label>
              <select
                id="org-head-role"
                value={headRole}
                onChange={(e) => {
                  setHeadRole(e.target.value);
                  setPreview(null);
                }}
                className="h-10 w-[220px] rounded-lg border border-slate-200 bg-white px-3 text-[13px] text-slate-700"
              >
                {HEAD_ROLE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <Button
              onClick={runPreview}
              disabled={!csvText || isPreviewing}
              className="flex h-10 items-center gap-2 rounded-lg px-4 text-[13px] font-semibold shadow-sm"
            >
              <Upload className="h-4 w-4" />
              {isPreviewing ? "Reading..." : "Preview"}
            </Button>
          </div>
          {fileName && <div className="text-[12px] text-slate-400">Selected: {fileName}</div>}

          {importError ? (
            <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-red-800">
              <AlertTriangle className="h-4 w-4 shrink-0 text-red-600" />
              <span>{importError}</span>
            </div>
          ) : null}

          {preview && overview ? (
            <>
              <PreviewPanel preview={preview} levels={overview.levels} />
              <div className="flex items-center justify-end gap-3 border-t border-slate-100 pt-4">
                <span className="text-[12px] text-slate-400">
                  {preview.blocking ? "Fix the file to continue." : "Nothing has changed yet."}
                </span>
                <Button
                  onClick={() => setConfirmOpen(true)}
                  disabled={preview.blocking || isApplying}
                  className="h-10 rounded-lg px-4 text-[13px] font-semibold shadow-sm"
                >
                  Replace hierarchy…
                </Button>
              </div>
            </>
          ) : null}
        </div>
      </div>

      {/* Summary + tree */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-[#fcfdfd] px-6 py-4">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2 text-[15px] font-semibold text-slate-900">
              <Network className="h-4 w-4 text-slate-500" />
              Current hierarchy
            </div>
            {overview &&
              overview.levels
                .slice()
                .reverse()
                .map((level) => (
                  <span key={level.role} className="inline-flex items-center gap-1 text-[12px] text-slate-500">
                    <RoleBadge role={level.role} label={level.label} />
                    <span className="font-semibold text-slate-700">{counts?.by_role[level.role] ?? 0}</span>
                  </span>
                ))}
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search name, email or vertical"
                className="h-9 w-[260px] rounded-lg border-slate-200 pl-8 text-[13px]"
              />
            </div>
            <Button variant="outline" onClick={expandAll} className="h-9 rounded-lg px-3 text-[12.5px] font-semibold">
              Expand all
            </Button>
            <Button variant="outline" onClick={collapseAll} className="h-9 rounded-lg px-3 text-[12.5px] font-semibold">
              Collapse all
            </Button>
          </div>
        </div>

        {counts && counts.without_email > 0 && (
          <div className="flex items-start gap-2 border-b border-amber-100 bg-amber-50 px-6 py-3 text-[13px] text-amber-900">
            <MailX className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <span>
              <span className="font-semibold">{counts.without_email}</span> people are in the tree without an email. They
              see nothing until you add it: download the CSV, fill in their Email cell, and upload it again.
            </span>
          </div>
        )}

        {isLoading && !overview ? (
          <div className="px-6 py-10 text-center text-[13px] text-slate-400">Loading…</div>
        ) : !overview || overview.members.length === 0 ? (
          <div className="px-6 py-10 text-center text-[13px] text-slate-500">
            No hierarchy yet. Upload the mapping sheet above to create it — until then everyone sees only their own jobs
            (plus their Teams).
          </div>
        ) : shownRoots.length === 0 ? (
          <div className="px-6 py-10 text-center text-[13px] text-slate-500">Nobody matches “{query}”.</div>
        ) : (
          <div>
            {shownRoots.map((root) => (
              <TreeNode
                key={root.id}
                member={root}
                depth={0}
                childrenOf={childrenOf}
                expanded={effectiveExpanded}
                onToggle={toggle}
                visible={visible}
              />
            ))}
          </div>
        )}
      </div>

      <Dialog open={confirmOpen} onOpenChange={(open) => !isApplying && setConfirmOpen(open)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Replace the org hierarchy?</DialogTitle>
            <DialogDescription>
              {preview
                ? `This replaces the whole hierarchy with ${preview.summary.people} people from ${fileName ?? "the file"}. `
                : ""}
              Who sees whose jobs and analytics changes immediately, for everyone. If the import fails, nothing is
              changed.
            </DialogDescription>
          </DialogHeader>
          {preview?.diff && preview.diff.emails_removed > 0 && (
            <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[13px] text-amber-900">
              {preview.diff.emails_removed} people in today&apos;s tree are not in this file and will be removed.
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)} disabled={isApplying}>
              Cancel
            </Button>
            <Button onClick={applyImport} disabled={isApplying}>
              {isApplying ? "Replacing..." : "Replace hierarchy"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
