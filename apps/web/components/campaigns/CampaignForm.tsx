"use client";

// Common-properties form for a campaign (name + operational settings every
// child job inherits). Used by both the "New Campaign" creation flow and the
// "Edit" dialog on the detail page.

import { useState } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import {
  Campaign,
  CampaignCreatePayload,
  EMPLOYMENT_TYPES,
  JOB_BOARDS,
  SCREENING_LEVELS,
  isValidRecruiterEmail,
  TemplateQuestion,
  getDefaultCampaignScreeningQuestions,
} from "@/lib/campaigns";
import { ScreeningQuestionsEditor } from "@/components/campaigns/ScreeningQuestionsEditor";

interface CampaignFormProps {
  initial?: Partial<Campaign>;
  submitting?: boolean;
  submitLabel?: string;
  onSubmit: (payload: CampaignCreatePayload) => void;
  onCancel?: () => void;
}

function PillToggle({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "px-3 py-1.5 rounded-lg border text-sm font-medium transition-colors",
        active
          ? "bg-primary text-white border-primary"
          : "bg-white text-slate-600 border-slate-200 hover:border-slate-300",
      )}
    >
      {children}
    </button>
  );
}

export function CampaignForm({
  initial,
  submitting = false,
  submitLabel = "Create Campaign",
  onSubmit,
  onCancel,
}: CampaignFormProps) {
  const [name, setName] = useState(initial?.name ?? "");
  const [customerName, setCustomerName] = useState(initial?.customer_name ?? "");
  const [emails, setEmails] = useState<string[]>(initial?.recruiter_emails ?? []);
  const [emailInput, setEmailInput] = useState("");
  const [emailError, setEmailError] = useState<string | null>(null);
  const [empTypes, setEmpTypes] = useState<string[]>(initial?.selected_employment_types ?? []);
  const [screeningLevel, setScreeningLevel] = useState<string>(initial?.screening_level ?? "L1.5");
  const [jobBoards, setJobBoards] = useState<string[]>(initial?.selected_job_boards ?? []);
  const defaultBotIntro = `Hi {{candidate name}}, I'm Alex, a virtual recruiter with Pyramid Consulting. We are helping our client recruit for a {{job_title}} in {{job_location}}, and you seem to be a good fit for the role. Please note that conversation may be recorded for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?`;
  const [botIntro, setBotIntro] = useState(initial?.bot_introduction?.trim() ? initial.bot_introduction : defaultBotIntro);
  const [outreachDelayMins, setOutreachDelayMins] = useState<string>(
    initial?.outreach_delay_mins !== null && initial?.outreach_delay_mins !== undefined
      ? initial.outreach_delay_mins.toString()
      : ""
  );
  const [recruiterNotes, setRecruiterNotes] = useState(initial?.recruiter_notes ?? "");
  const [questions, setQuestions] = useState<TemplateQuestion[]>(
    initial?.template_screen_questions && initial.template_screen_questions.length > 0
      ? (initial.template_screen_questions as TemplateQuestion[])
      : getDefaultCampaignScreeningQuestions()
  );
  const [nameError, setNameError] = useState<string | null>(null);

  const addEmail = () => {
    const candidate = emailInput.trim();
    if (!candidate) return;
    if (!isValidRecruiterEmail(candidate)) {
      setEmailError(
        candidate.toLowerCase().endsWith("@jobdiva.com")
          ? "JobDiva placeholder emails aren't allowed"
          : "Enter a valid email address",
      );
      return;
    }
    if (!emails.includes(candidate)) setEmails([...emails, candidate]);
    setEmailInput("");
    setEmailError(null);
  };

  const toggle = (list: string[], setList: (v: string[]) => void, value: string) =>
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

  const handleSubmit = () => {
    if (!name.trim() || !customerName.trim() || emails.length === 0 || empTypes.length === 0) {
      setNameError("Campaign Name, Customer, at least one Recruiter Email, and Employment Type are required");
      return;
    }
    setNameError(null);
    const parsedDelay = outreachDelayMins.trim() ? parseInt(outreachDelayMins.trim(), 10) : undefined;
    onSubmit({
      name: name.trim(),
      customer_name: customerName.trim() || undefined,
      recruiter_emails: emails,
      selected_employment_types: empTypes,
      screening_level: screeningLevel,
      selected_job_boards: jobBoards,
      bot_introduction: botIntro.trim() || undefined,
      outreach_delay_mins: parsedDelay !== undefined && !isNaN(parsedDelay) ? parsedDelay : undefined,
      recruiter_notes: recruiterNotes.trim() || undefined,
      template_screen_questions: questions,
    });
  };

  return (
    <div className="space-y-5">
      <div className="space-y-1.5">
        <Label htmlFor="campaign-name">
          Campaign Name <span className="text-red-500">*</span>
        </Label>
        <Input
          id="campaign-name"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            if (nameError) setNameError(null);
          }}
          placeholder="e.g. Q3 Java Backend — 10 seats"
        />
        {nameError && <p className="text-xs text-destructive">{nameError}</p>}
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="campaign-customer">
          Customer / Account <span className="text-red-500">*</span>
        </Label>
        <Input
          id="campaign-customer"
          value={customerName ?? ""}
          onChange={(e) => setCustomerName(e.target.value)}
          placeholder="Hiring client / account name"
        />
      </div>

      <div className="space-y-1.5">
        <Label>
          Recruiter Email(s) <span className="text-red-500">*</span>
        </Label>
        <div className="flex flex-wrap gap-2">
          {emails.map((email) => (
            <span
              key={email}
              className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-3 py-1 text-sm text-slate-700"
            >
              {email}
              <button
                type="button"
                onClick={() => setEmails(emails.filter((e) => e !== email))}
                className="text-slate-400 hover:text-slate-600"
                aria-label={`Remove ${email}`}
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
        <Input
          value={emailInput}
          onChange={(e) => {
            setEmailInput(e.target.value);
            if (emailError) setEmailError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              addEmail();
            }
          }}
          onBlur={addEmail}
          placeholder="Type an email and press Enter"
        />
        {emailError && <p className="text-xs text-destructive">{emailError}</p>}
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="campaign-notes">Recruiter Notes</Label>
        <Textarea
          id="campaign-notes"
          value={recruiterNotes ?? ""}
          onChange={(e) => setRecruiterNotes(e.target.value)}
          placeholder="Enter only common administrative rules across all roles (e.g., 'W2 only, max rate $80/hr'). Do not enter role-specific tech skills."
          rows={3}
        />
        <p className="text-xs text-amber-600">
          Enter only common administrative rules across all roles (e.g., 'W2 only, max rate $80/hr'). Do not enter role-specific tech skills, as these notes are prioritized when generating AI Job Descriptions across child jobs.
        </p>
      </div>

      <div className="space-y-1.5">
        <Label>
          Employment Type <span className="text-red-500">*</span>
        </Label>
        <div className="flex flex-wrap gap-2">
          {EMPLOYMENT_TYPES.map((t) => (
            <PillToggle key={t} active={empTypes.includes(t)} onClick={() => toggle(empTypes, setEmpTypes, t)}>
              {t}
            </PillToggle>
          ))}
        </div>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="campaign-screening">Screening Level</Label>
        <Select value={screeningLevel} onValueChange={setScreeningLevel}>
          <SelectTrigger id="campaign-screening" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SCREENING_LEVELS.map((s) => (
              <SelectItem key={s.value} value={s.value}>
                {s.label} · {s.hint}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="campaign-outreach-delay">Outreach Frequency (Minutes)</Label>
        <Input
          id="campaign-outreach-delay"
          type="number"
          min={0}
          value={outreachDelayMins}
          onChange={(e) => setOutreachDelayMins(e.target.value)}
          placeholder="e.g. 30 (Leaves empty for default delay)"
        />
        <p className="text-xs text-slate-500">
          Minutes to wait after sending initial Email/SMS before the AI bot dials the candidate.
        </p>
      </div>

      <div className="space-y-1.5">
        <Label>Publish To (Job Boards)</Label>
        <div className="flex flex-wrap gap-2">
          {JOB_BOARDS.map((b) => (
            <PillToggle key={b} active={jobBoards.includes(b)} onClick={() => toggle(jobBoards, setJobBoards, b)}>
              {b}
            </PillToggle>
          ))}
        </div>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="campaign-bot-intro">Bot Introduction</Label>
        <Textarea
          id="campaign-bot-intro"
          value={botIntro ?? ""}
          onChange={(e) => setBotIntro(e.target.value)}
          placeholder="What the screening bot says at the start of a call (supports {{brackets}} variables)"
          rows={3}
        />
      </div>

      <div className="space-y-3 pt-3 border-t border-slate-200">
        <div className="flex flex-col gap-1">
          <Label className="text-base font-semibold text-slate-900">
            Default Campaign Screening Questions
          </Label>
          <p className="text-xs text-slate-500 leading-relaxed">
            These baseline questions (shared across all child roles in the campaign) check openness to opportunities, work arrangement, availability, compensation, and work authorization before any role-specific technical questions are appended.
          </p>
        </div>
        <div className="rounded-xl border border-slate-200 bg-slate-50/50 p-4">
          <ScreeningQuestionsEditor
            questions={questions}
            onChange={setQuestions}
          />
        </div>
      </div>

      <div className="flex justify-end gap-2 pt-2">
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel} disabled={submitting}>
            Cancel
          </Button>
        )}
        <Button type="button" onClick={handleSubmit} disabled={submitting}>
          {submitting ? "Saving…" : submitLabel}
        </Button>
      </div>
    </div>
  );
}
