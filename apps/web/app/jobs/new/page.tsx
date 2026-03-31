"use client";

import { useState, useEffect, Suspense } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
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
  Settings,
  ListChecks,
  ChevronUp,
  ChevronDown,
  GraduationCap,
  UserCheck,
  Lightbulb,
  X,
  Filter
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";

// Utility function to clean location_type values and filter out employment terms
function cleanLocationType(locationType: string | null | undefined): string {
  if (!locationType) return "";

  const employmentTerms = [
    "direct placement", "contract", "full-time", "part-time",
    "w2", "1099", "c2c", "corp to corp", "open", "pending",
    "temporary", "permanent", "temp to perm", "fulltime", "parttime",
    "consultant", "consulting", "employee", "contractor"
  ];

  const cleanType = locationType.toLowerCase().trim();

  // If the location type contains any employment terms, return empty string
  if (employmentTerms.some(term => cleanType.includes(term))) {
    return "";
  }

  // Return the original value if it's clean
  return locationType.trim();
}

type Step = 1 | 2 | 3 | 4 | 5;
type ScreeningLevel = "L1" | "L1.5" | "L2";
type EmploymentType = "W2" | "1099" | "C2C" | "Full-Time";
type ScreenQuestion = {
  id: number;
  question_text: string;
  pass_criteria: string;
  is_default: boolean;
  category: string;
  order_index: number;
};

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
  return (
    <Suspense fallback={<div>Loading...</div>}>
      <NewJobPageContent />
    </Suspense>
  );
}

function NewJobPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [currentStep, setCurrentStep] = useState<Step>(1);
  const [numericJobId, setNumericJobId] = useState("");
  const [jobdivaId, setJobdivaId] = useState("");
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
  const [jobTitle, setJobTitle] = useState("");
  const [enhancedTitle, setEnhancedTitle] = useState("");
  const [jobPosting, setJobPosting] = useState("");
  const [isGeneratingJD, setIsGeneratingJD] = useState(false);
  const [isEnhancingTitle, setIsEnhancingTitle] = useState(false);
  const [isEditingJD, setIsEditingJD] = useState(false);
  const [selectedJobBoards, setSelectedJobBoards] = useState<string[]>([]);
  const [screeningLevel, setScreeningLevel] = useState<ScreeningLevel>("L1.5");
  const [toast, setToast] = useState<{ message: string; type: "success" | "info" } | null>(null);
  const [pageSubtitle, setPageSubtitle] = useState(STEP_DESCRIPTIONS[1]);
  const [rubricData, setRubricData] = useState<any>(null);
  const [isGeneratingRubric, setIsGeneratingRubric] = useState(false);
  const [workAuthorization, setWorkAuthorization] = useState("");

  // Step 4 - Set Filters state
  const [resumeMatchFilters, setResumeMatchFilters] = useState<Array<{
    id: number;
    category: string;
    value: string;
    active: boolean;
    ai: boolean;
    fromRubric: boolean;
  }>>([]);
  const [filterIdCounter, setFilterIdCounter] = useState(1);
  // Step 4 - Phone Screen state
  const [botIntroduction, setBotIntroduction] = useState("");
  const [screenQuestions, setScreenQuestions] = useState<ScreenQuestion[]>([]);
  const [questionIdCounter, setQuestionIdCounter] = useState(1);

  useEffect(() => {
    const jobIdFromUrl = searchParams.get("jobId");
    if (jobIdFromUrl) {
      if (jobIdFromUrl.includes("-")) {
        setJobdivaId(jobIdFromUrl);
      } else {
        setNumericJobId(jobIdFromUrl);
      }
      loadJobDraft(jobIdFromUrl);
    }
  }, [searchParams]);

  const showToast = (message: string, type: "success" | "info" = "success") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  };

  const loadJobDraft = async (jobIdToLoad: string) => {
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL;

      // 1. Fetch the basic draft info from monitored_jobs
      const draftResponse = await fetch(`${apiUrl}/jobs/${jobIdToLoad}/draft`);
      if (!draftResponse.ok) {
        console.error("Draft fetch HTTP error:", draftResponse.status);
        return false;
      }
      const draftResult = await draftResponse.json();

      // Backend returns HTTP 200 with status:error when not found
      if (draftResult.status === "error" || !draftResult.data) {
        console.error("Draft not found:", draftResult.message);
        return false;
      }

      const draft = draftResult.data;

      // 2. Fetch full job details from JobDiva to populate 'jobData'
      // This is critical for subsequent "Save & Exit" or "Next" actions
      const detailsResponse = await fetch(`${apiUrl}/jobs/fetch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobIdToLoad.trim() })
      });

      if (detailsResponse.ok) {
        const details = await detailsResponse.json();
        setJobData(details);
        if (details.jobdiva_id) {
          setJobdivaId(details.jobdiva_id);
        }
      }

      // 3. If we are on or past step 3, try to fetch existing rubric data
      if (draft.current_step >= 3) {
        try {
          const rubricRes = await fetch(`${apiUrl}/api/v1/gemini/jobs/${jobIdToLoad}/rubric`);
          if (rubricRes.ok) {
            const rData = await rubricRes.json();
            setRubricData(rData);
            if (rData.screen_questions?.length) {
              setScreenQuestions(rData.screen_questions.map((q: any, i: number) => ({ ...q, id: i + 1 })));
              setQuestionIdCounter(rData.screen_questions.length + 1);
            }
            if (rData.bot_introduction) {
              setBotIntroduction(rData.bot_introduction);
            }
          }
        } catch (e) {
          console.error("Failed to load existing rubric:", e);
        }
      }

      // 4. Restore form state (Draft values overlay JobDiva values)
      if (draft.title !== undefined && draft.title !== null) setJobTitle(draft.title || "");
      if (draft.enhanced_title !== undefined && draft.enhanced_title !== null) setEnhancedTitle(draft.enhanced_title || "");
      if (draft.ai_description !== undefined && draft.ai_description !== null) setJobPosting(draft.ai_description || "");
      if (draft.recruiter_notes !== undefined && draft.recruiter_notes !== null) setRecruiterNotes(draft.recruiter_notes || "");
      if (draft.selected_employment_types?.length) setSelectedEmpTypes(draft.selected_employment_types);
      if (draft.recruiter_emails?.length) setRecruiterEmails(draft.recruiter_emails);
      if (draft.pair_level) setScreeningLevel(draft.pair_level);
      if (draft.selected_job_boards?.length) setSelectedJobBoards(draft.selected_job_boards);
      if (draft.work_authorization) setWorkAuthorization(draft.work_authorization);
      if (draft.bot_introduction) setBotIntroduction(draft.bot_introduction);

      // 5. Navigate to the saved step
      if (draft.current_step) {
        const savedStep = draft.current_step as Step;
        setCurrentStep(savedStep);
        setPageSubtitle(STEP_DESCRIPTIONS[savedStep]);
        setIsFetched(true);
        setNumericJobId(jobIdToLoad);
      }

      return true;
    } catch (error) {
      console.error("Failed to load draft:", error);
    }
    return false;
  };

  const handleFetchJob = async () => {
    const isValidJobDivaId = (id: string) => id.trim().includes("-");

    if (!isValidJobDivaId(jobdivaId)) {
      showToast("Please enter a valid JobDiva Reference code (e.g., 26-06182)", "info");
      return;
    }

    const searchId = jobdivaId.trim();

    setIsFetching(true);
    setIsFetched(false);

    // RESET all states before new fetch to prevent stale data
    setJobTitle("");
    setEnhancedTitle("");
    setJobPosting("");
    setRecruiterNotes("");
    setRecruiterEmails([]);
    setSelectedEmpTypes([]);

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL;
      const response = await fetch(`${apiUrl}/jobs/fetch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: searchId })
      });

      if (!response.ok) {
        showToast("Job not found. Check the ID.", "info");
        return;
      }

      const data = await response.json();

      // Completeness Check: Ensure the job has at least a title
      if (!data.title) {
        showToast("Job not found or incomplete data from JobDiva.", "info");
        return;
      }

      setJobData(data); // Store the full data object from backend

      if (data.id) {
        console.log(`🔄 Identifier Resolved: Syncing internal numericJobId to Numeric PK '${data.id}'`);
        setNumericJobId(data.id.toString());
      }
      if (data.jobdiva_id) {
        console.log(`🔄 Ref Code Resolved: Setting UI jobdivaId to '${data.jobdiva_id}'`);
        setJobdivaId(data.jobdiva_id.toString());
      }

      const displayData = {
        title: data.title,
        customer_name: data.customer_name || data.customer,
        location: `${data.city || ""}, ${data.state || ""}`.trim() || "Remote",
        openings: data.openings || "1",
        type: data.employment_type || "Full-Time",
        rate: data.pay_rate || "Market Rate",
        startDate: data.start_date || "ASAP",
        postedDate: data.posted_date || "Recently posted",
        description: data.description
      };

      // Auto-populate intake form fields from JobDiva data
      console.log("Auto-populating intake form with JobDiva data...", data);

      // 1. Job Title and Description
      setJobTitle(data.title || "");
      setEnhancedTitle(data.enhanced_title || data.title || "");

      // Strict Check for AI Description (UDF 230)
      // If JobDiva result has "" or null for ai_description, then setJobPosting to ""
      // We no longer fall back to data.description to respect clearing intentionality
      if (data.ai_description !== undefined && data.ai_description !== null) {
        setJobPosting(data.ai_description);
      } else {
        // Only use description if we have ABSOLUTELY no AI description UDF info at all
        setJobPosting(data.description || "");
      }
      setPageSubtitle(`${displayData.title} · ${displayData.customer_name}`);

      // 2. Employment Type - auto-select from JobDiva OR restore previously selected types
      if (data.selected_employment_types && Array.isArray(data.selected_employment_types) && data.selected_employment_types.length > 0) {
        console.log("Restoring previously selected employment types:", data.selected_employment_types);
        setSelectedEmpTypes(data.selected_employment_types as EmploymentType[]);
      } else if (data.employment_type) {
        const empType = data.employment_type as EmploymentType;
        if (["W2", "1099", "C2C", "Full-Time"].includes(empType)) {
          setSelectedEmpTypes([empType]);
          showToast(`Employment type set to: ${empType}`, "info");
        }
      }

      // 3. Recruiter Notes - populate from JobDiva job_notes or local recruiter_notes if available
      const notes = data.recruiter_notes !== undefined ? data.recruiter_notes : data.job_notes;
      setRecruiterNotes(notes || "");
      if (notes) {
        showToast("Recruiter notes populated", "info");
      }

      // 4. Recruiter Emails - auto-populate from local database OR JobDiva recruiter_emails
      if (data.recruiter_emails && Array.isArray(data.recruiter_emails) && data.recruiter_emails.length > 0) {
        const validEmails = data.recruiter_emails.filter((email: string) =>
          email && typeof email === 'string' && /^\S+@\S+\.\S+$/.test(email.trim())
        );
        if (validEmails.length > 0) {
          setRecruiterEmails(validEmails);
          showToast(`${validEmails.length} recruiter email(s) populated`, "info");
        }
      }

      // 5. Set default screening level to L1.5 (recommended)
      setScreeningLevel("L1.5");

      // 6. Set Work Authorization from JobDiva
      if (data.work_authorization) {
        setWorkAuthorization(data.work_authorization);
      }

      setIsFetched(true);

      // FORCE: Always stay on step 1 for newly imported jobs to follow normal workflow
      setCurrentStep(1);
      setPageSubtitle(`${displayData.title} · ${displayData.customer_name}`);
      showToast("Job intake form auto-populated from JobDiva.", "success");
    } catch (error: any) {
      console.error("Error fetching job:", error);
      showToast(error.message === "Job not found or incomplete data from JobDiva." ? "Job not found. Check the ID." : "Failed to fetch job. Use format: 26-06182", "info");
    } finally {
      setIsFetching(false);
    }
  };

  const handleEnhanceJob = async (titleOverride?: string, descOverride?: string, notesOverride?: string) => {
    setIsGeneratingJD(true);
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL;
      const response = await fetch(`${apiUrl}/api/v1/gemini/jobs/${numericJobId || jobdivaId || 'new'}/generate-description`, {
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
      const apiUrl = process.env.NEXT_PUBLIC_API_URL;
      const res = await fetch(`${apiUrl}/api/v1/gemini/jobs/generate-title`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobTitle: jobTitle, // Always use original title as base for enhancement
          enhancedTitle: enhancedTitle, // Pass current enhanced title just in case 
          jobNotes: recruiterNotes,
          jobDescription: jobPosting
        })
      });
      if (res.ok) {
        const data = await res.json();
        setEnhancedTitle(data.title);

        showToast("Title enhanced by PAIR.", "success");
      } else {
        const err = await res.text();
        console.error("Title enhance failed:", err);
        showToast("Failed to enhance title.", "info");
      }
    } catch (e) {
      console.error(e);
      showToast("Failed to enhance title.", "info");
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
    setSelectedEmpTypes(prev => {
      const newTypes = prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type];
      return newTypes;
    });
  };

  const toggleJobBoard = (board: string) => {
    setSelectedJobBoards(prev => {
      const newSelection = prev.includes(board) ? prev.filter(b => b !== board) : [...prev, board];
      return newSelection;
    });
  };

  const saveJobDraft = async (stepData: {
    currentStep: number,
    saveType?: string,
    skipToast?: boolean
  }) => {
    if (!jobData || (!numericJobId && !jobdivaId)) {
      showToast("Job data not available for saving.", "info");
      return false;
    }

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL;
      // Use the new endpoint that saves directly to monitored_jobs
      const response = await fetch(`${apiUrl}/jobs/${numericJobId || jobdivaId}/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: numericJobId || jobdivaId,
          jobdiva_id: jobdivaId || jobData?.jobdiva_id || jobData?.id?.toString(),
          user_session: "default", // Add user session parameter required by API
          current_step: stepData.currentStep,
          title: jobTitle,
          enhanced_title: enhancedTitle,
          ai_description: jobPosting,
          recruiter_notes: recruiterNotes,
          work_authorization: workAuthorization || jobData?.work_authorization || "",
          selected_employment_types: selectedEmpTypes,
          recruiter_emails: recruiterEmails,
          pair_level: screeningLevel,
          selected_job_boards: selectedJobBoards,
          rubric: {
            ...rubricData,
            screen_questions: screenQuestions
          }, // 🔥 SEND FULL RUBRIC DATA + Screen Questions
          bot_introduction: botIntroduction,
          step1_completed: stepData.currentStep >= 1,
          step2_completed: stepData.currentStep >= 2,
          step3_completed: stepData.currentStep >= 3,
          is_auto_saved: stepData.saveType === "auto"
        })
      });

      if (!response.ok) throw new Error("Save failed");

      const result = await response.json();
      if (!stepData.skipToast) {
        showToast(stepData.saveType === "auto" ? "Auto-saved to monitored jobs" : "Saved to monitored jobs successfully", "success");
      }
      return true;
    } catch (error) {
      console.error("Error saving job to monitored jobs:", error);
      if (!stepData.skipToast) {
        showToast("Failed to save. Please try again.", "info");
      }
      return false;
    }
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
                    className={`absolute top-1/2 left-[calc(50%+18px)] right-[-50%] h-[2.5px] -translate-y-1/2 -z-10 transition-colors duration-300 ${isCompleted ? "bg-[#10b981]" : "bg-slate-200"}`}
                  />
                )}

                <div className={`
                  w-7 h-7 rounded-full flex items-center justify-center text-[13px] font-bold transition-all duration-300 relative z-10
                  ${isActive ? "bg-primary text-white shadow-[0_0_0_6px_rgba(99,102,241,0.12)]" : ""}
                  ${isCompleted ? "bg-[#10b981] text-white" : ""}
                  ${!isActive && !isCompleted ? "bg-slate-200 text-slate-500" : ""}
                `}>
                  {isCompleted ? <Check className="w-4 h-4 stroke-[3]" /> : stepNumber}
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
        <div>
          <label className="block text-[14px] font-medium text-slate-900 mb-3">JobDiva Job ID</label>
          <div className="flex items-center gap-3">
            <Input
              placeholder="e.g. 26-08025"
              value={jobdivaId}
              onChange={(e) => setJobdivaId(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && jobdivaId.trim().includes("-") && handleFetchJob()}
              className="max-w-[180px] h-[36px] bg-white border-slate-200 text-[13px]"
            />
            <button
              onClick={handleFetchJob}
              disabled={!jobdivaId.trim().includes("-") || isFetching}
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
                  // Row 1 — Identity
                  { label: "Job Title", value: jobData.title || "—" },
                  { label: "Customer", value: jobData.customer_name || jobData.customer || "—" },
                  { label: "Status", value: jobData.status || "—" },
                  // Row 2 — Contract Terms
                  { label: "Priority", value: (!jobData.priority || jobData.priority === "[null]") ? "—" : jobData.priority },
                  { label: "Program Duration", value: (!jobData.program_duration && !jobData.duration) || jobData.program_duration === "[null]" || jobData.duration === "[null]" ? "—" : (jobData.program_duration || jobData.duration) },
                  {
                    label: "Max Allowed Submittals",
                    value: (!jobData.max_allowed_submittals && !jobData.max_submittals) || jobData.max_allowed_submittals === "[null]" || jobData.max_submittals === "[null]" || Number.isNaN(Number.parseInt(jobData.max_allowed_submittals ?? jobData.max_submittals, 10))
                      ? "—"
                      : Number.parseInt(jobData.max_allowed_submittals ?? jobData.max_submittals, 10).toString()
                  },
                  // Row 3 — Compensation & Slots
                  { label: "Employment Type", value: jobData.employment_type || "—" },
                  { label: "Pay Rate", value: (!jobData.pay_rate || jobData.pay_rate === "[null]") ? "—" : jobData.pay_rate },
                  { label: "Openings", value: jobData.openings || "—" },
                  // Row 4 — Where & When
                  {
                    label: "Location",
                    value: [
                      `${jobData.city || ""}, ${jobData.state || ""}`.trim(),
                      jobData.zip_code || jobData.zip ? (jobData.zip_code || jobData.zip) : null,
                      cleanLocationType(jobData.location_type) ? `(${cleanLocationType(jobData.location_type)})` : null
                    ].filter(Boolean).join(" ") || "—"
                  },
                  { label: "Job Start Date", value: jobData.start_date || "—" },
                  { label: "Job Posted Date", value: jobData.posted_date || "—" },
                ].map(({ label, value }) => (
                  <div key={label} className="flex flex-col gap-1">
                    <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-400">{label}</span>
                    <span className="text-[14px] font-medium text-slate-900" title={value?.toString()}>{value}</span>
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
                <label className="flex flex-col gap-1 mb-2">
                  <div className="flex items-center gap-1.5 text-[14px] font-medium text-slate-900">
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 text-primary"><path d="M17.414 2.586a2 2 0 00-2.828 0L7 10.172V13h2.828l7.586-7.586a2 2 0 000-2.828z" /><path fillRule="evenodd" d="M2 6a2 2 0 012-2h4a1 1 0 010 2H4v10h10v-4a1 1 0 112 0v4a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" clipRule="evenodd" /></svg>
                    Recruiter Notes
                  </div>
                  <div className="flex items-start gap-1.5 px-2 py-1.5 bg-amber-50 border border-amber-100 rounded-md">
                    <Info className="w-3.5 h-3.5 text-amber-600 mt-0.5 flex-shrink-0" />
                    <span className="text-[12px] font-medium text-amber-700 leading-tight">
                      Whatever you write here will be used to generate the AI Job Description for external posting. Please be cautious of what you include.
                    </span>
                  </div>
                </label>
                <Textarea
                  placeholder="e.g. Client strongly prefers fintech background. Must be local to Atlanta metro — no relocation. W2 only, no C2C. Ideally someone with NetSuite over SAP. Start date is flexible but ASAP preferred..."
                  value={recruiterNotes}
                  onChange={(e) => {
                    setRecruiterNotes(e.target.value);
                  }}
                  rows={3}
                  className="text-[14px] border-slate-200 resize-y min-h-[100px]"
                />
              </div>

              {/* Employment Type */}
              <div className="mb-5">
                <label className="block text-[14px] font-medium text-slate-900 mb-1">
                  Employment Type <span className="text-red-500">*</span>
                </label>
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
                      <button onClick={(e) => { e.stopPropagation(); removeEmail(email); }} className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-7 h-7 flex items-center justify-center rounded-md transition-all duration-200" title="Remove">
                        <X className="w-4 h-4" />
                      </button>
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
                <p className="text-[12px] text-slate-500 mt-1.5">Press comma, semicolon, or Enter to add. You'll receive notifications for this job.</p>
              </div>

              {/* Screening Level */}
              <div>
                <label className="block text-[14px] font-medium text-slate-900 mb-1">Screening Level</label>
                <p className="text-[13px] text-slate-500 mb-4">How deeply should PAIR screen each candidate?</p>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {/* L1 */}
                  <div
                    className={`flex-1 border-2 rounded-[10px] p-4 cursor-pointer transition-all ${screeningLevel === "L1" ? "border-primary bg-[#f5f3ff]" : "border-slate-200 hover:border-primary"}`}
                    onClick={() => {
                      setScreeningLevel("L1");
                    }}
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
                    onClick={() => {
                      setScreeningLevel("L1.5");
                    }}
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
                    onClick={() => {
                      setScreeningLevel("L2");
                    }}
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
                  value={enhancedTitle}
                  onChange={(e) => {
                    setEnhancedTitle(e.target.value);
                  }}
                  placeholder="Enhanced Job Title"
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
                  onChange={(e) => {
                    setJobPosting(e.target.value);
                  }}
                  onBlur={() => {
                    setIsEditingJD(false);
                  }}
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
                className="bg-slate-50/50 border border-slate-200 rounded-lg p-7 h-[500px] overflow-y-auto scrollbar-thin scrollbar-thumb-slate-200 text-[13.5px] font-normal leading-relaxed text-slate-900 cursor-text hover:border-primary/40 hover:bg-white transition-colors group relative flex items-center justify-center text-center"
              >
                {jobPosting ? (
                  <>
                    <div className="absolute top-4 right-4 bg-slate-200 text-slate-600 text-[11px] font-bold px-3 py-1.5 rounded-md shadow-sm opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none">
                      Click anywhere to edit
                    </div>
                    <div className="w-full h-full text-left">
                      <AIPostingJobDescription text={jobPosting} />
                    </div>
                  </>
                ) : (
                  <div className="flex flex-col items-center gap-4 max-w-sm px-6">
                    <div className="w-16 h-16 bg-white rounded-full shadow-sm flex items-center justify-center border border-slate-100">
                      <Sparkles className="w-8 h-8 text-primary/40" />
                    </div>
                    <div>
                      <h4 className="text-[17px] font-bold text-slate-900">No AI Description Yet</h4>
                      <p className="text-[14px] text-slate-500 mt-2 leading-relaxed">
                        This job doesn't have an AI-enhanced description. Click the
                        <strong> "Regenerate"</strong> button above to generate one now.
                      </p>
                    </div>
                    <Button
                      variant="outline"
                      className="mt-2 border-primary/20 hover:bg-white hover:text-primary hover:border-primary/40"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleEnhanceJob();
                      }}
                    >
                      Generate AI JD
                    </Button>
                  </div>
                )}
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
                { name: "Monster", icon: <PawPrint className="w-4 h-4 text-[#6d1f7e]" /> },
                { name: "CareerBuilder", icon: <Building2 className="w-4 h-4 text-[#00a4bd]" /> },
              ].map(board => (
                <label key={board.name} className="flex items-center gap-3 p-2.5 hover:bg-white hover:shadow-sm cursor-pointer transition-all rounded-xl group/item">
                  <Checkbox
                    checked={selectedJobBoards.includes(board.name)}
                    onCheckedChange={() => toggleJobBoard(board.name)}
                    className="w-[18px] h-[18px] rounded-md border-slate-300 data-[state=checked]:bg-[#4f46e5] data-[state=checked]:border-[#4f46e5] text-white transition-all"
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

  const updateRubricItem = (category: string, index: number, field: string, value: any) => {
    setRubricData((prev: any) => {
      if (!prev || !prev[category]) return prev;
      const updated = { ...prev };
      updated[category] = [...updated[category]];
      updated[category][index] = { ...updated[category][index], [field]: value };
      return updated;
    });
  };

  const moveRubricItem = (category: string, from: number, to: number) => {
    setRubricData((prev: any) => {
      if (!prev || !prev[category]) return prev;
      const updated = { ...prev };
      const items = [...updated[category]];
      const [moved] = items.splice(from, 1);
      items.splice(to, 0, moved);
      updated[category] = items;
      return updated;
    });
  };

  const removeRubricItem = (category: string, index: number) => {
    console.log(`🗑️ Removing ${category} at index ${index}`);
    setRubricData((prev: any) => {
      if (!prev || !prev[category]) return prev;
      return {
        ...prev,
        [category]: prev[category].filter((_: any, i: number) => i !== index)
      };
    });
  };

  const addRubricItem = (category: string, newItem: any) => {
    setRubricData((prev: any) => {
      if (!prev) return prev;
      const updated = { ...prev };
      if (!updated[category]) updated[category] = [];
      updated[category] = [...updated[category], newItem];
      return updated;
    });
  };

  const establishRubricStep = (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      <div className="flex flex-row items-start gap-4 px-7 py-6 border-b border-slate-100" style={{ background: "linear-gradient(135deg, #f8f7ff 0%, #ffffff 60%)" }}>
        <ListChecks className="w-[22px] h-[22px] text-primary mt-0.5 flex-shrink-0" />
        <div>
          <h2 className="text-[21px] font-medium text-slate-900 leading-tight tracking-tight">Establish Rubric</h2>
          <p className="text-slate-500 text-[15px] mt-1 leading-relaxed">PAIR-extracted rubric items from the job description. These become the rubric by which candidates are graded. Edit freely.</p>
        </div>
      </div>

      {isGeneratingRubric ? (
        <div className="p-20 flex flex-col items-center justify-center gap-4">
          <div className="w-8 h-8 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
          <p className="text-[15px] font-medium text-slate-600 animate-pulse">Extracting criteria from PAIR Job Description...</p>
        </div>
      ) : rubricData ? (
        <div className="p-7 space-y-7">

          {/* Titles */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Clipboard className="w-4 h-4 text-slate-900 flex-shrink-0" />
              <h3 className="text-[14px] font-bold text-slate-800">Titles</h3>
              <span className="text-[12px] font-normal text-slate-500">Job title for sourcing & resume matching · 5 max</span>
            </div>

            {/* Column Headers */}
            <div className="flex items-center gap-2.5 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-1">
              <div className="flex-1 min-w-0">Job Title</div>
              <div className="w-[110px] flex-shrink-0 flex items-center justify-center">
                Min. Years
              </div>
              <div className="w-[70px] flex-shrink-0 flex items-center justify-center">
                Recent
              </div>
              <div className="w-[170px] flex-shrink-0 flex items-center justify-center">
                Match Type
              </div>
              <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                Required / Preferred
              </div>
              <div className="w-[70px] flex-shrink-0"></div>
              <div className="w-[36px] flex-shrink-0"></div>
            </div>

            <div className="space-y-0">
              {rubricData.titles?.map((title: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <input
                      type="text"
                      value={title.value}
                      onChange={(e) => updateRubricItem('titles', idx, 'value', e.target.value)}
                      className="flex-1 min-w-0 text-[13px] font-normal text-slate-700 bg-transparent border border-transparent rounded px-2 py-1.5 outline-none focus:border-slate-200 focus:bg-white transition-all"
                    />
                    {title.source === 'JobDiva' && (
                      <span className="bg-slate-100 text-slate-600 text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap border border-slate-200">JOBDIVA</span>
                    )}
                    {title.source === 'PAIR' && (
                      <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap border border-[#ddd6fe]">PAIR</span>
                    )}
                    {title.source === 'AI' && (
                      <span className="bg-[#dcfce7] text-[#166534] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap border border-[#bbf7d0]">AI</span>
                    )}
                    {(!title.source || title.source === 'User') && (
                      <span className="bg-blue-50 text-blue-600 text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap border border-blue-100">USER</span>
                    )}
                  </div>
                  <div className="w-[110px] flex-shrink-0 flex items-center gap-1.5">
                    <input
                      type="number"
                      value={title.minYears}
                      onChange={(e) => updateRubricItem('titles', idx, 'minYears', parseInt(e.target.value) || 0)}
                      className="w-12 border border-slate-200 rounded px-1.5 py-1 text-[13px] text-center outline-none focus:border-[#818cf8]"
                    />
                    <span className="text-[12px] text-slate-500">{title.minYears === 0 ? '—' : 'yrs'}</span>
                  </div>
                  <div className="w-[70px] flex-shrink-0 flex items-center justify-center">
                    <Checkbox checked={title.recent} onCheckedChange={(checked) => updateRubricItem('titles', idx, 'recent', !!checked)} className="border-slate-300 rounded-[4px] data-[state=checked]:bg-[#6d28d9] data-[state=checked]:border-[#6d28d9] text-white w-[16px] h-[16px] hover:border-[#6d28d9] transition-all" />
                  </div>
                  <div className="w-[170px] flex-shrink-0">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[118px] bg-white cursor-pointer select-none">
                      <button onClick={() => updateRubricItem('titles', idx, 'matchType', 'Exact')} className={`flex-1 py-[3px] rounded-full transition-all ${title.matchType === 'Exact' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Exact</button>
                      <button onClick={() => updateRubricItem('titles', idx, 'matchType', 'Similar')} className={`flex-1 py-[3px] rounded-full transition-all ${title.matchType === 'Similar' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Similar</button>
                    </div>
                  </div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none">
                      <button onClick={() => updateRubricItem('titles', idx, 'required', 'Required')} className={`flex-1 py-[3px] rounded-full transition-all ${title.required === 'Required' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Required</button>
                      <button onClick={() => updateRubricItem('titles', idx, 'required', 'Preferred')} className={`flex-1 py-[3px] rounded-full transition-all ${title.required === 'Preferred' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[70px] flex-shrink-0 flex flex-col gap-1 items-center">
                    <button
                      disabled={idx === 0}
                      onClick={() => moveRubricItem('titles', idx, idx - 1)}
                      className="w-[22px] h-[22px] flex items-center justify-center border border-slate-200 rounded-[4px] bg-white text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all disabled:opacity-20 disabled:pointer-events-none"
                    >
                      <ChevronUp className="w-3.5 h-3.5" />
                    </button>
                    <button
                      disabled={idx === (rubricData.titles?.length - 1)}
                      onClick={() => moveRubricItem('titles', idx, idx + 1)}
                      className="w-[22px] h-[22px] flex items-center justify-center border border-slate-200 rounded-[4px] bg-white text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all disabled:opacity-20 disabled:pointer-events-none"
                    >
                      <ChevronDown className="w-3.5 h-3.5" />
                    </button>
                  </div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button 
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('titles', idx);
                      }} 
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200" 
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}

              <div className="mt-3">
                <Button 
                  variant="outline" 
                  size="sm" 
                  disabled={(rubricData.titles?.length || 0) >= 5}
                  onClick={() => addRubricItem('titles', { value: '', minYears: 0, recent: false, matchType: 'Similar', required: 'Preferred', source: 'User' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Title
                </Button>
                <span className={`ml-3 text-[13.5px] font-medium ${(rubricData.titles?.length || 0) >= 5 ? 'text-rose-600' : 'text-slate-500'}`}>
                  {(rubricData.titles?.length || 0)} / 5
                </span>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Skills */}
          <section>
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <Wand2 className="w-4 h-4 text-slate-900 flex-shrink-0" />
                <h3 className="text-[14px] font-bold text-slate-800">Skills</h3>
                <span className="text-[12px] font-normal text-slate-500">Top 8 · ordered by importance</span>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => showToast("No new suggestions — list is full or already complete.", "info")}
                className="border-slate-200 text-[#1e293b] bg-white hover:bg-slate-50 font-medium text-[13px] rounded-[7px] shadow-none h-[28px] px-2.5 border transition-all"
              >
                <Wand2 className="w-3 h-3 mr-1 text-[#7e22ce]" />
                Suggest More
              </Button>
            </div>

            {/* Column Headers */}
            <div className="flex items-center gap-2.5 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-1">
              <div className="flex-1 min-w-0">Hard Skill</div>
              <div className="w-[110px] flex-shrink-0 flex items-center justify-center">
                Min. Years
              </div>
              <div className="w-[70px] flex-shrink-0 flex items-center justify-center">
                Recent
              </div>
              <div className="w-[170px] flex-shrink-0 flex items-center justify-center">
                Match Type
              </div>
              <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                Required / Preferred
              </div>
              <div className="w-[106px] flex-shrink-0 flex items-center justify-center">
                Actions
              </div>
            </div>
            <div className="space-y-0">
              {rubricData.skills?.map((skill: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <input
                      type="text"
                      value={skill.value}
                      onChange={(e) => updateRubricItem('skills', idx, 'value', e.target.value)}
                      className="flex-1 min-w-0 text-[13px] font-normal text-slate-700 bg-transparent border border-transparent rounded px-2 py-1.5 outline-none focus:border-slate-200 focus:bg-white transition-all"
                    />
                    <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap">PAIR</span>
                  </div>
                  <div className="w-[110px] flex-shrink-0 flex items-center gap-1.5">
                    <input
                      type="number"
                      value={skill.minYears}
                      onChange={(e) => updateRubricItem('skills', idx, 'minYears', parseInt(e.target.value) || 0)}
                      className="w-12 border border-slate-200 rounded px-1.5 py-1 text-[13px] text-center outline-none focus:border-[#818cf8]"
                    />
                    <span className="text-[12px] text-slate-500">{skill.minYears === 0 ? '—' : 'yrs'}</span>
                  </div>
                  <div className="w-[70px] flex-shrink-0 flex items-center justify-center">
                    <Checkbox checked={skill.recent} onCheckedChange={(checked) => updateRubricItem('skills', idx, 'recent', !!checked)} className="border-slate-300 rounded-[4px] data-[state=checked]:bg-[#6d28d9] data-[state=checked]:border-[#6d28d9] text-white w-[16px] h-[16px] hover:border-[#6d28d9] transition-all" />
                  </div>
                  <div className="w-[170px] flex-shrink-0">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[118px] bg-white cursor-pointer select-none">
                      <button onClick={() => updateRubricItem('skills', idx, 'matchType', 'Exact')} className={`flex-1 py-[3px] rounded-full transition-all ${skill.matchType === 'Exact' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Exact</button>
                      <button onClick={() => updateRubricItem('skills', idx, 'matchType', 'Similar')} className={`flex-1 py-[3px] rounded-full transition-all ${skill.matchType === 'Similar' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Similar</button>
                    </div>
                  </div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none">
                      <button onClick={() => updateRubricItem('skills', idx, 'required', 'Required')} className={`flex-1 py-[3px] rounded-full transition-all ${skill.required === 'Required' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Required</button>
                      <button onClick={() => updateRubricItem('skills', idx, 'required', 'Preferred')} className={`flex-1 py-[3px] rounded-full transition-all ${skill.required === 'Preferred' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[70px] flex-shrink-0 flex flex-col gap-1 items-center">
                    <button
                      disabled={idx === 0}
                      onClick={() => moveRubricItem('skills', idx, idx - 1)}
                      className="w-[22px] h-[22px] flex items-center justify-center border border-slate-200 rounded-[4px] bg-white text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all disabled:opacity-20 disabled:pointer-events-none"
                    >
                      <ChevronUp className="w-3.5 h-3.5" />
                    </button>
                    <button
                      disabled={idx === (rubricData.skills?.length - 1)}
                      onClick={() => moveRubricItem('skills', idx, idx + 1)}
                      className="w-[22px] h-[22px] flex items-center justify-center border border-slate-200 rounded-[4px] bg-white text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all disabled:opacity-20 disabled:pointer-events-none"
                    >
                      <ChevronDown className="w-3.5 h-3.5" />
                    </button>
                  </div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button 
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('skills', idx);
                      }} 
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200" 
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}

              <div className="ml-1 mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={(rubricData.skills?.length || 0) >= 8}
                  onClick={() => addRubricItem('skills', { value: '', minYears: 0, recent: false, matchType: 'Similar', required: 'Preferred' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Skill
                </Button>
                <span className={`ml-3 text-[13.5px] font-medium ${(rubricData.skills?.length || 0) >= 8 ? 'text-rose-600' : 'text-slate-500'}`}>
                  {(rubricData.skills?.length || 0)} / 8
                </span>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Education & Certificates */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <div className="flex items-center gap-2">
                <GraduationCap className="w-4 h-4 text-slate-900" />
                <h3 className="text-[14px] font-bold text-slate-800">Education & Certificates</h3>
              </div>
              <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full flex items-center gap-1">
                <Sparkles className="w-3 h-3" /> PAIR detected
              </span>
            </div>

            <div className="space-y-0">
              {rubricData.education?.map((edu: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <select
                      value={edu.degree}
                      onChange={(e) => updateRubricItem('education', idx, 'degree', e.target.value)}
                      className="h-[34px] w-[150px] bg-slate-50 border border-slate-200 rounded-lg text-slate-700 text-[13px] px-2 font-medium outline-none cursor-pointer flex-shrink-0 hover:border-slate-300 transition-all shadow-sm"
                    >
                      <option value="No requirement">No requirement</option>
                      <option value="High School / GED">High School / GED</option>
                      <option value="Associate's degree">Associate's degree</option>
                      <option value="Bachelor's degree">Bachelor's degree</option>
                      <option value="Master's degree">Master's degree</option>
                      <option value="PhD or equivalent">PhD or equivalent</option>
                      <option value="Certification / License">Certification / License</option>
                    </select>
                    <span className="text-slate-400 font-medium text-[11.5px] whitespace-nowrap flex-shrink-0 px-1">in / as</span>
                    <Input
                      value={edu.field}
                      onChange={(e) => updateRubricItem('education', idx, 'field', e.target.value)}
                      className="w-[260px] flex-shrink-0 h-[34px] text-[13px] font-medium text-slate-700 bg-white border-slate-200"
                      placeholder="Field of study"
                    />
                    <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight whitespace-nowrap ml-1 uppercase">PAIR</span>
                  </div>
                  <div className="w-[110px] flex-shrink-0"></div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[170px] flex-shrink-0"></div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none shadow-sm">
                      <button onClick={() => updateRubricItem('education', idx, 'required', 'Required')} className={`flex-1 py-[2.5px] rounded-full transition-all ${edu.required === 'Required' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Required</button>
                      <button onClick={() => updateRubricItem('education', idx, 'required', 'Preferred')} className={`flex-1 py-[2.5px] rounded-full transition-all ${edu.required === 'Preferred' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button 
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('education', idx);
                      }} 
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200" 
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRubricItem('education', { degree: "Bachelor's degree", field: '', required: 'Required' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Education / Certificate
                </Button>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Domain */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Building2 className="w-4 h-4 text-slate-900" />
              <h3 className="text-[14px] font-bold text-slate-800">Domain</h3>
              <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full flex items-center gap-1">
                <Sparkles className="w-3 h-3" /> Detected in JD
              </span>
            </div>

            <div className="space-y-0">
              {rubricData.domain?.map((dom: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <Input
                      value={dom.value}
                      onChange={(e) => updateRubricItem('domain', idx, 'value', e.target.value)}
                      className="flex-1 h-[34px] text-[13px] font-medium text-slate-700 bg-white border-slate-200"
                      readOnly
                    />
                    <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight whitespace-nowrap ml-2 uppercase">PAIR</span>
                  </div>
                  <div className="w-[110px] flex-shrink-0"></div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[170px] flex-shrink-0"></div>
                  <div className="w-[180px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none">
                      <button onClick={() => updateRubricItem('domain', idx, 'required', 'Required')} className={`flex-1 py-[2px] rounded-full transition-all ${dom.required === 'Required' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Required</button>
                      <button onClick={() => updateRubricItem('domain', idx, 'required', 'Preferred')} className={`flex-1 py-[2px] rounded-full transition-all ${dom.required === 'Preferred' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button 
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('domain', idx);
                      }} 
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200" 
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRubricItem('domain', { value: '', required: 'Required' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Domain
                </Button>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Customer Requirements */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <UserCheck className="w-4 h-4 text-slate-900 flex-shrink-0" />
              <h3 className="text-[14px] font-bold text-slate-800">Customer Requirements</h3>
              <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full flex items-center gap-1">
                <Sparkles className="w-3 h-3" /> PAIR generated
              </span>
            </div>

            <div className="space-y-0">
              {rubricData.customer_requirements?.map((req: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <select
                      className="h-[34px] w-[190px] bg-slate-50 border border-slate-200 rounded-lg text-slate-700 text-[13px] px-2 font-medium outline-none cursor-pointer flex-shrink-0"
                      value={req.type}
                      onChange={(e) => updateRubricItem('customer_requirements', idx, 'type', e.target.value)}
                    >
                      <option value="Must not be employed by">Must not be employed by</option>
                      <option value="Currently employed by">Currently employed by</option>
                      <option value="Previously employed by">Previously employed by</option>
                    </select>
                    <Input
                      value={req.value}
                      onChange={(e) => updateRubricItem('customer_requirements', idx, 'value', e.target.value)}
                      className="w-[350px] flex-shrink-0 h-[34px] text-[13px] font-medium text-slate-700 bg-[#fffafb] border-[#fecaca] focus:border-rose-300 focus:ring-0"
                      placeholder="Company name"
                    />
                  </div>
                  <div className="w-[110px] flex-shrink-0"></div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[170px] flex-shrink-0"></div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center"></div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button 
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('customer_requirements', idx);
                      }} 
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200" 
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}

              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRubricItem('customer_requirements', { type: 'Must not be employed by', value: '' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Requirement
                </Button>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Other Requirements */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Lightbulb className="w-4 h-4 text-slate-900" />
              <h3 className="text-[14px] font-bold text-slate-800">Other Requirements</h3>
              <span className="text-[12px] text-slate-500 font-normal">Location constraints, shift requirements, work authorization, etc.</span>
            </div>

            <div className="space-y-0">
              {rubricData.other_requirements?.map((req: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <input
                      type="text"
                      value={req.value}
                      onChange={(e) => updateRubricItem('other_requirements', idx, 'value', e.target.value)}
                      className="flex-1 text-[13px] font-medium text-slate-700 bg-transparent border-none outline-none focus:ring-0 placeholder:text-slate-400 py-1"
                      placeholder="Requirement..."
                    />
                  </div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none shadow-sm">
                      <button onClick={() => updateRubricItem("other_requirements", idx, "required", "Required")} className={`flex-1 py-[2.5px] rounded-full transition-all ${req.required === "Required" ? "bg-[#dcfce7] text-[#166534]" : "text-slate-400"}`}>Required</button>
                      <button onClick={() => updateRubricItem("other_requirements", idx, "required", "Preferred")} className={`flex-1 py-[2.5px] rounded-full transition-all ${req.required === "Preferred" ? "bg-[#ede9fe] text-[#6d28d9]" : "text-slate-400"}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button 
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('other_requirements', idx);
                      }} 
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200" 
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}

              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRubricItem('other_requirements', { value: '', required: 'Required' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Requirement
                </Button>
              </div>
            </div>
          </section>

        </div>
      ) : null}
    </div>
  );

  // Filter management functions
  const toggleResumeFilter = (id: number, active: boolean) => {
    setResumeMatchFilters(prev =>
      prev.map(filter =>
        filter.id === id ? { ...filter, active } : filter
      )
    );
  };

  const updateResumeFilter = (id: number, value: string) => {
    setResumeMatchFilters(prev =>
      prev.map(filter =>
        filter.id === id ? { ...filter, value } : filter
      )
    );
  };

  const deleteResumeFilter = (id: number) => {
    setResumeMatchFilters(prev => prev.filter(filter => filter.id !== id));
  };

  const addResumeFilter = () => {
    const category = prompt('Filter category (e.g. Skills, Location, Certification):');
    if (!category || !category.trim()) return;
    const value = prompt(`Value for "${category.trim()}":`);
    if (!value || !value.trim()) return;

    setResumeMatchFilters(prev => [
      ...prev,
      {
        id: filterIdCounter,
        category: category.trim(),
        value: value.trim(),
        active: true,
        ai: false,
        fromRubric: false
      }
    ]);
    setFilterIdCounter(prev => prev + 1);
  };

  // Initialize filters from rubric data when moving to step 4
  const initializeFiltersFromRubric = () => {
    if (!rubricData || resumeMatchFilters.length > 0) return;

    const filters: Array<{
      id: number;
      category: string;
      value: string;
      active: boolean;
      ai: boolean;
      fromRubric: boolean;
    }> = [];

    let idCounter = 1;

    // Add title filters (all active)
    if (rubricData.titles) {
      rubricData.titles.forEach((title: any) => {
        filters.push({
          id: idCounter++,
          category: 'Required Title',
          value: `${title.value} — ${title.minYears}+ yrs, ${title.matchType} match`,
          active: true,
          ai: true,
          fromRubric: true
        });
      });
    }

    // Add skill filters (first few active, rest inactive to show variety)
    if (rubricData.skills) {
      rubricData.skills.forEach((skill: any, index: number) => {
        filters.push({
          id: idCounter++,
          category: skill.required === 'Required' ? 'Required Skill' : 'Preferred Skill',
          value: `${skill.value} — ${skill.minYears}+ yrs, ${skill.matchType} match`,
          active: index < 4, // First 4 skills active, rest inactive
          ai: true,
          fromRubric: true
        });
      });
    }

    // Add education filters (inactive by default)
    if (rubricData.education) {
      rubricData.education.forEach((edu: any) => {
        filters.push({
          id: idCounter++,
          category: 'Education',
          value: `${edu.degree}${edu.field ? ` in ${edu.field}` : ''}`,
          active: false,
          ai: true,
          fromRubric: true
        });
      });
    }

    // Add domain experience (inactive by default)
    if (rubricData.domain) {
      rubricData.domain.forEach((dom: any) => {
        filters.push({
          id: idCounter++,
          category: 'Domain',
          value: dom.value,
          active: false,
          ai: true,
          fromRubric: true
        });
      });
    }

    // Add common filters based on job data
    if (jobData) {
      const location = `${jobData.city || ''}, ${jobData.state || ''}`.trim();
      if (location) {
        filters.push({
          id: idCounter++,
          category: 'Requirement',
          value: `Must be local to ${location} metro`,
          active: true,
          ai: false,
          fromRubric: true
        });
      }

      if (jobData.customer_name || jobData.customer) {
        filters.push({
          id: idCounter++,
          category: 'Customer Req.',
          value: `Must not be employed by: ${jobData.customer_name || jobData.customer}`,
          active: true,
          ai: false,
          fromRubric: true
        });
      }
    }

    setResumeMatchFilters(filters);
    setFilterIdCounter(idCounter);
  };

  const initializeScreenQuestionsFromRubric = () => {
    if (!jobData) return;

    const location = `${jobData.city || ""}, ${jobData.state || ""}`.trim().replace(/^, |, $/g, "");
    const isRemote = jobData.location_type?.toLowerCase() === "remote";
    
    let idCounter = 1;
    const questions: ScreenQuestion[] = [];

    // 1. Bot Introduction
    const intro = `Hi {{candidate name}}, I'm Nova, a virtual recruiter with Pyramid Consulting. We are helping our client recruit for a ${jobTitle || "role"} in ${location || "your area"}, and you seem to be a good fit for the role. Please note that conversation may be recorded for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?`;
    setBotIntroduction(intro);

    // 2. Default Questions
    const defaultQs = [
      { text: "Are you open to exploring new job opportunities?", criteria: "Must be open to new job opportunities" },
      { text: "What is your current or most recent role and key responsibilities?", criteria: "" },
      { text: "What is your current location?", criteria: "" },
      ...(isRemote ? [] : [{ text: "Are you open to working onsite if required for the role?", criteria: "Must be willing to work onsite" }]),
      { text: "What is your earliest availability to start a new role?", criteria: `Must be available by ${jobData.start_date || 'ASAP'}` },
      { text: "What is your current compensation and expected compensation?", criteria: "" },
      { text: "Are you authorized to work in the United States?", criteria: "" },
      { text: "Will you now or in the future require visa sponsorship to continue working in the United States?", criteria: "" }
    ];

    defaultQs.forEach((q, index) => {
      questions.push({
        id: idCounter++,
        question_text: q.text,
        pass_criteria: q.criteria,
        is_default: true,
        category: "default",
        order_index: index
      });
    });

    // 3. Role-Specific Questions (from rubric skills)
    if (rubricData?.skills) {
      rubricData.skills.forEach((skill: any, index: number) => {
        // Only generate questions for required skills or first 4 skills
        if (questions.length < 12) {
          questions.push({
            id: idCounter++,
            question_text: `Can you describe your experience with ${skill.value}? We're looking for ${skill.minYears}+ years of experience.`,
            pass_criteria: `Must have ${skill.minYears}+ yrs of ${skill.value} experience`,
            is_default: false,
            category: "role-specific",
            order_index: questions.length
          });
        }
      });
    }

    setScreenQuestions(questions);
    setQuestionIdCounter(idCounter);
  };

  const addScreenQuestion = () => {
    const newQuestion: ScreenQuestion = {
      id: questionIdCounter,
      question_text: "",
      pass_criteria: "",
      is_default: false,
      category: "other",
      order_index: screenQuestions.length
    };
    setScreenQuestions([...screenQuestions, newQuestion]);
    setQuestionIdCounter(questionIdCounter + 1);
  };

  const updateScreenQuestion = (id: number, field: keyof ScreenQuestion, value: any) => {
    setScreenQuestions(prev => prev.map(q => q.id === id ? { ...q, [field]: value } : q));
  };

  const deleteScreenQuestion = (id: number) => {
    setScreenQuestions(prev => prev.filter(q => q.id !== id));
  };

  const setFiltersStep = (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      <div className="flex flex-row items-start gap-4 px-7 py-6 border-b border-slate-100"
        style={{ background: "linear-gradient(135deg, #f8f7ff 0%, #ffffff 60%)" }}>
        <Filter className="w-[22px] h-[22px] text-primary mt-0.5 flex-shrink-0" />
        <div>
          <h2 className="text-[20px] font-medium text-slate-900 leading-tight tracking-tight">Set Filters</h2>
          <p className="text-slate-500 text-[14px] mt-1 leading-relaxed">Each rubric item from Establish Rubric is evaluated here. Toggle, edit, or add filters for resume matching and the PAIR phone screen.</p>
        </div>
      </div>

      <div className="p-7 space-y-7">
        {/* Resume Match Section */}
        <section>
          <div className="flex items-center gap-2 mb-4">
            <FileText className="w-4 h-4 text-slate-900 flex-shrink-0" />
            <h3 className="text-[14px] font-bold text-slate-800">Resume Match</h3>
            <span className="text-[12px] font-normal text-slate-500">Hard filters applied during resume screening</span>
            <span className="ml-auto bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
              <Sparkles className="w-3 h-3 inline mr-1" />
              PAIR pre-filled
            </span>
          </div>

          {/* Filter Header */}
          <div className="flex items-center gap-3 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-2">
            <div className="w-[44px] flex-shrink-0"></div>
            <div className="w-[110px] flex-shrink-0">Category</div>
            <div className="flex-1">Value</div>
            <div className="w-[100px] flex-shrink-0"></div>
          </div>

          {/* Active Filters */}
          {resumeMatchFilters.filter(f => f.active).length > 0 && (
            <>
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-500 py-2">
                <div className="w-2 h-2 bg-green-500 rounded-full"></div>
                <span>Active ({resumeMatchFilters.filter(f => f.active).length})</span>
              </div>
              {resumeMatchFilters.filter(f => f.active).map((filter) => (
                <div key={filter.id} className="flex items-center gap-3 py-3 border-b border-slate-100 last:border-b-0">
                  <button
                    onClick={() => toggleResumeFilter(filter.id, false)}
                    className="w-10 h-7 rounded bg-green-100 border border-green-300 text-green-600 text-[11px] font-bold flex items-center justify-center transition-all hover:bg-green-200"
                    title="Disable"
                  >
                    On
                  </button>
                  <span className="w-[110px] flex-shrink-0 bg-slate-100 text-slate-600 text-[11px] font-semibold px-3 py-1 rounded-full text-center">
                    {filter.category}
                  </span>
                  <div className="flex-1 min-w-0">
                    <input
                      type="text"
                      value={filter.value}
                      onChange={(e) => updateResumeFilter(filter.id, e.target.value)}
                      className="w-full text-[13px] bg-transparent border-none outline-none text-slate-900 font-medium"
                    />
                  </div>
                  <div className="w-[100px] flex-shrink-0 flex items-center justify-end gap-2">
                    {filter.ai && (
                      <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
                        PAIR
                      </span>
                    )}
                    {filter.fromRubric && (
                      <span className="bg-slate-100 text-slate-600 text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
                        from rubric
                      </span>
                    )}
                    <button
                      onClick={() => deleteResumeFilter(filter.id)}
                      className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-6 h-6 flex items-center justify-center rounded transition-all ml-2"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
            </>
          )}

          {/* Inactive Filters */}
          {resumeMatchFilters.filter(f => !f.active).length > 0 && (
            <>
              {resumeMatchFilters.filter(f => f.active).length > 0 && (
                <div className="h-px bg-slate-200 my-4"></div>
              )}
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-400 py-2">
                <div className="w-2 h-2 bg-slate-400 rounded-full"></div>
                <span>Off ({resumeMatchFilters.filter(f => !f.active).length})</span>
              </div>
              {resumeMatchFilters.filter(f => !f.active).map((filter) => (
                <div key={filter.id} className="flex items-center gap-3 py-3 border-b border-slate-100 last:border-b-0 opacity-70">
                  <button
                    onClick={() => toggleResumeFilter(filter.id, true)}
                    className="w-10 h-7 rounded bg-slate-100 border border-slate-300 text-slate-400 text-[11px] font-bold flex items-center justify-center transition-all hover:border-primary hover:text-primary"
                    title="Enable"
                  >
                    Off
                  </button>
                  <span className="w-[110px] flex-shrink-0 bg-slate-50 text-slate-400 text-[11px] font-semibold px-3 py-1 rounded-full text-center">
                    {filter.category}
                  </span>
                  <div className="flex-1 min-w-0">
                    <input
                      type="text"
                      value={filter.value}
                      onChange={(e) => updateResumeFilter(filter.id, e.target.value)}
                      className="w-full text-[13px] bg-transparent border-none outline-none text-slate-500 font-medium"
                    />
                  </div>
                  <div className="w-[100px] flex-shrink-0 flex items-center justify-end gap-2">
                    {filter.ai && (
                      <span className="bg-slate-100 text-slate-400 text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
                        PAIR
                      </span>
                    )}
                    {filter.fromRubric && (
                      <span className="bg-slate-50 text-slate-400 text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
                        from rubric
                      </span>
                    )}
                    <button
                      onClick={() => deleteResumeFilter(filter.id)}
                      className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-6 h-6 flex items-center justify-center rounded transition-all ml-2"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
            </>
          )}

          {/* No filters state */}
          {resumeMatchFilters.length === 0 && (
            <p className="text-[13px] text-slate-400 py-4">No filters set.</p>
          )}

          {/* Add Filter Button */}
          <Button
            variant="outline"
            size="sm"
            onClick={addResumeFilter}
            className="mt-3 border-slate-200 text-slate-600 bg-white hover:bg-slate-50 font-medium text-[13px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
          >
            <Plus className="w-3.5 h-3.5 mr-1.5" />
            Add Resume Filter
          </Button>
        </section>

        <div className="h-px bg-slate-100 my-2"></div>

        {/* Screen Section */}
        <section className="pt-2">
          <div className="flex items-center gap-2 mb-4">
            <Users className="w-4 h-4 text-slate-900 flex-shrink-0" />
            <h3 className="text-[14px] font-bold text-slate-800">Screen</h3>
            <span className="text-[12px] font-normal text-slate-500">Questions asked during PAIR phone screen</span>
            <span className="ml-auto text-slate-400 text-[11px] font-bold">
              {screenQuestions.length} / 12 questions
            </span>
          </div>

          {/* Bot Introduction */}
          <div className="bg-[#f8faff] rounded-xl border border-[#e0e7ff] p-5 mb-6 relative">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-5 h-5 bg-[#6d28d9] rounded flex items-center justify-center">
                <Users className="w-3 h-3 text-white" />
              </div>
              <span className="text-[12px] font-bold text-slate-800">Bot Introduction</span>
              <span className="text-[11px] text-slate-400 font-normal">— what Nova says at the start of each call. Variables in {"{{brackets}}"} are filled at runtime.</span>
            </div>
            <textarea
              value={botIntroduction}
              onChange={(e) => setBotIntroduction(e.target.value)}
              className="w-full bg-transparent border-none outline-none text-[13px] text-slate-600 leading-relaxed resize-none h-24"
              placeholder="Enter bot introduction..."
            />
          </div>

          {/* Questions Table */}
          <div className="flex items-center gap-3 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-2">
            <div className="w-8 flex-shrink-0">#</div>
            <div className="flex-1">Question</div>
            <div className="flex-1">Pass Criteria <span className="text-[10px] font-normal lowercase">(blank = informational only)</span></div>
            <div className="w-10 flex-shrink-0"></div>
          </div>

          {screenQuestions.map((q, index) => (
            <div key={q.id} className="flex items-start gap-3 py-3 border-b border-slate-100 last:border-b-0 group">
              <div className="w-8 h-8 rounded-full bg-[#6366f1] text-white flex items-center justify-center text-[12px] font-bold flex-shrink-0 mt-0.5">
                {index + 1}
              </div>
              
              <div className="flex-1 min-w-0">
                <textarea
                  value={q.question_text}
                  onChange={(e) => updateScreenQuestion(q.id, 'question_text', e.target.value)}
                  className="w-full text-[13px] bg-transparent border-none outline-none text-slate-900 font-medium resize-none"
                  rows={2}
                />
              </div>

              <div className="flex-1 min-w-0 border-l border-slate-100 pl-3">
                <input
                  type="text"
                  value={q.pass_criteria}
                  onChange={(e) => updateScreenQuestion(q.id, 'pass_criteria', e.target.value)}
                  className={`w-full text-[13px] bg-transparent border-none outline-none font-medium ${q.pass_criteria ? 'text-[#4338ca]' : 'text-slate-300 italic'}`}
                  placeholder="No hard filter"
                />
              </div>

              <div className="w-10 flex-shrink-0 flex flex-col items-end gap-2 pr-1">
                {q.category === 'role-specific' && (
                  <span className="bg-[#f0fdf4] text-[#166534] text-[9px] font-bold px-1.5 py-0.5 rounded border border-[#bbf7d0] whitespace-nowrap mb-1">
                    role-specific
                  </span>
                )}
                <button
                  onClick={() => deleteScreenQuestion(q.id)}
                  className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-6 h-6 flex items-center justify-center rounded transition-all opacity-0 group-hover:opacity-100"
                  title="Remove"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}

          {/* Add Question Button */}
          <Button
            variant="outline"
            size="sm"
            onClick={addScreenQuestion}
            className="mt-3 border-slate-200 text-slate-600 bg-white hover:bg-slate-50 font-medium text-[13px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
          >
            <Plus className="w-3.5 h-3.5 mr-1.5" />
            Add Question
          </Button>
        </section>
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
      case 3: return establishRubricStep;
      case 4: {
        // Initialize filters from rubric when entering step 4
        if (rubricData && resumeMatchFilters.length === 0) {
          initializeFiltersFromRubric();
          if (screenQuestions.length === 0) {
            initializeScreenQuestionsFromRubric();
          }
        }
        return setFiltersStep;
      }
      case 5: return <PlaceholderStep stepNumber={5} title="Launch & Source" />;
      default: return null;
    }
  };

  return (
    <div className="p-8 max-w-7xl mx-auto animate-in fade-in slide-in-from-bottom-4 duration-500">
      {/* Breadcrumb */}
      <div className="mb-5">
        <Link href="/jobs" className="text-slate-500 hover:text-slate-700 text-[15px] flex items-center gap-2 transition-colors">
          <ArrowLeft className="w-4 h-4" />
          Back to Jobs
        </Link>
      </div>

      {/* Page Header */}
      <div className="mb-7">
        <h1 className="text-[29px] font-bold text-slate-900 leading-none">New Job</h1>
        {currentStep !== 2 && <p className="text-slate-500 text-[15px] mt-2">{pageSubtitle}</p>}
      </div>

      {/* Step Indicator */}
      <StepIndicator />

      {/* Step Content */}
      {renderStepContent()}

      {/* Wizard Navigation — Back | Save & Exit … Next */}
      <div className="flex items-center justify-between pt-8 border-t border-slate-200 mt-8">
        <div className="flex items-center gap-3">
          {currentStep > 1 && (
            <Button
              variant="outline"
              className="h-[38px] px-5 border-slate-200 text-slate-700 font-bold text-[14px] shadow-none hover:bg-slate-50 flex items-center gap-2 rounded-xl transition-all active:scale-95"
              onClick={() => setCurrentStep((currentStep - 1) as Step)}
            >
              <ArrowLeft className="w-4 h-4" />
              Back
            </Button>
          )}
          <Button
            variant="outline"
            className="h-[38px] px-5 border-slate-200 text-slate-700 font-bold text-[14px] shadow-none hover:bg-slate-50 flex items-center gap-2 rounded-xl transition-all active:scale-95"
            onClick={async () => {
              if (currentStep > 1) {
                const saved = await saveJobDraft({ currentStep, saveType: "manual" });
                if (saved) window.location.href = "/";
              } else {
                window.location.href = "/";
              }
            }}
          >
            <Save className="w-4 h-4 text-slate-400" />
            Save & Exit
          </Button>
        </div>
        <div className="flex items-center gap-3">
          <Button
            className="h-[38px] px-7 bg-primary hover:bg-primary/90 flex items-center gap-2 shadow-sm text-[14px] font-bold text-white transition-all rounded-xl active:scale-95"
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
                if (selectedEmpTypes.length === 0) {
                  showToast("Employment Type is required.", "info");
                  return;
                }

                // Save Step 1 data to monitored jobs before moving to next step
                const saved = await saveJobDraft({ currentStep: 1, skipToast: true });
                if (!saved) {
                  showToast("Failed to save Step 1 data. Please try again.", "info");
                  return;
                }
                setCurrentStep(2);
                showToast("Step 1 data saved successfully!", "success");
              }

              if (currentStep === 2) {
                // Save Step 2 data to monitored jobs before proceeding
                const saved = await saveJobDraft({ currentStep: 2, skipToast: true });
                if (!saved) {
                  showToast("Failed to save Step 2 data to monitored jobs. Please try again.", "info");
                  return;
                }

                if (!rubricData) {
                  setIsGeneratingRubric(true);
                  setCurrentStep(3);
                  try {
                    const apiUrl = process.env.NEXT_PUBLIC_API_URL;
                    const res = await fetch(`${apiUrl}/api/v1/gemini/jobs/generate-rubric`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        jobId: numericJobId || jobdivaId,
                        jobdivaId: jobdivaId,
                        jobTitle: jobData?.title || jobTitle,           // Always the raw original
                        enhancedJobTitle: enhancedTitle || "",          // Set only if enhance was clicked
                        jobDescription: jobPosting,
                        jobNotes: recruiterNotes,
                        originalDescription: jobData?.description || "",
                        customerName: jobData?.customer_name || jobData?.customer || "",
                        requiredDegree: jobData?.required_degree || "",
                        jobCity: jobData?.city || "",
                        jobState: jobData?.state || "",
                        locationType: jobData?.location_type || ""
                      })
                    });
                    if (res.ok) {
                      const data = await res.json();
                      setRubricData(data);
                      showToast("Step 2 saved and rubric generated!", "success");
                    } else {
                      throw new Error("API failed");
                    }
                  } catch (e) {
                    console.error(e);
                    // Show error to user instead of hardcoded fallback
                    showToast("Failed to generate rubric. Please try again or contact support.", "info");
                    setRubricData(null);
                  } finally {
                    setIsGeneratingRubric(false);
                  }
                  return;
                } else {
                  showToast("Step 2 data saved to monitored jobs successfully!", "success");
                }
              }

              if (currentStep === 3) {
                // Save Step 3 (Rubric) data before moving to filters
                const saved = await saveJobDraft({ currentStep: 3, skipToast: true });
                if (!saved) {
                  showToast("Failed to save rubric data. Please try again.", "info");
                  return;
                }
              }

              if (currentStep === 4) {
                // Save Step 4 (Screening & Intro) data before moving to final step
                const saved = await saveJobDraft({ currentStep: 4, skipToast: true });
                if (!saved) {
                  showToast("Failed to save screening data. Please try again.", "info");
                  return;
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
};