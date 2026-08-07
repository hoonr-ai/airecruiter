"use client";

// Common-properties form for a campaign (name + operational settings every
// child job inherits). Used by both the "New Campaign" creation flow and the
// "Edit" dialog on the detail page.

import { useState } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
  const [recruiterNotes, setRecruiterNotes] = useState(initial?.recruiter_notes ?? "");

  // Outreach 1
  const [outreach1Enabled, setOutreach1Enabled] = useState(
    (initial?.outreach_delay_mins ?? 0) >= 0
  );
  const [outreachDelayMins, setOutreachDelayMins] = useState<string>(
    initial?.outreach_delay_mins !== null && initial?.outreach_delay_mins !== undefined
      ? initial.outreach_delay_mins < 0 ? "10" : initial.outreach_delay_mins.toString()
      : "10"
  );

  // Outreach 2
  const initialOutreach2Enabled = (initial?.phase1_6hr_reminder_hours ?? 0) >= 0;
  const [outreach2Enabled, setOutreach2Enabled] = useState(initialOutreach2Enabled);
  const [questions, setQuestions] = useState<TemplateQuestion[]>(
    initial?.template_screen_questions && initial.template_screen_questions.length > 0
      ? (initial.template_screen_questions as TemplateQuestion[])
      : getDefaultCampaignScreeningQuestions()
  );
  const [phase1ReminderHours, setPhase1ReminderHours] = useState<string>(
    initial?.phase1_6hr_reminder_hours !== null && initial?.phase1_6hr_reminder_hours !== undefined
      ? initial.phase1_6hr_reminder_hours < 0 ? "1.0" : initial.phase1_6hr_reminder_hours.toString()
      : "1.0"
  );
  const [phase1ReminderCallDelayMins, setPhase1ReminderCallDelayMins] = useState<string>(
    initial?.phase1_6hr_call_delay_mins !== null && initial?.phase1_6hr_call_delay_mins !== undefined
      ? initial.phase1_6hr_call_delay_mins < 0 ? "10" : initial.phase1_6hr_call_delay_mins.toString()
      : "10"
  );

  // Outreach 3 (Enforce sequential: require outreach 2 enabled)
  const initialOutreach3Enabled = initialOutreach2Enabled && initial?.phase1_to_phase2_hours !== -1;
  const [outreach3Enabled, setOutreach3Enabled] = useState(initialOutreach3Enabled);
  const [phase1To2Hours, setPhase1To2Hours] = useState<string>(
    initial?.phase1_to_phase2_hours !== null && initial?.phase1_to_phase2_hours !== undefined
      ? initial.phase1_to_phase2_hours < 0 ? "1.5" : initial.phase1_to_phase2_hours.toString()
      : "1.5"
  );
  const [phase2CallDelayMins, setPhase2CallDelayMins] = useState<string>(
    initial?.phase2_call_delay_mins !== null && initial?.phase2_call_delay_mins !== undefined
      ? initial.phase2_call_delay_mins < 0 ? "10" : initial.phase2_call_delay_mins.toString()
      : "10"
  );

  // Outreach 4 (Enforce sequential: require outreach 3 & 2 enabled)
  const initialOutreach4Enabled = initialOutreach3Enabled && initial?.phase2_to_phase3_hours !== -1;
  const [outreach4Enabled, setOutreach4Enabled] = useState(initialOutreach4Enabled);
  const [phase2To3Hours, setPhase2To3Hours] = useState<string>(
    initial?.phase2_to_phase3_hours !== null && initial?.phase2_to_phase3_hours !== undefined
      ? initial.phase2_to_phase3_hours < 0 ? "3.0" : initial.phase2_to_phase3_hours.toString()
      : "3.0"
  );
  const [phase3CallDelayMins, setPhase3CallDelayMins] = useState<string>(
    initial?.phase3_call_delay_mins !== null && initial?.phase3_call_delay_mins !== undefined
      ? initial.phase3_call_delay_mins < 0 ? "10" : initial.phase3_call_delay_mins.toString()
      : "10"
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

  const parseNum = (val: string, isFloat = false): number | undefined => {
    const trimmed = val.trim();
    if (!trimmed) return undefined;
    const num = isFloat ? parseFloat(trimmed) : parseInt(trimmed, 10);
    return !isNaN(num) ? num : undefined;
  };

  const setIfNonNegative = (value: string, setValue: (v: string) => void) => {
    if (value === "") {
      setValue(value);
      return;
    }
    const num = Number(value);
    if (!isNaN(num) && num >= 0) setValue(value);
  };

  const handleSubmit = () => {
    if (!name.trim() || !customerName.trim() || emails.length === 0 || empTypes.length === 0) {
      setNameError("Campaign Name, Customer, at least one Recruiter Email, and Employment Type are required");
      return;
    }
    setNameError(null);

    // Reconcile sequential enforcement against any potential stale/inconsistent data at submit time
    const effectiveOutreach2 = outreach2Enabled;
    const effectiveOutreach3 = effectiveOutreach2 && outreach3Enabled;
    const effectiveOutreach4 = effectiveOutreach3 && outreach4Enabled;

    onSubmit({
      name: name.trim(),
      customer_name: customerName.trim() || undefined,
      recruiter_emails: emails,
      selected_employment_types: empTypes,
      screening_level: screeningLevel,
      selected_job_boards: jobBoards,
      bot_introduction: botIntro.trim() || undefined,
      outreach_delay_mins: outreach1Enabled ? (parseNum(outreachDelayMins) ?? 0) : -1,
      phase1_6hr_reminder_hours: effectiveOutreach2 ? (parseNum(phase1ReminderHours, true) ?? 0) : -1,
      phase1_to_phase2_hours: effectiveOutreach3 ? (parseNum(phase1To2Hours, true) ?? 0) : -1,
      phase2_to_phase3_hours: effectiveOutreach4 ? (parseNum(phase2To3Hours, true) ?? 0) : -1,
      phase1_6hr_call_delay_mins: effectiveOutreach2 ? (parseNum(phase1ReminderCallDelayMins) ?? 0) : -1,
      phase2_call_delay_mins: effectiveOutreach3 ? (parseNum(phase2CallDelayMins) ?? 0) : -1,
      phase3_call_delay_mins: effectiveOutreach4 ? (parseNum(phase3CallDelayMins) ?? 0) : -1,
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
        <Label>Publish To (Job Boards)</Label>
        <div className="flex flex-wrap gap-2">
          {JOB_BOARDS.map((b) => (
            <PillToggle key={b} active={jobBoards.includes(b)} onClick={() => toggle(jobBoards, setJobBoards, b)}>
              {b}
            </PillToggle>
          ))}
        </div>
      </div>

      <div className="space-y-4 rounded-xl border border-slate-200 bg-slate-50/70 p-4">
        <div>
          <h3 className="text-sm font-semibold text-slate-900 font-outfit">Outreach Schedule</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Configure the 4 outreach steps. Uncheck a step to skip it (<span className="font-semibold text-slate-700">-1</span> saved internally). Default values from environment are used when 0 or left as-is.
          </p>
        </div>

        {/* Header row */}
        <div className="grid grid-cols-[20px_1fr_120px_120px] gap-2 items-center px-1">
          <span />
          <span className="text-[11px] font-semibold text-slate-500 uppercase tracking-wide">Outreach Step</span>
          <span className="text-[11px] font-semibold text-slate-500 uppercase tracking-wide text-center">Msg Delay</span>
          <span className="text-[11px] font-semibold text-slate-500 uppercase tracking-wide text-center">Call Delay</span>
        </div>

        {/* Outreach 1 — Initial */}
        <div className={cn("grid grid-cols-[20px_1fr_120px_120px] gap-2 items-center rounded-lg border p-2 transition-colors", outreach1Enabled ? "bg-white border-slate-200" : "bg-slate-100 border-slate-200 opacity-60")}>
          <Checkbox
            id="o1-enabled"
            checked={outreach1Enabled}
            onCheckedChange={(v) => setOutreach1Enabled(!!v)}
          />
          <label htmlFor="o1-enabled" className="text-sm font-medium text-slate-700 cursor-pointer select-none">
            Outreach 1 <span className="text-xs text-slate-400 font-normal">· Initial (immediate)</span>
          </label>
          <div className="text-center text-xs text-slate-400 italic">Immediate</div>
          <Input
            id="call-p1"
            type="number"
            min={0}
            disabled={!outreach1Enabled}
            value={outreachDelayMins}
            onChange={(e) => setIfNonNegative(e.target.value, setOutreachDelayMins)}
            placeholder="10 min"
            className="text-xs h-8 text-center bg-white"
          />
        </div>

        {/* Outreach 2 — Reminder 1 */}
        <div className={cn("grid grid-cols-[20px_1fr_120px_120px] gap-2 items-center rounded-lg border p-2 transition-colors", outreach2Enabled ? "bg-white border-slate-200" : "bg-slate-100 border-slate-200 opacity-60")}>
          <Checkbox
            id="o2-enabled"
            checked={outreach2Enabled}
            onCheckedChange={(v) => {
              const checked = !!v;
              setOutreach2Enabled(checked);
              if (!checked) {
                // Note: Cascading reset intentionally only clears the *Enabled booleans, 
                // not the associated delay/time input fields (e.g. phase1To2Hours, phase2To3Hours). 
                // Disabled steps are serialized as -1 during submission regardless of their input values, 
                // and retaining the user's input prevents data loss if they toggle off and back on.
                setOutreach3Enabled(false);
                setOutreach4Enabled(false);
              }
            }}
          />
          <label htmlFor="o2-enabled" className="text-sm font-medium text-slate-700 cursor-pointer select-none">
            Outreach 2 <span className="text-xs text-slate-400 font-normal">· Reminder 1</span>
          </label>
          <Input
            id="rem-6hr"
            type="number"
            step="0.1"
            min={0}
            disabled={!outreach2Enabled}
            value={phase1ReminderHours}
            onChange={(e) => setIfNonNegative(e.target.value, setPhase1ReminderHours)}
            placeholder="1.0 hr"
            className="text-xs h-8 text-center bg-white"
          />
          <Input
            id="call-p1-6hr"
            type="number"
            min={0}
            disabled={!outreach2Enabled}
            value={phase1ReminderCallDelayMins}
            onChange={(e) => setIfNonNegative(e.target.value, setPhase1ReminderCallDelayMins)}
            placeholder="10 min"
            className="text-xs h-8 text-center bg-white"
          />
        </div>

        {/* Outreach 3 — Reminder 2 */}
        <div className={cn("grid grid-cols-[20px_1fr_120px_120px] gap-2 items-center rounded-lg border p-2 transition-colors", (outreach3Enabled && outreach2Enabled) ? "bg-white border-slate-200" : "bg-slate-100 border-slate-200 opacity-60")}>
          <Checkbox
            id="o3-enabled"
            checked={outreach3Enabled}
            disabled={!outreach2Enabled}
            title={!outreach2Enabled ? "Enable Outreach 2 first" : undefined}
            onCheckedChange={(v) => {
              const checked = !!v;
              setOutreach3Enabled(checked);
              if (!checked) {
                // Note: Cascading reset intentionally only clears the *Enabled booleans, 
                // not the associated delay/time input fields (e.g. phase2To3Hours). 
                // Disabled steps are serialized as -1 during submission regardless of their input values.
                setOutreach4Enabled(false);
              }
            }}
          />
          <label 
            htmlFor="o3-enabled" 
            className="text-sm font-medium text-slate-700 cursor-pointer select-none flex items-center gap-2"
            title={!outreach2Enabled ? "Enable Outreach 2 first" : undefined}
          >
            <span>Outreach 3 <span className="text-xs text-slate-400 font-normal">· Reminder 2</span></span>
            {!outreach2Enabled && (
              <span className="text-[10px] text-amber-600 font-normal bg-amber-50 px-1.5 py-0.5 rounded border border-amber-200">
                Requires Outreach 2
              </span>
            )}
          </label>
          <Input
            id="rem-p1-p2"
            type="number"
            step="0.1"
            min={0}
            disabled={!outreach3Enabled}
            value={phase1To2Hours}
            onChange={(e) => setIfNonNegative(e.target.value, setPhase1To2Hours)}
            placeholder="1.5 hr"
            className="text-xs h-8 text-center bg-white"
          />
          <Input
            id="call-p2"
            type="number"
            min={0}
            disabled={!outreach3Enabled}
            value={phase2CallDelayMins}
            onChange={(e) => setIfNonNegative(e.target.value, setPhase2CallDelayMins)}
            placeholder="10 min"
            className="text-xs h-8 text-center bg-white"
          />
        </div>

        {/* Outreach 4 — Reminder 3 */}
        <div className={cn("grid grid-cols-[20px_1fr_120px_120px] gap-2 items-center rounded-lg border p-2 transition-colors", (outreach4Enabled && outreach3Enabled && outreach2Enabled) ? "bg-white border-slate-200" : "bg-slate-100 border-slate-200 opacity-60")}>
          <Checkbox
            id="o4-enabled"
            checked={outreach4Enabled}
            disabled={!outreach3Enabled}
            title={!outreach3Enabled ? "Enable Outreach 3 first" : undefined}
            onCheckedChange={(v) => setOutreach4Enabled(!!v)}
          />
          <label 
            htmlFor="o4-enabled" 
            className="text-sm font-medium text-slate-700 cursor-pointer select-none flex items-center gap-2"
            title={!outreach3Enabled ? "Enable Outreach 3 first" : undefined}
          >
            <span>Outreach 4 <span className="text-xs text-slate-400 font-normal">· Reminder 3</span></span>
            {!outreach3Enabled && (
              <span className="text-[10px] text-amber-600 font-normal bg-amber-50 px-1.5 py-0.5 rounded border border-amber-200">
                Requires Outreach 3
              </span>
            )}
          </label>
          <Input
            id="rem-p2-p3"
            type="number"
            step="0.1"
            min={0}
            disabled={!outreach4Enabled}
            value={phase2To3Hours}
            onChange={(e) => setIfNonNegative(e.target.value, setPhase2To3Hours)}
            placeholder="3.0 hr"
            className="text-xs h-8 text-center bg-white"
          />
          <Input
            id="call-p3"
            type="number"
            min={0}
            disabled={!outreach4Enabled}
            value={phase3CallDelayMins}
            onChange={(e) => setIfNonNegative(e.target.value, setPhase3CallDelayMins)}
            placeholder="10 min"
            className="text-xs h-8 text-center bg-white"
          />
        </div>

        <p className="text-[11px] text-slate-400 pt-1">
          <span className="font-semibold text-slate-500">Msg Delay</span>: hours after previous outreach to send next message. &nbsp;
          <span className="font-semibold text-slate-500">Call Delay</span>: minutes after message to initiate AI phone call.
        </p>
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
