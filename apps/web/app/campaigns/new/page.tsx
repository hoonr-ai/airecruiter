"use client";

// Lean 3-step campaign wizard.
//   Step 1  Setup           — common properties every job inherits + the seed role
//   Step 2  JD & Rubric     — AI-generated (editable) job description + rubric template
//   Step 3  Screening       — AI-generated (editable) screening questions
// Reuses the SAME backend AI endpoints as the jobs wizard (via lib/campaigns)
// but shares no code with the 9,591-line jobs wizard. Saves the template_* fields
// onto the campaign; child jobs inherit them.

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, ArrowRight, Check, Loader2, Sparkles, X } from "lucide-react";
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
import { RubricEditor } from "@/components/campaigns/RubricEditor";
import { ScreeningQuestionsEditor } from "@/components/campaigns/ScreeningQuestionsEditor";
import { cn } from "@/lib/utils";
import {
  Rubric,
  TemplateQuestion,
  CampaignCreatePayload,
  EMPLOYMENT_TYPES,
  JOB_BOARDS,
  SCREENING_LEVELS,
  isValidRecruiterEmail,
  createCampaign,
  generateJobDescription,
  generateRubric,
  generateScreeningQuestions,
} from "@/lib/campaigns";

const STEPS = ["Setup", "JD & Rubric", "Screening"];

function Pill({
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

export default function NewCampaignPage() {
  const router = useRouter();
  const [step, setStep] = useState(1);

  // Step 1 — common props + seed role
  const [name, setName] = useState("");
  const [customerName, setCustomerName] = useState("");
  const [emails, setEmails] = useState<string[]>([]);
  const [emailInput, setEmailInput] = useState("");
  const [emailError, setEmailError] = useState<string | null>(null);
  const [empTypes, setEmpTypes] = useState<string[]>([]);
  const [screeningLevel, setScreeningLevel] = useState("L1.5");
  const [jobBoards, setJobBoards] = useState<string[]>([]);
  const defaultBotIntro = `Hi {{candidate name}}, I'm Alex, a virtual recruiter with Pyramid Consulting. We are helping our client recruit for a {{job_title}} in {{job_location}}, and you seem to be a good fit for the role. Please note that conversation may be recorded for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?`;
  const [botIntro, setBotIntro] = useState(defaultBotIntro);
  const [recruiterNotes, setRecruiterNotes] = useState("");
  const [jobTitle, setJobTitle] = useState("");
  const [seedNotes, setSeedNotes] = useState("");
  const [setupError, setSetupError] = useState<string | null>(null);

  // Step 2 — JD + rubric
  const [jobDescription, setJobDescription] = useState("");
  const [rubric, setRubric] = useState<Rubric | null>(null);
  const [isGeneratingJD, setIsGeneratingJD] = useState(false);
  const [isGeneratingRubric, setIsGeneratingRubric] = useState(false);

  // Step 3 — questions
  const [questions, setQuestions] = useState<TemplateQuestion[]>([]);
  const [isGeneratingQuestions, setIsGeneratingQuestions] = useState(false);

  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ message: string; type: "error" | "success" } | null>(null);

  const flash = (message: string, type: "error" | "success" = "error") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3500);
  };

  const toggle = (list: string[], setList: (v: string[]) => void, value: string) =>
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

  const addEmail = () => {
    const c = emailInput.trim();
    if (!c) return;
    if (!isValidRecruiterEmail(c)) {
      setEmailError(
        c.toLowerCase().endsWith("@jobdiva.com")
          ? "JobDiva placeholder emails aren't allowed"
          : "Enter a valid email address",
      );
      return;
    }
    if (!emails.includes(c)) setEmails([...emails, c]);
    setEmailInput("");
    setEmailError(null);
  };

  const runGenerateJD = async () => {
    if (!jobTitle.trim()) return;
    setIsGeneratingJD(true);
    try {
      const jd = await generateJobDescription({
        jobTitle: jobTitle.trim(),
        jobNotes: seedNotes,
        jobDescription,
      });
      if (jd) setJobDescription(jd);
    } catch {
      flash("Couldn't auto-generate the description — you can write it manually.");
    } finally {
      setIsGeneratingJD(false);
    }
  };

  const runGenerateRubric = async () => {
    if (!jobTitle.trim()) return;
    setIsGeneratingRubric(true);
    try {
      const r = await generateRubric({
        jobTitle: jobTitle.trim(),
        jobDescription,
        jobNotes: seedNotes,
        customerName,
      });
      setRubric(r);
    } catch {
      flash("Couldn't auto-generate the rubric — you can edit it manually.");
      if (!rubric) setRubric({ titles: [], skills: [], soft_skills: [] });
    } finally {
      setIsGeneratingRubric(false);
    }
  };

  const runGenerateQuestions = async () => {
    setIsGeneratingQuestions(true);
    try {
      const qs = await generateScreeningQuestions({
        jobTitle: jobTitle.trim(),
        rubric: rubric ?? {},
        screeningLevel,
        jobDescription,
        customerName,
        totalYears: rubric?.total_years,
      });
      setQuestions(qs);
    } catch {
      flash("Couldn't auto-generate questions — you can add them manually.");
    } finally {
      setIsGeneratingQuestions(false);
    }
  };

  const goToStep2 = async () => {
    if (!name.trim() || !jobTitle.trim() || !customerName.trim() || emails.length === 0 || empTypes.length === 0) {
      setSetupError("Campaign Name, Seed Role/Title, Customer, at least one Recruiter Email, and Employment Type are required.");
      return;
    }
    setSetupError(null);
    setStep(2);
    if (!jobDescription) await runGenerateJD();
    if (!rubric) await runGenerateRubric();
  };

  const goToStep3 = async () => {
    setStep(3);
    if (!botIntro.trim()) {
      const defaultCampaignIntro = `Hi {{candidate name}}, I'm Alex, a virtual recruiter with Pyramid Consulting. We are helping our client recruit for a {{job_title}} in {{job_location}}, and you seem to be a good fit for the role. Please note that conversation may be recorded for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?`;
      setBotIntro(defaultCampaignIntro);
    }
    if (questions.length === 0) await runGenerateQuestions();
  };

  const handleCreate = async () => {
    if (!name.trim() || !jobTitle.trim() || !customerName.trim() || emails.length === 0 || empTypes.length === 0) {
      setStep(1);
      setSetupError("Campaign Name, Seed Role/Title, Customer, at least one Recruiter Email, and Employment Type are required.");
      return;
    }
    setSaving(true);
    try {
      const payload: CampaignCreatePayload = {
        name: name.trim(),
        customer_name: customerName.trim() || undefined,
        recruiter_emails: emails,
        selected_employment_types: empTypes,
        screening_level: screeningLevel,
        recruiter_notes: recruiterNotes.trim() || undefined,
        selected_job_boards: jobBoards,
        bot_introduction: botIntro.trim() || undefined,
        template_enhanced_title: jobTitle.trim(),
        template_ai_description: jobDescription,
        template_rubric: rubric ?? undefined,
        template_screen_questions: questions,
      };
      const { campaign_id } = await createCampaign(payload);
      router.push(`/campaigns/${campaign_id}`);
    } catch {
      flash("Couldn't create the campaign. Try again.");
      setSaving(false);
    }
  };

  return (
    <div className="max-w-6xl mx-auto pb-24">
      <Link
        href="/campaigns"
        className="inline-flex items-center text-sm text-slate-500 hover:text-slate-800 mb-4"
      >
        <ArrowLeft className="h-4 w-4 mr-1.5" /> Campaigns
      </Link>

      <h1 className="text-2xl font-semibold text-slate-900 font-outfit mb-1">New Campaign</h1>
      <p className="text-sm text-slate-500 mb-6">
        Define the shared settings and a reusable JD / rubric / questions template. Jobs added later
        inherit all of it in one step.
      </p>

      {/* Step indicator */}
      <div className="flex items-center gap-2 mb-8">
        {STEPS.map((label, i) => {
          const n = i + 1;
          const active = n === step;
          const done = n < step;
          return (
            <button
              key={label}
              type="button"
              onClick={() => n < step && setStep(n)}
              disabled={n > step}
              className={cn(
                "flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm transition-colors",
                active && "bg-primary text-white",
                done && "text-slate-700 hover:bg-slate-100 cursor-pointer",
                !active && !done && "text-slate-400",
              )}
            >
              <span
                className={cn(
                  "flex items-center justify-center h-5 w-5 rounded-full text-xs",
                  active ? "bg-white/20" : done ? "bg-emerald-100 text-emerald-700" : "bg-slate-100",
                )}
              >
                {done ? <Check className="h-3 w-3" /> : n}
              </span>
              {label}
            </button>
          );
        })}
      </div>

      {/* Step 1 — Setup */}
      {step === 1 && (
        <div className="space-y-5 bg-white border border-slate-200 rounded-xl p-6">
          <div className="space-y-1.5">
            <Label htmlFor="c-name">
              Campaign Name <span className="text-red-500">*</span>
            </Label>
            <Input id="c-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Q3 Java Backend — 10 seats" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="c-title">
              Seed Role / Title <span className="text-red-500">*</span>
            </Label>
            <Input id="c-title" value={jobTitle} onChange={(e) => setJobTitle(e.target.value)} placeholder="e.g. Senior Java Engineer" />
            <p className="text-xs text-slate-400">The AI drafts the template JD, rubric, and questions from this.</p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="c-customer">
              Customer <span className="text-red-500">*</span>
            </Label>
            <Input id="c-customer" value={customerName} onChange={(e) => setCustomerName(e.target.value)} placeholder="Hiring client / account" />
          </div>

          <div className="space-y-1.5">
            <Label>
              Recruiter Email(s) <span className="text-red-500">*</span>
            </Label>
            <div className="flex flex-wrap gap-2">
              {emails.map((email) => (
                <span key={email} className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-3 py-1 text-sm text-slate-700">
                  {email}
                  <button type="button" onClick={() => setEmails(emails.filter((e) => e !== email))} aria-label={`Remove ${email}`}>
                    <X className="h-3 w-3 text-slate-400 hover:text-slate-600" />
                  </button>
                </span>
              ))}
            </div>
            <Input
              value={emailInput}
              onChange={(e) => { setEmailInput(e.target.value); if (emailError) setEmailError(null); }}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addEmail(); } }}
              onBlur={addEmail}
              placeholder="Type an email and press Enter"
            />
            {emailError && <p className="text-xs text-destructive">{emailError}</p>}
          </div>

          <div className="space-y-1.5">
            <Label>
              Employment Type <span className="text-red-500">*</span>
            </Label>
            <div className="flex flex-wrap gap-2">
              {EMPLOYMENT_TYPES.map((t) => (
                <Pill key={t} active={empTypes.includes(t)} onClick={() => toggle(empTypes, setEmpTypes, t)}>{t}</Pill>
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="c-screen">Screening Level</Label>
            <Select value={screeningLevel} onValueChange={setScreeningLevel}>
              <SelectTrigger id="c-screen" className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {SCREENING_LEVELS.map((s) => (
                  <SelectItem key={s.value} value={s.value}>{s.label} · {s.hint}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Publish To (Job Boards)</Label>
            <div className="flex flex-wrap gap-2">
              {JOB_BOARDS.map((b) => (
                <Pill key={b} active={jobBoards.includes(b)} onClick={() => toggle(jobBoards, setJobBoards, b)}>{b}</Pill>
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="c-bot">Bot Introduction</Label>
            <Textarea id="c-bot" value={botIntro} onChange={(e) => setBotIntro(e.target.value)} rows={2} placeholder="What the screening bot says at the start of a call" />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="c-notes">Recruiter Notes / Seed Context</Label>
            <Textarea id="c-notes" value={seedNotes} onChange={(e) => { setSeedNotes(e.target.value); setRecruiterNotes(e.target.value); }} rows={3} placeholder="Context for the AI to draft the template (skills, must-haves, etc.)" />
          </div>

          {setupError && <p className="text-sm text-destructive">{setupError}</p>}
        </div>
      )}

      {/* Step 2 — JD + Rubric */}
      {step === 2 && (
        <div className="space-y-6">
          <div className="bg-white border border-slate-200 rounded-xl p-6 space-y-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="c-jd">Template Job Description</Label>
              <Button type="button" variant="outline" size="sm" onClick={runGenerateJD} disabled={isGeneratingJD}>
                {isGeneratingJD ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                <span className="ml-1.5">Regenerate</span>
              </Button>
            </div>
            <Textarea id="c-jd" value={jobDescription} onChange={(e) => setJobDescription(e.target.value)} rows={12} placeholder={isGeneratingJD ? "Generating…" : "Job description (markdown)"} />
          </div>

          <div className="bg-white border border-slate-200 rounded-xl p-6 space-y-3">
            <div className="flex items-center justify-between">
              <Label>Grading Rubric</Label>
              <Button type="button" variant="outline" size="sm" onClick={runGenerateRubric} disabled={isGeneratingRubric}>
                {isGeneratingRubric ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                <span className="ml-1.5">Regenerate</span>
              </Button>
            </div>
            {isGeneratingRubric && !rubric ? (
              <p className="text-sm text-slate-400">Generating rubric…</p>
            ) : (
              <RubricEditor rubric={rubric ?? { titles: [], skills: [], soft_skills: [] }} onChange={setRubric} />
            )}
          </div>
        </div>
      )}

      {/* Step 3 — Screening questions */}
      {step === 3 && (
        <div className="bg-white border border-slate-200 rounded-xl p-6 space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <Label>Screening Questions</Label>
              {questions.length > 0 && (
                <span className="ml-2 text-[12px] font-normal text-slate-500">
                  {questions.length} question{questions.length !== 1 ? "s" : ""}
                </span>
              )}
            </div>
          </div>
          {isGeneratingQuestions && questions.length === 0 ? (
            <p className="text-sm text-slate-400">Generating questions…</p>
          ) : (
            <ScreeningQuestionsEditor
              questions={questions}
              onChange={setQuestions}
            />
          )}
        </div>
      )}

      {/* Footer nav */}
      <div className="fixed bottom-0 left-64 right-0 bg-white border-t border-slate-200 px-8 py-4 flex justify-between">
        <Button type="button" variant="outline" onClick={() => (step === 1 ? router.push("/campaigns") : setStep(step - 1))} disabled={saving}>
          {step === 1 ? "Cancel" : "Back"}
        </Button>
        {step < 3 ? (
          <Button type="button" onClick={step === 1 ? goToStep2 : goToStep3}>
            Next <ArrowRight className="h-4 w-4 ml-1.5" />
          </Button>
        ) : (
          <Button type="button" onClick={handleCreate} disabled={saving}>
            {saving ? "Creating…" : "Create Campaign"}
          </Button>
        )}
      </div>

      {toast && (
        <div className={`fixed bottom-20 right-6 px-4 py-3 rounded-lg shadow-lg text-sm text-white ${toast.type === "success" ? "bg-emerald-600" : "bg-red-600"}`}>
          {toast.message}
        </div>
      )}
    </div>
  );
}
