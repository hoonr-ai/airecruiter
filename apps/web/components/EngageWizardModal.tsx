"use client";

import { useState, useEffect, useCallback } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  User,
  Briefcase,
  HelpCircle,
  FileJson,
  ChevronRight,
  ChevronLeft,
  ChevronDown,
  Plus,
  Trash2,
  X,
  Copy,
  Check,
  Send,
  Loader2,
  Mail,
  Phone,
  MapPin,
  Clock,
  Code2,
} from "lucide-react";

// ─── Types ───────────────────────────────────────────────────────────────────

interface Question {
  question_text: string;
  pass_criteria: string;
  category: string;
  is_default: boolean;
}

interface CandidateFields {
  name: string;
  email: string;
  phone: string;
  summary: string;
  skills: string;
  experience: string;
  education: string;
}

interface JobFields {
  job_id: string;
  jobdiva_id: string;
  title: string;
  customer_name: string;
  city: string;
  state: string;
  location_type: string;
  description: string;
  interview_duration: string;
}

interface WizardState {
  candidate: CandidateFields;
  job: JobFields;
  questions: Question[];
}

interface EngageWizardModalProps {
  open: boolean;
  onClose: () => void;
  initialPayload: string;
  candidateIds: string[];
  onSend: (payload: string) => Promise<void>;
  loading: boolean;
  error: string | null;
  successData: any;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function parsePayload(raw: string): WizardState | null {
  try {
    const p = JSON.parse(raw);
    const resume = (p.resumes || [])[0] || {};
    const jd = p.jd || {};
    const ctx = jd.context || {};

    return {
      candidate: {
        name: resume.name || "",
        email: resume.email || "",
        phone: resume.phone || p.phone_number || "",
        summary: resume.summary || "",
        skills: resume.skills || "",
        experience: resume.experience || "",
        education: resume.education || "",
      },
      job: {
        job_id: jd.job_id || "",
        jobdiva_id: jd.jobdiva_id || "",
        title: ctx.title || "",
        customer_name: ctx.customer_name || "",
        city: ctx.city || "",
        state: ctx.state || "",
        location_type: ctx.location_type || "",
        description: ctx.jobdiva_description || "",
        interview_duration: p.interview_duration || "",
      },
      questions: (jd.pre_screen_questions || []).map((q: any) => ({
        question_text: q.question_text || "",
        pass_criteria: q.pass_criteria || "",
        category: q.category || "default",
        is_default: q.is_default ?? true,
      })),
    };
  } catch {
    return null;
  }
}

function buildPayload(raw: string, state: WizardState): string {
  try {
    const p = JSON.parse(raw);
    const resume = (p.resumes || [])[0] || {};

    const updatedResume = {
      ...resume,
      name: state.candidate.name,
      candidate_name: state.candidate.name,
      email: state.candidate.email,
      phone: state.candidate.phone,
      summary: state.candidate.summary,
      skills: state.candidate.skills,
      experience: state.candidate.experience,
      education: state.candidate.education,
    };

    const updatedJd = {
      ...p.jd,
      job_id: state.job.job_id,
      jobdiva_id: state.job.jobdiva_id,
      pre_screen_questions: state.questions.map((q) => ({
        question_text: q.question_text,
        pass_criteria: q.pass_criteria,
        is_default: q.is_default,
        category: q.category,
      })),
      context: {
        ...(p.jd?.context || {}),
        title: state.job.title,
        customer_name: state.job.customer_name,
        city: state.job.city,
        state: state.job.state,
        location_type: state.job.location_type,
        jobdiva_description: state.job.description,
      },
    };

    return JSON.stringify(
      {
        ...p,
        phone_number: state.candidate.phone,
        resumes: [updatedResume],
        jd: updatedJd,
        interview_duration: state.job.interview_duration,
      },
      null,
      2
    );
  } catch {
    return raw;
  }
}

const CATEGORY_COLORS: Record<string, string> = {
  technical: "bg-indigo-100 text-indigo-700 border-indigo-200",
  methodology: "bg-amber-100 text-amber-700 border-amber-200",
  "role-specific": "bg-emerald-100 text-emerald-700 border-emerald-200",
  default: "bg-slate-100 text-slate-600 border-slate-200",
};

const CATEGORY_BORDER: Record<string, string> = {
  technical: "border-l-indigo-500",
  methodology: "border-l-amber-500",
  "role-specific": "border-l-emerald-500",
  default: "border-l-slate-400",
};

const STEPS = [
  { id: 1, label: "Candidate", icon: User },
  { id: 2, label: "Job Details", icon: Briefcase },
  { id: 3, label: "Questions", icon: HelpCircle },
  { id: 4, label: "Review", icon: FileJson },
];

// ─── Sub-components ──────────────────────────────────────────────────────────

function StepIndicator({ current }: { current: number }) {
  return (
    <div className="mb-4 max-w-sm mx-auto w-full relative">
      <div className="flex items-start justify-between">
        {STEPS.map((step, idx) => {
          const Icon = step.icon;
          const isActive = step.id === current;
          const isDone = step.id < current;
          return (
            <div key={step.id} className="relative flex flex-col items-center flex-1">
              <div className="flex items-center w-full relative">
                {/* Left connector */}
                <div className={`flex-1 h-0.5 ${idx === 0 ? 'bg-transparent' : isDone || isActive ? 'bg-indigo-300' : 'bg-slate-200'}`} />

                {/* Circle */}
                <div
                  className={`w-9 h-9 shrink-0 rounded-full flex items-center justify-center border-2 transition-all duration-200 z-10 ${isActive
                      ? "bg-indigo-600 border-indigo-600 text-white shadow-md shadow-indigo-200"
                      : isDone
                        ? "bg-indigo-100 border-indigo-300 text-indigo-600"
                        : "bg-slate-50 border-slate-200 text-slate-400"
                    }`}
                >
                  <Icon className="w-4 h-4" />
                </div>

                {/* Right connector */}
                <div className={`flex-1 h-0.5 ${idx === STEPS.length - 1 ? 'bg-transparent' : isDone ? 'bg-indigo-300' : 'bg-slate-200'}`} />
              </div>

              {/* Label */}
              <span
                className={`text-[11px] font-medium whitespace-nowrap mt-1 ${isActive
                    ? "text-indigo-700"
                    : isDone
                      ? "text-indigo-500"
                      : "text-slate-400"
                  }`}
              >
                {step.label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function FieldGroup({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider px-0.5">
        {label}
      </label>
      {children}
    </div>
  );
}

// ─── Steps ───────────────────────────────────────────────────────────────────

function Step1Candidate({
  data,
  onChange,
}: {
  data: CandidateFields;
  onChange: (d: CandidateFields) => void;
}) {
  const set = (key: keyof CandidateFields) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => onChange({ ...data, [key]: e.target.value });

  return (
    <div className="space-y-6 pt-2">
      <div className="grid grid-cols-2 gap-6">
        <FieldGroup label="Full Name">
          <Input value={data.name} onChange={set("name")} placeholder="Jane Smith" className="h-11" />
        </FieldGroup>
        <FieldGroup label="Email">
          <Input value={data.email} onChange={set("email")} placeholder="jane@example.com" className="h-11" />
        </FieldGroup>
      </div>
      <div className="grid grid-cols-2 gap-6">
        <FieldGroup label="Phone">
          <Input value={data.phone} onChange={set("phone")} placeholder="+1-555-000-0000" className="h-11" />
        </FieldGroup>
        <FieldGroup label="Experience">
          <Input value={data.experience} onChange={set("experience")} placeholder="8 years" className="h-11" />
        </FieldGroup>
      </div>
      <FieldGroup label="Summary / Headline">
        <Textarea
          value={data.summary}
          onChange={set("summary")}
          rows={3}
          placeholder="Senior Software Engineer specializing in..."
          className="resize-none text-sm"
        />
      </FieldGroup>
      <FieldGroup label="Skills">
        <Textarea
          value={data.skills}
          onChange={set("skills")}
          rows={2}
          placeholder="React, TypeScript, Node.js..."
          className="resize-none text-sm"
        />
      </FieldGroup>
      <FieldGroup label="Education">
        <Input value={data.education} onChange={set("education")} placeholder="B.S. Computer Science, MIT" />
      </FieldGroup>
    </div>
  );
}

function Step2Job({
  data,
  onChange,
}: {
  data: JobFields;
  onChange: (d: JobFields) => void;
}) {
  const set = (key: keyof JobFields) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => onChange({ ...data, [key]: e.target.value });

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <FieldGroup label="Job Title">
          <Input value={data.title} onChange={set("title")} placeholder="Software Engineer" />
        </FieldGroup>
        <FieldGroup label="Customer / Company">
          <Input value={data.customer_name} onChange={set("customer_name")} placeholder="Acme Corp" />
        </FieldGroup>
      </div>
      <div className="grid grid-cols-3 gap-4">
        <FieldGroup label="City">
          <Input value={data.city} onChange={set("city")} placeholder="Austin" />
        </FieldGroup>
        <FieldGroup label="State">
          <Input value={data.state} onChange={set("state")} placeholder="TX" />
        </FieldGroup>
        <FieldGroup label="Location Type">
          <Select
            value={data.location_type}
            onValueChange={(v) => onChange({ ...data, location_type: v })}
          >
            <SelectTrigger className="h-9 text-sm">
              <SelectValue placeholder="Select..." />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="Onsite">Onsite</SelectItem>
              <SelectItem value="Remote">Remote</SelectItem>
              <SelectItem value="Hybrid">Hybrid</SelectItem>
            </SelectContent>
          </Select>
        </FieldGroup>
      </div>
      <div className="grid grid-cols-3 gap-4">
        <FieldGroup label="Job ID">
          <Input value={data.job_id} onChange={set("job_id")} placeholder="31920032" />
        </FieldGroup>
        <FieldGroup label="JobDiva ID">
          <Input value={data.jobdiva_id} onChange={set("jobdiva_id")} placeholder="26-06182" />
        </FieldGroup>
        <FieldGroup label="Duration (min)">
          <Input value={data.interview_duration} onChange={set("interview_duration")} placeholder="20-25" />
        </FieldGroup>
      </div>
      <FieldGroup label="Job Description">
        <Textarea
          value={data.description}
          onChange={set("description")}
          rows={5}
          placeholder="Immediate need for a talented..."
          className="resize-none text-sm"
        />
      </FieldGroup>
    </div>
  );
}

function Step3Questions({
  questions,
  onChange,
}: {
  questions: Question[];
  onChange: (q: Question[]) => void;
}) {
  const update = (idx: number, patch: Partial<Question>) => {
    const next = questions.map((q, i) => (i === idx ? { ...q, ...patch } : q));
    onChange(next);
  };

  const remove = (idx: number) => onChange(questions.filter((_, i) => i !== idx));

  const add = () =>
    onChange([
      ...questions,
      { question_text: "", pass_criteria: "", category: "default", is_default: false },
    ]);

  return (
    <div className="space-y-3">
      {questions.length === 0 && (
        <div className="text-center py-8 text-slate-400 text-sm border-2 border-dashed border-slate-200 rounded-lg">
          No pre-screen questions yet. Add one below.
        </div>
      )}

      {questions.map((q, idx) => (
        <div
          key={idx}
          className={`border-l-4 ${CATEGORY_BORDER[q.category] || CATEGORY_BORDER.default
            } bg-white border border-slate-200 rounded-lg p-4 space-y-3 shadow-sm`}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2 flex-wrap">
              <Select
                value={q.category}
                onValueChange={(v) => update(idx, { category: v })}
              >
                <SelectTrigger className="h-7 text-[11px] w-36 font-medium px-2">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.keys(CATEGORY_COLORS).map((cat) => (
                    <SelectItem key={cat} value={cat} className="text-[12px]">
                      {cat}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <button
                onClick={() => update(idx, { is_default: !q.is_default })}
                className={`text-[11px] px-2.5 py-1 rounded-full border font-medium transition-colors ${q.is_default
                    ? "bg-indigo-100 text-indigo-700 border-indigo-200"
                    : "bg-slate-100 text-slate-500 border-slate-200"
                  }`}
              >
                {q.is_default ? "Default ✓" : "Not Default"}
              </button>
            </div>
            <button
              onClick={() => remove(idx)}
              className="text-slate-400 hover:text-red-500 transition-colors shrink-0"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
          <Textarea
            value={q.question_text}
            onChange={(e) => update(idx, { question_text: e.target.value })}
            rows={2}
            placeholder="Question text..."
            className="resize-none text-sm border-slate-200 bg-slate-50"
          />
          <Input
            value={q.pass_criteria}
            onChange={(e) => update(idx, { pass_criteria: e.target.value })}
            placeholder="Pass criteria (e.g. Must have 3+ years of React)"
            className="text-sm border-slate-200 bg-slate-50"
          />
        </div>
      ))}

      <Button
        variant="outline"
        size="sm"
        onClick={add}
        className="w-full border-dashed border-slate-300 text-slate-600 hover:border-indigo-400 hover:text-indigo-700 hover:bg-indigo-50"
      >
        <Plus className="w-4 h-4 mr-2" />
        Add Question
      </Button>
    </div>
  );
}

function Step4Review({
  json,
  loading,
  error,
  wizardState,
}: {
  json: string;
  loading: boolean;
  error: string | null;
  wizardState: WizardState;
}) {
  const [copied, setCopied] = useState(false);
  const [showJson, setShowJson] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(json);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const { candidate, job, questions } = wizardState;
  const locationParts = [job.city, job.state].filter(Boolean).join(", ");

  return (
    <div className="space-y-4">
      {/* ── Candidate Summary Card ── */}
      <div className="bg-gradient-to-br from-indigo-50 to-slate-50 border border-indigo-100 rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3">
          <div className="w-7 h-7 rounded-full bg-indigo-100 flex items-center justify-center">
            <User className="w-3.5 h-3.5 text-indigo-600" />
          </div>
          <h4 className="text-[13px] font-bold text-slate-800">Candidate</h4>
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2">
          <div className="flex items-center gap-2">
            <User className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <span className="text-[13px] text-slate-800 font-medium truncate">{candidate.name || "—"}</span>
          </div>
          <div className="flex items-center gap-2">
            <Mail className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <span className="text-[13px] text-slate-600 truncate">{candidate.email || "—"}</span>
          </div>
          <div className="flex items-center gap-2">
            <Phone className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <span className="text-[13px] text-slate-600">{candidate.phone || "—"}</span>
          </div>
          <div className="flex items-center gap-2">
            <Clock className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <span className="text-[13px] text-slate-600">{candidate.experience || "—"}</span>
          </div>
        </div>
        {candidate.summary && (
          <p className="text-[12px] text-slate-500 mt-2 line-clamp-2 border-t border-indigo-100 pt-2">
            {candidate.summary}
          </p>
        )}
      </div>

      {/* ── Job Summary Card ── */}
      <div className="bg-gradient-to-br from-emerald-50 to-slate-50 border border-emerald-100 rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3">
          <div className="w-7 h-7 rounded-full bg-emerald-100 flex items-center justify-center">
            <Briefcase className="w-3.5 h-3.5 text-emerald-600" />
          </div>
          <h4 className="text-[13px] font-bold text-slate-800">Job Details</h4>
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2">
          <div>
            <span className="text-[11px] text-slate-400 uppercase tracking-wide">Title</span>
            <p className="text-[13px] text-slate-800 font-medium truncate">{job.title || "—"}</p>
          </div>
          <div>
            <span className="text-[11px] text-slate-400 uppercase tracking-wide">Company</span>
            <p className="text-[13px] text-slate-600 truncate">{job.customer_name || "—"}</p>
          </div>
          <div className="flex items-center gap-1.5">
            <MapPin className="w-3 h-3 text-slate-400 shrink-0" />
            <span className="text-[12px] text-slate-500">{locationParts || "—"} · {job.location_type || "—"}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <Clock className="w-3 h-3 text-slate-400 shrink-0" />
            <span className="text-[12px] text-slate-500">{job.interview_duration ? `${job.interview_duration} min` : "—"}</span>
          </div>
        </div>
        <div className="flex items-center gap-3 mt-2 pt-2 border-t border-emerald-100">
          <span className="text-[11px] font-mono text-slate-400">ID: {job.job_id || "—"}</span>
          <span className="text-[11px] text-slate-300">|</span>
          <span className="text-[11px] font-mono text-slate-400">JD: {job.jobdiva_id || "—"}</span>
        </div>
      </div>

      {/* ── Questions Summary ── */}
      <div className="bg-gradient-to-br from-amber-50 to-slate-50 border border-amber-100 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-full bg-amber-100 flex items-center justify-center">
              <HelpCircle className="w-3.5 h-3.5 text-amber-600" />
            </div>
            <h4 className="text-[13px] font-bold text-slate-800">Pre-Screen Questions</h4>
          </div>
          <span className="text-[12px] font-medium text-amber-600 bg-amber-100 px-2 py-0.5 rounded-full">
            {questions.length} question{questions.length !== 1 ? "s" : ""}
          </span>
        </div>
        {questions.length > 0 ? (
          <div className="space-y-1.5 max-h-[120px] overflow-y-auto pr-1">
            {questions.map((q, idx) => (
              <div key={idx} className="flex items-start gap-2">
                <span className="text-[11px] font-bold text-slate-400 mt-0.5 shrink-0 w-4 text-right">{idx + 1}.</span>
                <p className="text-[12px] text-slate-600 leading-relaxed line-clamp-1">{q.question_text}</p>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-[12px] text-slate-400 italic">No questions configured.</p>
        )}
      </div>

      {/* ── Raw JSON (collapsible) ── */}
      <div className="border border-slate-200 rounded-xl overflow-hidden">
        <div
          onClick={() => setShowJson(!showJson)}
          className="w-full flex items-center justify-between px-4 py-2.5 bg-slate-50 hover:bg-slate-100 transition-colors text-left cursor-pointer"
          role="button"
        >
          <div className="flex items-center gap-2">
            <Code2 className="w-3.5 h-3.5 text-slate-400" />
            <span className="text-[12px] font-medium text-slate-600">Raw JSON Payload</span>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleCopy(); }} className="gap-1.5 text-slate-400 h-6 text-[11px] px-2 hover:text-slate-700">
              {copied ? (
                <><Check className="w-3 h-3 text-emerald-500" /> Copied</>
              ) : (
                <><Copy className="w-3 h-3" /> Copy</>
              )}
            </Button>
            <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform duration-200 ${showJson ? "rotate-180" : ""}`} />
          </div>
        </div>
        {showJson && (
          <div className="relative">
            <div className="absolute top-0 left-0 right-0 h-7 bg-slate-800 flex items-center px-3 gap-1.5">
              <span className="w-2 h-2 rounded-full bg-red-400" />
              <span className="w-2 h-2 rounded-full bg-amber-400" />
              <span className="w-2 h-2 rounded-full bg-emerald-400" />
              <span className="text-[10px] text-slate-500 ml-2 font-mono">payload.json</span>
            </div>
            <pre className="text-[11px] font-mono bg-slate-900 text-slate-300 overflow-auto max-h-[200px] p-4 pt-10 leading-relaxed whitespace-pre-wrap">
              {json}
            </pre>
          </div>
        )}
      </div>

      {error && (
        <div className="text-[13px] text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2 flex items-start gap-2">
          <X className="w-4 h-4 mt-0.5 shrink-0" />
          {error}
        </div>
      )}
    </div>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

export function EngageWizardModal({
  open,
  onClose,
  initialPayload,
  candidateIds,
  onSend,
  loading,
  error,
  successData,
}: EngageWizardModalProps) {
  const [step, setStep] = useState(1);
  const [wizardState, setWizardState] = useState<WizardState | null>(null);
  const [finalJson, setFinalJson] = useState("");
  const [showSuccess, setShowSuccess] = useState(false);
  const [successInterviewId, setSuccessInterviewId] = useState<string | null>(null);

  // Parse payload when modal opens
  useEffect(() => {
    if (open && initialPayload) {
      const parsed = parsePayload(initialPayload);
      setWizardState(parsed);
      setStep(1);
      setFinalJson(initialPayload);
      setShowSuccess(false);
      setSuccessInterviewId(null);
    }
  }, [open, initialPayload]);

  // Rebuild JSON whenever wizard state changes
  useEffect(() => {
    if (wizardState && initialPayload) {
      setFinalJson(buildPayload(initialPayload, wizardState));
    }
  }, [wizardState, initialPayload]);

  // Show success overlay when API confirms
  useEffect(() => {
    if (successData?.success) {
      const interviewId = successData.data?.[0]?.interview_id || null;
      setSuccessInterviewId(interviewId);
      setShowSuccess(true);
      // Auto-close after 2.5 seconds
      const timer = setTimeout(() => {
        setShowSuccess(false);
        onClose();
      }, 2500);
      return () => clearTimeout(timer);
    }
  }, [successData, onClose]);

  const handleNext = () => setStep((s) => Math.min(s + 1, 4));
  const handleBack = () => setStep((s) => Math.max(s - 1, 1));

  const handleSend = useCallback(async () => {
    await onSend(finalJson);
  }, [onSend, finalJson]);

  if (!wizardState) {
    return (
      <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
        <DialogContent className="sm:max-w-[680px]">
          <DialogHeader>
            <DialogTitle>Loading payload...</DialogTitle>
          </DialogHeader>
          <div className="py-8 flex justify-center">
            <Loader2 className="w-6 h-6 animate-spin text-indigo-500" />
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-[720px] max-h-[85vh] flex flex-col p-0 gap-0 overflow-hidden">
        {/* Header */}
        <DialogHeader className="px-6 pt-6 pb-5 border-b border-slate-100 shrink-0">
          <DialogTitle className="text-xl font-bold text-slate-900 text-center">
            Send Interview
          </DialogTitle>
          <p className="text-sm text-slate-500 mt-1 text-center">
            Review and edit before sending to the candidate.
          </p>
        </DialogHeader>

        {/* Step Indicator */}
        <div className="px-6 pt-6 pb-2 shrink-0 flex justify-center">
          <StepIndicator current={step} />
        </div>

        {/* Step Content — scrollable */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {step === 1 && (
            <Step1Candidate
              data={wizardState.candidate}
              onChange={(c) => setWizardState({ ...wizardState, candidate: c })}
            />
          )}
          {step === 2 && (
            <Step2Job
              data={wizardState.job}
              onChange={(j) => setWizardState({ ...wizardState, job: j })}
            />
          )}
          {step === 3 && (
            <Step3Questions
              questions={wizardState.questions}
              onChange={(q) => setWizardState({ ...wizardState, questions: q })}
            />
          )}
          {step === 4 && wizardState && (
            <Step4Review
              json={finalJson}
              loading={loading}
              error={error}
              wizardState={wizardState}
            />
          )}
        </div>

        {/* ── Success Overlay ── */}
        {showSuccess && (
          <div className="absolute inset-0 z-50 flex flex-col items-center justify-center bg-white/95 backdrop-blur-sm rounded-2xl animate-in fade-in duration-300">
            {/* Animated checkmark ring */}
            <div className="relative flex items-center justify-center mb-6">
              <div className="w-24 h-24 rounded-full bg-emerald-100 flex items-center justify-center">
                <div className="w-16 h-16 rounded-full bg-emerald-500 flex items-center justify-center shadow-lg shadow-emerald-200">
                  <Check className="w-8 h-8 text-white stroke-[3]" />
                </div>
              </div>
              {/* Pulse rings */}
              <div className="absolute inset-0 rounded-full border-2 border-emerald-400 animate-ping opacity-30" />
            </div>
            <h3 className="text-2xl font-bold text-slate-900 mb-2">Interview Sent!</h3>
            <p className="text-slate-500 text-sm mb-1">The interview has been scheduled successfully.</p>
            {successInterviewId && (
              <p className="text-[12px] text-slate-400 font-mono mt-1">
                ID: {successInterviewId}
              </p>
            )}
            <p className="text-xs text-slate-400 mt-6">Closing automatically…</p>
          </div>
        )}

        {/* Footer */}
        <DialogFooter className="px-6 py-4 border-t border-slate-100 shrink-0 flex justify-between sm:justify-between gap-2">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              onClick={onClose}
              className="text-slate-500"
            >
              Cancel
            </Button>
          </div>
          <div className="flex items-center gap-2">
            {step > 1 && (
              <Button variant="outline" onClick={handleBack} className="gap-1.5">
                <ChevronLeft className="w-4 h-4" />
                Back
              </Button>
            )}
            {step < 4 ? (
              <Button
                onClick={handleNext}
                className="bg-indigo-600 hover:bg-indigo-700 text-white gap-1.5"
              >
                Next
                <ChevronRight className="w-4 h-4" />
              </Button>
            ) : (
              <Button
                onClick={handleSend}
                disabled={loading}
                className="bg-indigo-600 hover:bg-indigo-700 text-white gap-2 min-w-[140px]"
              >
                {loading ? (
                  <><Loader2 className="w-4 h-4 animate-spin" /> Sending...</>
                ) : (
                  <><Send className="w-4 h-4" /> Send Interview</>
                )}
              </Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
