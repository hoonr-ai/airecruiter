"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Plus,
  Search,
  Linkedin,
  Zap,
  Star,
  Building2,
  PawPrint,
  LayoutGrid,
  Check,
  CheckCircle2,
  ChevronRight,
  Timer,
  Users,
  ArrowRight,
  Clipboard,
  Wand2,
  FileText,
  RotateCcw,
  Sparkles,
  Info,
  Save,
  Megaphone,
  Eye,
  Type,
  ArrowLeft,
  FileInput,
  CloudDownload,
  Settings
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";

type Step = 1 | 2 | 3 | 4 | 5;
type ScreeningLevel = "L1" | "L1.5" | "L2";
type EmploymentType = "W2" | "1099" | "C2C" | "Full-Time";

const STEP_LABELS = {
  1: "Intake",
  2: "Publish",
  3: "Establish Rubric",
  4: "Set Filters",
  5: "Source"
};

const STEP_DESCRIPTIONS: Record<Step, string> = {
  1: "Enter a JobDiva Job ID to get started.",
  2: "Review your PAIR-enhanced job posting and select where to publish externally.",
  3: "Define evaluation criteria and rubric for candidate assessment.",
  4: "Configure filters and requirements for candidate matching.",
  5: "Launch sourcing and begin candidate collection."
};

export default function NewJobPage() {
  const router = useRouter();
  const [currentStep, setCurrentStep] = useState<Step>(1);
  const [jobId, setJobId] = useState("");
  const [jobData, setJobData] = useState<any>(null);
  const [isFetching, setIsFetching] = useState(false);
  const [isFetched, setIsFetched] = useState(false);
  const [recruiterNotes, setRecruiterNotes] = useState("");
  const [selectedEmpTypes, setSelectedEmpTypes] = useState<EmploymentType[]>([]);
  const [recruiterEmails, setRecruiterEmails] = useState<string[]>([]);
  const [emailInput, setEmailInput] = useState("");
  const [emailError, setEmailError] = useState(false);
  const [isInputInvalid, setIsInputInvalid] = useState(false);
  const [emailErrorMessage, setEmailErrorMessage] = useState("");
  const [screeningLevel, setScreeningLevel] = useState<ScreeningLevel>("L1.5");
  const [jobTitle, setJobTitle] = useState("");
  const [jobPosting, setJobPosting] = useState("");
  const [isGeneratingJD, setIsGeneratingJD] = useState(false);
  const [isEnhancingTitle, setIsEnhancingTitle] = useState(false);
  const [isEditingJD, setIsEditingJD] = useState(false);
  const [selectedJobBoards, setSelectedJobBoards] = useState<string[]>(["LinkedIn", "Indeed"]);
  const [toast, setToast] = useState<{ message: string; type: "success" | "info" } | null>(null);
  const [pageSubtitle, setPageSubtitle] = useState(STEP_DESCRIPTIONS[1]);

  const showToast = (message: string, type: "success" | "info" = "success") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  };

  const handleFetchJob = async () => {
    if (!jobId.trim()) return;

    setIsFetching(true);
    setIsFetched(false);
    try {
      const response = await fetch("http://localhost:8001/jobs/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId.trim() })
      });

      if (!response.ok) throw new Error("Job not found");

      const data = await response.json();

      setJobData(data); // Store the full data object from backend

      const displayData = {
        title: data.title,
        customer: data.customer_name || data.customer,
        location: `${data.city || ""}, ${data.state || ""}`.trim() || "Remote",
        openings: data.openings || "1",
        type: data.employment_type || "Full-Time",
        rate: data.pay_rate || "Market Rate",
        startDate: data.start_date || "ASAP",
        postedDate: data.posted_date || new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }),
        description: data.description
      };

      setJobTitle(data.title || "");
      setJobPosting(data.description || "");
      setPageSubtitle(`${displayData.title} · ${displayData.customer}`);
      setIsFetched(true);
      showToast("Job data loaded from JobDiva.", "success");
    } catch (error) {
      console.error("Error fetching job:", error);
      showToast("Failed to fetch job. Check the Job ID.", "info");
    } finally {
      setIsFetching(false);
    }
  };

  const handleEnhanceJob = async (titleOverride?: string, descOverride?: string, notesOverride?: string) => {
    setIsGeneratingJD(true);
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8001";
      const response = await fetch(`${apiUrl}/api/v1/gemini/jobs/${jobId || 'new'}/generate-description`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobTitle: titleOverride || jobTitle,
          jobDescription: descOverride || jobData?.description || jobPosting,
          jobNotes: notesOverride === undefined ? recruiterNotes : notesOverride,
          workAuthorization: selectedEmpTypes.join(", ")
        })
      });

      if (!response.ok) {
        const errorText = await response.text();
        console.error("Enhance failed:", errorText);
        throw new Error("Failed to generate JD");
      }
      
      const data = await response.json();
      setJobPosting(data.description);
      showToast("AI Job Description enriched!", "success");
    } catch (error) {
      console.error("Enhance error:", error);
      showToast("AI enhancement failed. Please try again.", "info");
    } finally {
      setIsGeneratingJD(false);
    }
  };

  const handleEnhanceTitle = async () => {
    if (!jobTitle) return;
    setIsEnhancingTitle(true);
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8001";
      const res = await fetch(`${apiUrl}/api/v1/gemini/jobs/generate-title`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
          jobTitle, 
          jobNotes: recruiterNotes,
          jobDescription: jobPosting 
        })
      });
      if (res.ok) {
        const data = await res.json();
        setJobTitle(data.title);
        showToast("Title enhanced by PAIR.", "success");
      } else {
        const err = await res.text();
        console.error("Title enhance failed:", err);
        showToast("Failed to enhance title.", "info");
      }
    } catch (e) {
      console.error(e);
    } finally {
      setIsEnhancingTitle(false);
    }
  };

  const handleAddEmail = () => {
    const trimmed = emailInput.trim();
    if (trimmed && /^\S+@\S+\.\S+$/.test(trimmed) && !recruiterEmails.includes(trimmed)) {
      setRecruiterEmails([...recruiterEmails, trimmed]);
      setEmailInput("");
      setEmailError(false);
      setIsInputInvalid(false);
      setEmailErrorMessage("");
    } else if (trimmed && !/^\S+@\S+\.\S+$/.test(trimmed)) {
      setIsInputInvalid(true);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === "," || e.key === ";") {
      e.preventDefault();
      handleAddEmail();
    }
  };

  const removeEmail = (email: string) => {
    setRecruiterEmails(recruiterEmails.filter(e => e !== email));
  };

  const toggleEmpType = (type: EmploymentType) => {
    setSelectedEmpTypes(prev =>
      prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type]
    );
  };

  const toggleJobBoard = (board: string) => {
    setSelectedJobBoards(prev =>
      prev.includes(board) ? prev.filter(b => b !== board) : [...prev, board]
    );
  };

  const StepIndicator = () => (
    <div className="flex items-start mb-8 relative">
      {Object.entries(STEP_LABELS).map(([step, label], index) => {
        const stepNumber = parseInt(step) as Step;
        const isActive = stepNumber === currentStep;
        const isCompleted = stepNumber < currentStep;
        const isClickable = stepNumber <= currentStep || (stepNumber === currentStep + 1 && !!jobData);
        const isLast = index === Object.keys(STEP_LABELS).length - 1;

        return (
          <div key={step} className="flex-1 flex flex-col items-center relative z-10">
            <div
              className={`flex flex-col items-center w-full ${isClickable ? "cursor-pointer" : "cursor-not-allowed"}`}
              onClick={() => isClickable && setCurrentStep(stepNumber)}
            >
              <div className="relative flex items-center justify-center w-full mb-3">
                {/* Connector Line — pinned perfectly between bubbles */}
                {!isLast && (
                  <div
                    className={`absolute top-1/2 left-[calc(50%+24px)] right-[-50%] h-[2.5px] -translate-y-1/2 -z-10 transition-colors duration-300 ${isCompleted ? "bg-[#10b981]" : "bg-slate-200"}`}
                  />
                )}

                <div className={`
                  w-10 h-10 rounded-full flex items-center justify-center text-[15px] font-bold transition-all duration-300 relative z-10
                  ${isActive ? "bg-primary text-white shadow-[0_0_0_8px_rgba(99,102,241,0.12)]" : ""}
                  ${isCompleted ? "bg-[#10b981] text-white" : ""}
                  ${!isActive && !isCompleted ? "bg-slate-200 text-slate-500" : ""}
                `}>
                  {isCompleted ? <Check className="w-5 h-5 stroke-[3]" /> : stepNumber}
                </div>
              </div>
              <span className={`text-[12px] font-medium transition-colors duration-200 whitespace-nowrap text-center
                ${isActive ? "text-primary" : ""}
                ${isCompleted ? "text-[#10b981]" : ""}
                ${!isActive && !isCompleted ? "text-slate-400" : ""}
              `}>
                {label}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );

  // Helper component to format AI-generated postings with rich text rendering
const AIPostingJobDescription = ({ text }: { text: string }) => {
  const renderInline = (content: string) => {
    // Parse [text](url), **bold** and *italic*
    const parts = content.split(/(\[.*?\]\(.*?\)+|\*\*.*?\*\*|\*(?!\*).*?\*(?!\*))/g);
    return parts.map((part, i) => {
      if (part.startsWith('[') && part.includes('](') && part.endsWith(')')) {
        const match = part.match(/\[(.*?)\]\((.*?)\)/);
        if (match) {
          return (
            <a key={i} href={match[2]} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">
              {match[1]}
            </a>
          );
        }
      } else if (part.startsWith('**') && part.endsWith('**')) {
        return <strong key={i} className="font-semibold text-slate-900">{part.slice(2, -2)}</strong>;
      } else if (part.startsWith('*') && part.endsWith('*')) {
        return <em key={i} className="italic text-slate-800">{part.slice(1, -1)}</em>;
      }
      return <span key={i}>{part}</span>;
    });
  };

  const formatLines = (rawText: string) => {
    if (!rawText) return null;
    return rawText.split('\n').map((line, index) => {
      const trimmedLine = line.trim();
      if (!trimmedLine) return <div key={index} className="h-2" />;

      // Header check: starts with bold all caps or is just an all caps line
      const isHeader = /^\*\*[A-Z\s]+\*\*$/.test(trimmedLine) || /^[A-Z\s]{3,25}$/.test(trimmedLine);
      if (isHeader) {
        const title = trimmedLine.replace(/\*\*/g, '').trim();
        return (
          <div key={index} className="text-[15px] font-semibold text-slate-900 mt-5 mb-2 first:mt-0 uppercase tracking-tight">
            {title}
          </div>
        );
      }

      // Bullet points
      if (trimmedLine.startsWith('•') || trimmedLine.startsWith('-')) {
        const content = trimmedLine.replace(/^[•-]\s*/, '').trim();
        return (
          <div key={index} className="flex gap-2.5 ml-1 my-1.5 items-start">
            <span className="text-slate-400 mt-1">•</span>
            <div className="flex-1">{renderInline(content)}</div>
          </div>
        );
      }

      return (
        <div key={index} className="mb-2 text-slate-600 leading-relaxed">
          {renderInline(trimmedLine)}
        </div>
      );
    });
  };

  return <div className="text-[13.5px] font-normal">{formatLines(text)}</div>;
};

const intakeStep = (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      {/* Card Header — reference style: no heavy background, very subtle gradient */}
      <div className="flex flex-row items-start gap-4 px-7 py-6 border-b border-slate-100"
        style={{ background: "linear-gradient(135deg, #f8f7ff 0%, #ffffff 60%)" }}>
        <FileInput className="w-[22px] h-[22px] text-primary mt-0.5 flex-shrink-0" />
        <div>
          <h2 className="text-[20px] font-semibold text-slate-900 leading-tight tracking-tight">Intake</h2>
          <p className="text-slate-500 text-[14px] mt-1 leading-relaxed">Fetch job details from JobDiva, then add any additional context for PAIR.</p>
        </div>
      </div>

      <div className="p-7 space-y-7">
        {/* JobDiva Job ID */}
        <div>
          <label className="block text-[14px] font-medium text-slate-900 mb-3">JobDiva Job ID</label>
          <div className="flex items-center gap-3">
            <Input
              placeholder="e.g. 26-08025"
              value={jobId}
              onChange={(e) => setJobId(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleFetchJob()}
              className="max-w-[180px] h-[36px] bg-white border-slate-200 text-[13px]"
            />
            <button
              onClick={handleFetchJob}
              disabled={!jobId.trim() || isFetching}
              className={`h-[36px] px-3.5 rounded-lg flex items-center gap-2 text-[13px] font-medium transition-all text-white disabled:opacity-50 disabled:cursor-not-allowed ${isFetched ? "bg-[#16a34a]" : "bg-primary hover:bg-[#5b21b6]"}`}
            >
              {isFetching ? (
                <>
                  <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Fetching...
                </>
              ) : isFetched ? (
                <>
                  <Check className="w-4 h-4" />
                  Fetched
                </>
              ) : (
                <>
                  <CloudDownload className="w-4 h-4" />
                  Fetch from JobDiva
                </>
              )}
            </button>
          </div>
        </div>

        {jobData && (
          <>
            {/* Data Grid — 3 column, bordered box, reference spec */}
            <div className="border-t border-slate-100 pt-6">
              <div
                className="grid grid-cols-3 gap-y-5 gap-x-6 p-5 rounded-lg mb-6"
                style={{ background: "#f8fafc", border: "1px solid #e2e8f0" }}
              >
                {[
                  { label: "Job Title", value: jobData.title },
                  { label: "Customer", value: jobData.customer_name || jobData.customer },
                  {
                    label: "Location",
                    value: `${jobData.city || ""}, ${jobData.state || ""}`.trim() + (jobData.location_type ? ` (${jobData.location_type})` : "") || "Remote"
                  },
                  { label: "Openings", value: jobData.openings },
                  { label: "Employment Type", value: jobData.employment_type },
                  { label: "Pay Rate", value: jobData.pay_rate },
                  { label: "Job Start Date", value: jobData.start_date },
                  { label: "Job Posted Date", value: jobData.posted_date },
                ].map(({ label, value }) => (
                  <div key={label} className="flex flex-col gap-1">
                    <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-400">{label}</span>
                    <span className="text-[14px] font-medium text-slate-900 truncate" title={value?.toString()}>{value}</span>
                  </div>
                ))}
              </div>

              {/* Job Description */}
              <div className="mb-5">
                <label className="block text-[14px] font-medium text-slate-900 mb-2">
                  Job Description{" "}
                  <span className="text-slate-500 font-normal ml-1">— pulled from JobDiva</span>
                </label>
                <div
                  className="rounded-md p-4 text-[13px] text-slate-900 leading-[1.75] max-h-[180px] overflow-y-auto whitespace-pre-wrap"
                  style={{ background: "#f8fafc", border: "1px solid #e2e8f0" }}
                >
                  {jobData.description}
                </div>
              </div>

              {/* Recruiter Notes */}
              <div className="mb-10">
                <label className="flex items-center gap-1.5 text-[14px] font-medium text-slate-900 mb-2">
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 text-primary"><path d="M17.414 2.586a2 2 0 00-2.828 0L7 10.172V13h2.828l7.586-7.586a2 2 0 000-2.828z" /><path fillRule="evenodd" d="M2 6a2 2 0 012-2h4a1 1 0 010 2H4v10h10v-4a1 1 0 112 0v4a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" clipRule="evenodd" /></svg>
                  Recruiter Notes
                  <span className="text-slate-500 font-normal ml-0.5">— helps PAIR generate better output</span>
                </label>
                <Textarea
                  placeholder="e.g. Client strongly prefers fintech background. Must be local to Atlanta metro — no relocation. W2 only, no C2C. Ideally someone with NetSuite over SAP. Start date is flexible but ASAP preferred..."
                  value={recruiterNotes}
                  onChange={(e) => setRecruiterNotes(e.target.value)}
                  rows={3}
                  className="text-[14px] border-slate-200 resize-y min-h-[100px]"
                />
              </div>

              {/* Employment Type */}
              <div className="mb-5">
                <label className="block text-[14px] font-medium text-slate-900 mb-1">Employment Type</label>
                <p className="text-[13px] text-slate-500 mb-3">Select all that apply for this role.</p>
                <div className="flex flex-wrap gap-2">
                  {(["W2", "1099", "C2C", "Full-Time"] as EmploymentType[]).map(type => (
                    <button
                      key={type}
                      onClick={() => toggleEmpType(type)}
                      className={`px-4 py-1.5 rounded-full border text-[13px] font-medium transition-all cursor-pointer ${selectedEmpTypes.includes(type)
                          ? "bg-primary border-primary text-white"
                          : "bg-white border-slate-300 text-slate-500 hover:border-primary hover:text-primary"
                        }`}
                    >
                      {type}
                    </button>
                  ))}
                </div>
              </div>

              <div className="border-t border-slate-100 my-6" />

              {/* PAIR Setup Section */}
              <div className="flex items-center gap-2 mb-5">
                <Settings className="w-5 h-5 text-slate-700 flex-shrink-0" />
                <span className="text-[14px] font-bold text-slate-900">PAIR Setup</span>
                <span className="text-[12px] text-slate-500 font-normal">Configure your screening before proceeding</span>
              </div>

              <div className="mb-7">
                <label className="block text-[14px] font-medium text-slate-900 mb-2">
                  Recruiter Email(s) <span className="text-red-500">*</span>
                </label>
                <div
                  className={`flex flex-wrap items-center gap-1.5 border rounded-lg px-2.5 py-1.5 min-h-[44px] max-w-[480px] bg-white cursor-text transition-colors ${emailError || isInputInvalid ? 'border-red-400' : 'border-slate-200 focus-within:border-primary'}`}
                  onClick={() => document.getElementById('recruiter-email-input')?.focus()}
                >
                  {recruiterEmails.map(email => (
                    <span key={email} className="inline-flex items-center gap-1.5 bg-[#eff6ff] text-[#2563eb] text-[12px] font-medium px-3 py-1 rounded-full border border-[#bfdbfe]">
                      {email}
                      <button onClick={(e) => { e.stopPropagation(); removeEmail(email); }} className="text-[#3b82f6] hover:text-[#1d4ed8] ml-0.5 font-bold text-[14px]">×</button>
                    </span>
                  ))}
                  <input
                    id="recruiter-email-input"
                    type="text"
                    placeholder="you@pyramidci.com"
                    value={emailInput}
                    onChange={(e) => {
                      const val = e.target.value;
                      setEmailInput(val);
                      if (val) {
                        const trimmed = val.trim();
                        if (!trimmed.includes("@")) {
                          setIsInputInvalid(true);
                          setEmailErrorMessage("The @ symbol is missing.");
                        } else {
                          const atParts = trimmed.split("@");
                          const domain = atParts[1];
                          if (!domain || domain.trim() === "") {
                            setIsInputInvalid(true);
                            setEmailErrorMessage("Domain name is missing.");
                          } else {
                            const domainParts = domain.split(".");
                            const tld = domainParts[domainParts.length - 1];
                            const domainBody = domainParts.slice(0, -1).join('.');
                            if (domainParts.length < 2 || !domainBody || !/^[a-zA-Z]{2,6}$/.test(tld)) {
                              setIsInputInvalid(true);
                              setEmailErrorMessage("Suffix is missing or invalid (e.g. .com, .org).");
                            } else {
                              setIsInputInvalid(false);
                              setEmailErrorMessage("");
                            }
                          }
                        }
                      } else {
                        setIsInputInvalid(false);
                        setEmailErrorMessage("");
                      }
                    }}
                    onKeyDown={handleKeyPress}
                    onBlur={handleAddEmail}
                    className="flex-1 min-w-[200px] border-none outline-none text-[14px] bg-transparent py-1 placeholder:text-slate-400"
                  />
                  {emailInput && (
                    <span className="flex items-center gap-1.5 ml-auto text-[10px] font-bold uppercase tracking-wider pr-1">
                      {!isInputInvalid ? (
                        <>
                          <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />
                          <span className="text-green-600">Valid</span>
                        </>
                      ) : (
                        <>
                          <span className="text-red-500">Invalid</span>
                        </>
                      )}
                    </span>
                  )}
                </div>
                {isInputInvalid && <p className="text-[11px] text-red-500 mt-1">{emailErrorMessage}</p>}
                <p className="text-[12px] text-slate-500 mt-1.5">Press comma, semicolon, or Enter to add. You'll be notified when candidates complete the PAIR screen.</p>
              </div>

              {/* Screening Level */}
              <div>
                <label className="block text-[14px] font-medium text-slate-900 mb-1">Screening Level</label>
                <p className="text-[13px] text-slate-500 mb-4">How deeply should PAIR screen each candidate?</p>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {/* L1 */}
                  <div
                    className={`flex-1 border-2 rounded-[10px] p-4 cursor-pointer transition-all ${screeningLevel === "L1" ? "border-primary bg-[#f5f3ff]" : "border-slate-200 hover:border-primary"}`}
                    onClick={() => setScreeningLevel("L1")}
                  >
                    <div className="flex items-center gap-2 mb-3 flex-wrap">
                      <span className="inline-flex items-center justify-center px-2.5 py-0.5 rounded-full text-[11px] font-bold tracking-wide bg-[#ede9fe] text-[#5b21b6]">L1</span>
                      <span className="font-semibold text-[14px] text-slate-900">Basic Screen</span>
                    </div>
                    <div className="flex flex-col gap-1.5 text-[12px]">
                      <p className="flex items-start gap-1.5 text-slate-500"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><circle cx="12" cy="12" r="10" strokeWidth="2" /><polyline points="12 6 12 12 16 14" strokeWidth="2" /></svg> ~4–8 min call</p>
                      <p className="flex items-start gap-1.5 text-slate-500 leading-snug"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg> Availability, location, work authorization, compensation, 1–2 skills-fit questions</p>
                      <p className="flex items-start gap-1.5 text-[#166534] font-medium"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg> Higher volume of candidates collected</p>
                      <p className="flex items-start gap-1.5 text-[#6b7280]"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM7 9a1 1 0 000 2h6a1 1 0 100-2H7z" clipRule="evenodd" /></svg> Less qualifying detail per candidate</p>
                    </div>
                  </div>

                  {/* L1.5 */}
                  <div
                    className={`flex-1 border-2 rounded-[10px] p-4 cursor-pointer transition-all ${screeningLevel === "L1.5" ? "border-primary bg-[#f5f3ff]" : "border-slate-200 hover:border-primary"}`}
                    onClick={() => setScreeningLevel("L1.5")}
                  >
                    <div className="flex items-center gap-2 mb-3 flex-wrap">
                      <span className="inline-flex items-center justify-center px-2.5 py-0.5 rounded-full text-[11px] font-bold tracking-wide bg-[#ede9fe] text-[#5b21b6]">L1.5</span>
                      <span className="font-semibold text-[14px] text-slate-900">Standard Screen</span>
                      <span className="text-[11px] bg-[#dcfce7] text-[#166534] px-2 py-0.5 rounded-full font-semibold">Recommended</span>
                    </div>
                    <div className="flex flex-col gap-1.5 text-[12px]">
                      <p className="flex items-start gap-1.5 text-slate-500"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><circle cx="12" cy="12" r="10" strokeWidth="2" /><polyline points="12 6 12 12 16 14" strokeWidth="2" /></svg> ~8–12 min call</p>
                      <p className="flex items-start gap-1.5 text-slate-500 leading-snug"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg> All L1 questions + 1–2 more skills-fit questions + probing</p>
                      <p className="flex items-start gap-1.5 text-[#166534] font-medium"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg> Best balance of depth and candidate volume</p>
                      <p className="flex items-start gap-1.5 text-[#6b7280]"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM7 9a1 1 0 000 2h6a1 1 0 100-2H7z" clipRule="evenodd" /></svg> Moderate drop-off vs. L1</p>
                    </div>
                  </div>

                  {/* L2 */}
                  <div
                    className={`flex-1 border-2 rounded-[10px] p-4 cursor-pointer transition-all ${screeningLevel === "L2" ? "border-primary bg-[#f5f3ff]" : "border-slate-200 hover:border-primary"}`}
                    onClick={() => setScreeningLevel("L2")}
                  >
                    <div className="flex items-center gap-2 mb-3 flex-wrap">
                      <span className="inline-flex items-center justify-center px-2.5 py-0.5 rounded-full text-[11px] font-bold tracking-wide bg-[#dcfce7] text-[#166534]">L2</span>
                      <span className="font-semibold text-[14px] text-slate-900">Deep Screen</span>
                    </div>
                    <div className="flex flex-col gap-1.5 text-[12px]">
                      <p className="flex items-start gap-1.5 text-slate-500"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><circle cx="12" cy="12" r="10" strokeWidth="2" /><polyline points="12 6 12 12 16 14" strokeWidth="2" /></svg> ~12–16 min call</p>
                      <p className="flex items-start gap-1.5 text-slate-500 leading-snug"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg> All L1.5 topics + 1–2 more skills/cultural fit questions</p>
                      <p className="flex items-start gap-1.5 text-[#166534] font-medium"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg> Richest candidate profiles, highest fit accuracy</p>
                      <p className="flex items-start gap-1.5 text-[#6b7280]"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM7 9a1 1 0 000 2h6a1 1 0 100-2H7z" clipRule="evenodd" /></svg> Fewest completions — best for niche or senior roles</p>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );

  const publishStep = (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      <div className="flex flex-row items-start gap-4 px-7 py-6 border-b border-slate-100"
        style={{ background: "linear-gradient(135deg, #f8f7ff 0%, #ffffff 60%)" }}>
        <Megaphone className="w-[22px] h-[22px] text-primary mt-0.5 flex-shrink-0" />
        <div>
          <h2 className="text-[20px] font-medium text-slate-900 leading-tight tracking-tight">Publish</h2>
          <p className="text-slate-500 text-[14px] mt-1 leading-relaxed">Review your PAIR-enhanced job posting and select where to publish externally.</p>
        </div>
      </div>
      <div className="p-7">
        <div className="flex flex-col lg:flex-row gap-6 items-start">
          <div className="flex-1 w-full relative">
            {/* Job Title Section */}
            <div className="mb-6">
              <label className="block text-[14px] font-bold text-slate-900 mb-2 ml-1">Job Title</label>
              <div className="flex items-center gap-3">
                <Input
                  value={jobTitle}
                  onChange={(e) => setJobTitle(e.target.value)}
                  placeholder="Job Title"
                  className="h-10 text-[14px] border-slate-200 focus:border-primary/50 focus:ring-primary/20 bg-white"
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleEnhanceTitle}
                  disabled={isEnhancingTitle}
                  className="h-10 px-4 flex items-center gap-2 border-slate-200 bg-white text-slate-900 hover:text-black shadow-sm text-[13px] font-bold rounded-lg disabled:opacity-50"
                >
                  <Sparkles className={`w-3.5 h-3.5 text-slate-900 ${isEnhancingTitle ? 'animate-pulse' : ''}`} />
                  {isEnhancingTitle ? 'Enhancing...' : 'Enhance'}
                </Button>
              </div>
              <p className="text-[11.5px] text-slate-400 mt-2 ml-1 font-normal italic">
                Pre-filled from JobDiva. Edit or enhance for external posting.
              </p>
            </div>

          <div className="flex items-center justify-between mb-3 mt-8">
            <div className="bg-[#eef2ff] text-[#4f46e5] flex items-center gap-1.5 px-3 py-1 rounded-full text-[11.5px] font-medium border border-[#e0e7ff]">
              <Sparkles className="w-3.5 h-3.5" />
              PAIR-Enhanced Job Posting
            </div>
            <Button 
              variant="outline" 
              size="sm" 
              onClick={() => handleEnhanceJob()}
              disabled={isGeneratingJD}
              className="h-9 px-4 flex items-center gap-2 border-slate-200 bg-white text-slate-900 shadow-sm text-[13px] font-bold rounded-xl hover:bg-slate-50 transition-all disabled:opacity-50"
            >
              <RotateCcw className={`w-3.5 h-3.5 text-slate-900 ${isGeneratingJD ? 'animate-spin' : ''}`} />
              {isGeneratingJD ? 'Regenerating...' : 'Regenerate'}
            </Button>
          </div>
          
          {isEditingJD ? (
            <div className="relative group">
              <textarea
                autoFocus
                value={jobPosting}
                onChange={(e) => setJobPosting(e.target.value)}
                onBlur={() => setIsEditingJD(false)}
                className="w-full bg-white border-2 border-primary/40 rounded-lg p-7 h-[500px] overflow-y-auto scrollbar-thin scrollbar-thumb-slate-200 text-[13.5px] font-normal leading-relaxed text-slate-900 focus-visible:outline-none focus:ring-4 focus:ring-primary/10 transition-all resize-none"
                placeholder="Edit Markdown here..."
              />
              <div className="absolute top-4 right-4 bg-primary text-white text-[11px] font-bold px-3 py-1.5 rounded-md shadow-md pointer-events-none animate-in fade-in duration-200">
                Click outside to save & preview
              </div>
            </div>
          ) : (
            <div 
              onClick={() => setIsEditingJD(true)}
              title="Click to edit job description"
              className="bg-slate-50/50 border border-slate-200 rounded-lg p-7 h-[500px] overflow-y-auto scrollbar-thin scrollbar-thumb-slate-200 text-[13.5px] font-normal leading-relaxed text-slate-900 cursor-text hover:border-primary/40 hover:bg-white transition-colors group relative"
            >
              <div className="absolute top-4 right-4 bg-slate-200 text-slate-600 text-[11px] font-bold px-3 py-1.5 rounded-md shadow-sm opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none">
                Click anywhere to edit
              </div>
              <AIPostingJobDescription text={jobPosting} />
            </div>
          )}
        </div>

          <div className="w-full lg:w-[240px] flex-shrink-0">
            <label className="block text-[15px] font-bold text-slate-900 mb-4 ml-1">Publish To</label>
            <div className="flex flex-col border border-slate-200 rounded-2xl bg-[#F8FAFC] p-2 shadow-sm">
              {[
                { name: "LinkedIn", icon: <Linkedin className="w-4 h-4 text-[#0A66C2]" /> },
                { name: "Indeed", icon: <Search className="w-4 h-4 text-[#2164f3]" /> },
                { name: "Dice", icon: <LayoutGrid className="w-4 h-4 text-[#1565c0]" /> },
                { name: "ZipRecruiter", icon: <Zap className="w-4 h-4 text-[#00873E]" /> },
                { name: "Glassdoor", icon: <Star className="w-4 h-4 text-[#0caa41]" /> },
                { name: "Monster", icon: <PawPrint className="w-4 h-4 text-[#6d1f7e]" /> },
                { name: "CareerBuilder", icon: <Building2 className="w-4 h-4 text-[#00a4bd]" /> },
              ].map(board => (
                <label key={board.name} className="flex items-center gap-3 p-2.5 hover:bg-white hover:shadow-sm cursor-pointer transition-all rounded-xl group/item">
                  <Checkbox
                    checked={selectedJobBoards.includes(board.name)}
                    onCheckedChange={() => toggleJobBoard(board.name)}
                    className="w-[18px] h-[18px] rounded-full border-slate-300 data-[state=checked]:bg-[#4f46e5] data-[state=checked]:border-[#4f46e5] transition-all"
                  />
                  <div className="flex items-center gap-3">
                    <div className="transition-transform group-hover/item:scale-110 duration-200">
                      {board.icon}
                    </div>
                    <span className="text-[14px] font-medium text-slate-700 group-hover/item:text-slate-900 transition-colors">
                      {board.name}
                    </span>
                  </div>
                </label>
              ))}
            </div>
            <div className="flex items-start gap-2 mt-5 px-1">
              <Info className="w-4 h-4 text-slate-400 mt-0.5 flex-shrink-0" />
              <p className="text-[12px] text-slate-500 leading-snug font-medium">
                Job posting team will receive your request to post after you Launch PAIR.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  const PlaceholderStep = ({ stepNumber, title }: { stepNumber: number; title: string }) => (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      <div className="p-12 text-center">
        <div className="text-4xl text-gray-300 mb-4">🚧</div>
        <h3 className="text-lg font-semibold text-gray-800 mb-2">Step {stepNumber}: {title}</h3>
        <p className="text-gray-500">This step is coming soon. Continue with the previous steps to set up your job.</p>
      </div>
    </div>
  );

  const renderStepContent = () => {
    switch (currentStep) {
      case 1: return intakeStep;
      case 2: return publishStep;
      case 3: return <PlaceholderStep stepNumber={3} title="Establish Rubric" />;
      case 4: return <PlaceholderStep stepNumber={4} title="Set Filters" />;
      case 5: return <PlaceholderStep stepNumber={5} title="Launch & Source" />;
      default: return null;
    }
  };

  return (
    <div className="p-8 max-w-7xl mx-auto animate-in fade-in slide-in-from-bottom-4 duration-500">
      {/* Breadcrumb */}
      <div className="mb-5">
        <Link href="/jobs" className="text-slate-500 hover:text-slate-700 text-[14px] flex items-center gap-2 transition-colors">
          <ArrowLeft className="w-4 h-4" />
          Back to Jobs
        </Link>
      </div>

      {/* Page Header */}
      <div className="mb-7">
        <h1 className="text-[28px] font-bold text-slate-900 leading-none">New Job</h1>
        {currentStep !== 2 && <p className="text-slate-500 text-[14px] mt-2">{pageSubtitle}</p>}
      </div>

      {/* Step Indicator */}
      <StepIndicator />

      {/* Step Content */}
      {renderStepContent()}

      {/* Wizard Navigation — reference spec: Back | Save & Exit … Next */}
      <div className="flex items-center justify-between pt-5 border-t border-slate-100 mt-2">
        <div className="flex items-center gap-3">
          {currentStep > 1 && (
            <Button
              variant="outline"
              className="h-11 px-5 border-slate-200 text-slate-700 font-semibold shadow-sm hover:bg-slate-50 flex items-center gap-2"
              onClick={() => setCurrentStep((currentStep - 1) as Step)}
            >
              <ArrowLeft className="w-4 h-4" />
              Back
            </Button>
          )}
          <Button
            variant="outline"
            className="h-11 px-5 border-slate-200 text-slate-700 font-semibold shadow-sm hover:bg-slate-50 flex items-center gap-2"
            onClick={async () => {
              if (!jobData) {
                showToast("Fetch a job first before saving.", "info");
                return;
              }
              if (recruiterEmails.length === 0) {
                setEmailError(true);
                showToast("Recruiter Email is required.", "info");
                return;
              }
              try {
                const payload = {
                  ...jobData,
                  recruiter_email: recruiterEmails[0] || "",
                  job_notes: recruiterNotes,
                };
                const response = await fetch(`http://localhost:8001/jobs/${jobData.id || jobId}/save`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(payload)
                });
                if (response.ok) {
                  showToast("Job saved successfully!", "success");
                  setTimeout(() => router.push('/jobs'), 1000);
                }
              } catch (e) { console.error(e); }
            }}
          >
            <Save className="w-4 h-4 text-slate-400" />
            Save & Exit
          </Button>
        </div>

        <div>
          <Button
            className="h-11 px-6 bg-primary hover:bg-primary/90 flex items-center gap-2 shadow-sm text-[15px] font-semibold text-white transition-all active:scale-95"
            onClick={async () => {
               if (currentStep === 1) {
                if (!jobData) {
                  showToast("Fetch a job first before saving.", "info");
                  return;
                }
                if (recruiterEmails.length === 0) {
                  setEmailError(true);
                  showToast("Recruiter Email is required.", "info");
                  return;
                }

                // AI Enhancement & Save before moving to next step
                try {
                  const payload = {
                    ...jobData,
                    recruiter_email: recruiterEmails[0] || "",
                    job_notes: recruiterNotes,
                  };
                  
                  // Save locally
                  await fetch(`http://localhost:8001/jobs/${jobData.id || jobId}/save`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                  });

                  // Trigger Enhancement
                  await handleEnhanceJob();
                  
                } catch (e) {
                  console.error("Next step preparation error:", e);
                }
              }
              if (currentStep < 5) setCurrentStep((currentStep + 1) as Step);
            }}
            disabled={(currentStep === 1 && !jobData) || isGeneratingJD}
          >
            {isGeneratingJD ? (
              <>
                <span className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                Enriching...
              </>
            ) : (
              <>
                {currentStep === 5 ? "Complete Setup" : "Next"}
                <ArrowRight className="w-5 h-5 ml-1" />
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Toast Notification */}
      {toast && (
        <div
          className={`fixed bottom-8 right-8 flex items-center gap-2.5 px-5 py-3 rounded-lg text-[14px] font-medium text-white shadow-xl z-50 transition-all duration-300 ${toast.type === "success" ? "bg-[#166534]" : "bg-primary"}`}
        >
          {toast.type === "success" ? (
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5 flex-shrink-0"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg>
          ) : (
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5 flex-shrink-0"><path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" /></svg>
          )}
          {toast.message}
        </div>
      )}
    </div>
  );
}