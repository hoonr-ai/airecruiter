"use client";

// Read-only view of the campaign template (JD, rubric, screening questions).
// Matches the visual style of the campaign wizard (steps 2 & 3) — same card
// containers, label typography, pill tags, and question row layout.

import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { Label } from "@/components/ui/label";
import { Campaign } from "@/lib/campaigns";

function summarizeRow(row: Record<string, unknown>): string {
  const primary =
    (row.value as string) ||
    (row.degree as string) ||
    (row.requirement as string) ||
    (row.name as string) ||
    JSON.stringify(row);
  const field = row.field ? ` (${row.field as string})` : "";
  const years = typeof row.minYears === "number" && row.minYears > 0 ? ` · ${row.minYears}+ yrs` : "";
  const req = row.required === "Required" ? " · Required" : row.required === "Preferred" ? " · Preferred" : "";
  return `${primary}${field}${years}${req}`;
}

export function CampaignTemplateCard({
  campaign,
}: {
  campaign?: Partial<Campaign> | null;
}) {
  const [open, setOpen] = useState(false);
  const [showJD, setShowJD] = useState(false);

  if (!campaign) return null;

  const rubric = (campaign.template_rubric as Record<string, unknown[]>) || {};
  const titles = (rubric.titles as Record<string, unknown>[]) ?? [];
  const skills = (rubric.skills as Record<string, unknown>[]) ?? [];
  const softSkills = (rubric.soft_skills as Record<string, unknown>[]) ?? [];
  const education = (rubric.education as Record<string, unknown>[]) ?? [];
  const domain = (rubric.domain as Record<string, unknown>[]) ?? [];
  const customerReqs = (rubric.customer_requirements as Record<string, unknown>[]) ?? [];
  const otherReqs = (rubric.other_requirements as Record<string, unknown>[]) ?? [];
  const questions = (campaign.template_screen_questions as Record<string, unknown>[]) ?? [];

  const hasData =
    Boolean(campaign.template_enhanced_title) ||
    Boolean(campaign.template_ai_description) ||
    titles.length > 0 ||
    skills.length > 0 ||
    softSkills.length > 0 ||
    questions.length > 0;

  if (!hasData) return null;

  const rubricSections = [
    { label: "Titles", items: titles },
    { label: "Skills", items: skills },
    { label: "Soft Skills", items: softSkills },
    { label: "Education & Certifications", items: education },
    { label: "Industry / Domain", items: domain },
    { label: "Customer Requirements", items: customerReqs },
    { label: "Other Requirements", items: otherReqs },
  ].filter((s) => s.items.length > 0);

  return (
    <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
      {/* Collapsible header */}
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-6 py-4 text-left hover:bg-slate-50 transition-colors"
      >
        <div>
          <Label className="cursor-pointer text-slate-800 text-sm font-semibold">
            Campaign Template
          </Label>
          <p className="text-xs text-slate-400 mt-0.5">
            {[
              titles.length > 0 && `${titles.length} title${titles.length !== 1 ? "s" : ""}`,
              skills.length > 0 && `${skills.length} skill${skills.length !== 1 ? "s" : ""}`,
              softSkills.length > 0 && `${softSkills.length} soft skill${softSkills.length !== 1 ? "s" : ""}`,
              customerReqs.length > 0 && `${customerReqs.length} customer req${customerReqs.length !== 1 ? "s" : ""}`,
              questions.length > 0 && `${questions.length} question${questions.length !== 1 ? "s" : ""}`,
            ]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
        {open ? (
          <ChevronUp className="h-4 w-4 text-slate-400 shrink-0" />
        ) : (
          <ChevronDown className="h-4 w-4 text-slate-400 shrink-0" />
        )}
      </button>

      {open && (
        <div className="border-t border-slate-100 divide-y divide-slate-100">
          {/* JD */}
          {(campaign.template_enhanced_title || campaign.template_ai_description) && (
            <div className="px-6 py-5 space-y-2">
              <div className="flex items-center justify-between">
                <Label>
                  Template Job Description
                  {campaign.template_enhanced_title && (
                    <span className="text-slate-400 font-normal ml-2">
                      — {campaign.template_enhanced_title}
                    </span>
                  )}
                </Label>
                {campaign.template_ai_description && (
                  <button
                    type="button"
                    onClick={() => setShowJD(!showJD)}
                    className="text-xs text-primary hover:underline"
                  >
                    {showJD ? "Hide" : "Show"}
                  </button>
                )}
              </div>
              {showJD && campaign.template_ai_description && (
                <div className="bg-slate-50 border border-slate-200 rounded-lg p-4 text-sm text-slate-700 whitespace-pre-wrap max-h-72 overflow-y-auto">
                  {campaign.template_ai_description}
                </div>
              )}
            </div>
          )}

          {/* Rubric */}
          {rubricSections.length > 0 && (
            <div className="px-6 py-5 space-y-4">
              <Label>Grading Rubric</Label>
              <div className="space-y-3">
                {rubricSections.map(({ label, items }) => (
                  <div key={label} className="space-y-1.5">
                    <Label className="text-slate-500">{label}</Label>
                    <div className="flex flex-wrap gap-1.5">
                      {items.map((it, i) => (
                        <span
                          key={i}
                          className="text-xs bg-slate-100 text-slate-600 rounded px-2 py-1"
                        >
                          {summarizeRow(it)}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Screening Questions */}
          {questions.length > 0 && (
            <div className="px-6 py-5 space-y-3">
              <Label>Screening Questions</Label>
              <div className="space-y-3">
                {questions.map((q, idx) => (
                  <div
                    key={idx}
                    className="border border-slate-200 rounded-lg p-3 space-y-2 bg-white"
                  >
                    <div className="flex items-start gap-2">
                      <span className="text-xs font-medium text-slate-400 mt-2 w-5 shrink-0">
                        {idx + 1}.
                      </span>
                      <p className="flex-1 text-sm text-slate-800 leading-relaxed">
                        {(q.question_text as string) || "—"}
                      </p>
                    </div>
                    {Boolean(q.pass_criteria) && (
                      <p className="pl-7 text-xs text-slate-500">
                        <span className="font-medium text-slate-600">Pass criteria:</span>{" "}
                        {String(q.pass_criteria)}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
