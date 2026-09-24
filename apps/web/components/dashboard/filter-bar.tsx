"use client";

import { Briefcase, Building2, CircleDot, Flag, Layers, RotateCcw, ShieldCheck, UserCog } from "lucide-react";
import type { DashboardFilterState, DashboardOptions } from "./types";
import { EMPTY_FILTERS } from "./types";

type SelectProps = {
  id: string;
  label: string;
  icon: typeof Layers;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  allLabel: string;
  disabled?: boolean;
  hint?: string;
};

function FilterSelect({ id, label, icon: Icon, value, onChange, options, allLabel, disabled, hint }: SelectProps) {
  return (
    <label htmlFor={id} className="flex min-w-0 flex-col gap-1" title={hint}>
      <span className="flex items-center gap-1 text-[10.5px] font-bold uppercase tracking-wider text-slate-500">
        <Icon className="h-3 w-3" />
        {label}
      </span>
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 rounded-lg border border-slate-200 bg-white px-2.5 text-[13px] font-medium text-slate-700 shadow-sm outline-none focus:border-primary disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400"
      >
        <option value="">{allLabel}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function FilterBar({
  options,
  filters,
  onChange,
  isAdmin,
  teamId,
  onTeamChange,
  pinnedTeamName,
  reqOnlyDisabled,
}: {
  options: DashboardOptions | null;
  filters: DashboardFilterState;
  onChange: (next: DashboardFilterState) => void;
  isAdmin: boolean;
  teamId: string | null;
  onTeamChange: (teamId: string | null) => void;
  pinnedTeamName: string | null;
  /** Productivity covers all reqs, so the PAIR-only filters don't apply there. */
  reqOnlyDisabled: boolean;
}) {
  const set = (key: keyof DashboardFilterState) => (value: string) => onChange({ ...filters, [key]: value });
  const list = (values: string[] | undefined) => (values ?? []).map((v) => ({ value: v, label: v }));
  const teams = options?.teams ?? [];
  const reqOnlyHint = reqOnlyDisabled ? "Productivity covers every requirement, not only PAIR ones." : undefined;
  const dirty =
    Object.entries(filters).some(([key, value]) => value !== EMPTY_FILTERS[key as keyof DashboardFilterState]) ||
    (isAdmin && teamId !== null);
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <FilterSelect
          id="filter-vertical"
          label="Primary Vertical"
          icon={Layers}
          value={filters.vertical}
          onChange={set("vertical")}
          options={list(options?.verticals)}
          allLabel="All Verticals"
          hint="JobDiva division of the requirement"
        />
        {isAdmin ? (
          <FilterSelect
            id="filter-manager"
            label="Recruiting Manager"
            icon={UserCog}
            value={teamId ?? ""}
            onChange={(value) => onTeamChange(value || null)}
            options={teams.map((t) => ({
              value: t.id,
              label: t.lead_emails.length ? `${t.name} (${t.lead_emails.join(", ")})` : t.name,
            }))}
            allLabel="All Managers"
            hint="A team on the Teams page: its lead is the Recruiting Manager"
          />
        ) : (
          <div className="flex min-w-0 flex-col gap-1">
            <span className="flex items-center gap-1 text-[10.5px] font-bold uppercase tracking-wider text-slate-500">
              <UserCog className="h-3 w-3" />
              Recruiting Manager
            </span>
            <div className="flex h-9 items-center rounded-lg border border-slate-200 bg-slate-50 px-2.5 text-[13px] font-semibold text-slate-700">
              {pinnedTeamName || "Your team"}
            </div>
          </div>
        )}
        <FilterSelect
          id="filter-job"
          label="Job"
          icon={Briefcase}
          value={filters.job}
          onChange={set("job")}
          options={(options?.jobs ?? []).map((j) => ({ value: j.job_id, label: `${j.ref} · ${j.title}` }))}
          allLabel="All Jobs"
          disabled={reqOnlyDisabled}
          hint={reqOnlyHint}
        />
        <FilterSelect
          id="filter-priority"
          label="Priority"
          icon={Flag}
          value={filters.priority}
          onChange={set("priority")}
          options={list(options?.priorities)}
          allLabel="All Priorities"
        />
        <FilterSelect
          id="filter-client"
          label="Client"
          icon={Building2}
          value={filters.client}
          onChange={set("client")}
          options={list(options?.clients)}
          allLabel="All Clients"
        />
        <FilterSelect
          id="filter-jd-status"
          label="JD Status"
          icon={CircleDot}
          value={filters.jdStatus}
          onChange={set("jdStatus")}
          options={list(options?.jd_statuses)}
          allLabel="All"
          disabled={reqOnlyDisabled}
          hint={reqOnlyHint}
        />
        <FilterSelect
          id="filter-pair-status"
          label="PAIR Status"
          icon={ShieldCheck}
          value={filters.pairStatus}
          onChange={set("pairStatus")}
          options={list(options?.pair_statuses)}
          allLabel="All"
          disabled={reqOnlyDisabled}
          hint={reqOnlyHint}
        />
        <button
          type="button"
          onClick={() => {
            onChange(EMPTY_FILTERS);
            if (isAdmin) onTeamChange(null);
          }}
          disabled={!dirty}
          className="inline-flex h-9 w-fit items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 text-[13px] font-semibold text-slate-600 shadow-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RotateCcw className="h-3.5 w-3.5" />
          Reset
        </button>
      </div>
    </div>
  );
}
