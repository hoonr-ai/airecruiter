"use client";

// Lean rubric editor for the campaign template. Titles / skills / soft skills
// are editable rows (value + required + min years); the remaining rubric
// sections (education/domain/customer/other) are shown read-only so the
// recruiter can review what the AI extracted. The full rubric object is what
// gets saved as the campaign template — child jobs inherit it.

import { X, Plus } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Rubric, RubricRow } from "@/lib/campaigns";

type EditableSection = "titles" | "skills" | "soft_skills";
const EDITABLE: { key: EditableSection; label: string }[] = [
  { key: "titles", label: "Titles" },
  { key: "skills", label: "Skills" },
  { key: "soft_skills", label: "Soft Skills" },
];

const READONLY: { key: keyof Rubric; label: string }[] = [
  { key: "education", label: "Education & Certifications" },
  { key: "domain", label: "Industry / Domain" },
  { key: "customer_requirements", label: "Customer Requirements" },
  { key: "other_requirements", label: "Other Requirements" },
];

function summarizeRow(row: Record<string, unknown>): string {
  const r = row as Record<string, unknown>;
  const primary =
    (r.value as string) ||
    (r.degree as string) ||
    (r.requirement as string) ||
    (r.name as string) ||
    JSON.stringify(r);
  const field = r.field ? ` (${r.field as string})` : "";
  return `${primary}${field}`;
}

export function RubricEditor({
  rubric,
  onChange,
}: {
  rubric: Rubric;
  onChange: (r: Rubric) => void;
}) {
  const rows = (key: EditableSection): RubricRow[] => (rubric[key] as RubricRow[]) ?? [];

  const setRows = (key: EditableSection, next: RubricRow[]) =>
    onChange({ ...rubric, [key]: next });

  const updateRow = (key: EditableSection, idx: number, patch: Partial<RubricRow>) =>
    setRows(key, rows(key).map((r, i) => (i === idx ? { ...r, ...patch } : r)));

  const removeRow = (key: EditableSection, idx: number) =>
    setRows(key, rows(key).filter((_, i) => i !== idx));

  const addRow = (key: EditableSection) =>
    setRows(key, [...rows(key), { value: "", required: "Required", minYears: 0 }]);

  return (
    <div className="space-y-6">
      {EDITABLE.map(({ key, label }) => (
        <div key={key} className="space-y-2">
          <div className="flex items-center justify-between">
            <Label>{label}</Label>
            <button
              type="button"
              onClick={() => addRow(key)}
              className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              <Plus className="h-3 w-3" /> Add
            </button>
          </div>
          {rows(key).length === 0 ? (
            <p className="text-xs text-slate-400">None</p>
          ) : (
            <div className="space-y-2">
              {rows(key).map((row, idx) => (
                <div key={idx} className="flex items-center gap-2">
                  <Input
                    value={(row.value as string) ?? ""}
                    onChange={(e) => updateRow(key, idx, { value: e.target.value })}
                    placeholder={label.slice(0, -1)}
                    className="flex-1"
                  />
                  {key !== "soft_skills" && (
                    <Input
                      type="number"
                      min={0}
                      value={typeof row.minYears === "number" ? row.minYears : 0}
                      onChange={(e) => updateRow(key, idx, { minYears: Number(e.target.value) })}
                      className="w-20"
                      title="Min years"
                    />
                  )}
                  <Select
                    value={(row.required as string) ?? "Required"}
                    onValueChange={(v) => updateRow(key, idx, { required: v })}
                  >
                    <SelectTrigger className="w-32">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="Required">Required</SelectItem>
                      <SelectItem value="Preferred">Preferred</SelectItem>
                    </SelectContent>
                  </Select>
                  <button
                    type="button"
                    onClick={() => removeRow(key, idx)}
                    className="text-slate-400 hover:text-slate-600"
                    aria-label="Remove"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}

      {READONLY.map(({ key, label }) => {
        const items = (rubric[key] as Array<Record<string, unknown>>) ?? [];
        if (items.length === 0) return null;
        return (
          <div key={String(key)} className="space-y-1.5">
            <Label className="text-slate-500">{label}</Label>
            <div className="flex flex-wrap gap-1.5">
              {items.map((it, i) => (
                <span key={i} className="text-xs bg-slate-100 text-slate-600 rounded px-2 py-1">
                  {summarizeRow(it)}
                </span>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
