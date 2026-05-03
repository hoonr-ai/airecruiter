"use client";

import { useState, useEffect } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  Download,
  Mail,
  Phone,
  Calendar,
  Building2,
  User,
  MessageSquare,
  AlertCircle,
  ChevronDown,
  Send,
  Ban,
  Headphones,
  AlertTriangle,
  CircleCheck,
  Briefcase,
  MapPin,
  Hash,
  DollarSign,
  Circle,
  Info,
  ExternalLink,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { API_BASE } from "@/lib/api";
import { cn } from "@/lib/utils";

interface EvaluationReport {
  status: string;
  candidate: {
    candidate_id: string;
    name: string;
    email: string;
    phone: string;
    headline: string;
    location: string;
    availability: string;
    resume_text: string;
    feedback_type?: string;
    feedback_reason?: string;
    feedback_at?: string;
  };
  scores: {
    resume_match_score: number;
    engage_score: number | null;
    engage_status: string;
    hard_filter_status: string;
    total_fit_score: number;
  };
  job: {
    job_id: string;
    jobdiva_id: string;
    title: string;
    customer_name: string;
    job_location: string;
    pay_rate: string;
    employment_type: string;
    ai_description: string;
    resume_match_filters: any[];
    screen_questions: any[];
  };
  pair: {
    interview: any;
    evaluation: any;
    transcriptions: any[];
    questions_answers: any[];
    audit_payload?: any;
    audit_response?: any;
  };
}

export default function CandidateEvaluationReportPage() {
  const { jobId } = useParams();
  const searchParams = useSearchParams();
  const candidateId = searchParams.get("candidateId");
  const router = useRouter();
  const [data, setData] = useState<EvaluationReport | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchReport() {
      try {
        setIsLoading(true);
        // Use query parameter to avoid URL encoding issues on QA
        const res = await fetch(`${API_BASE}/candidates/evaluation-report?candidate_id=${encodeURIComponent(candidateId as string)}&job_id=${jobId}`);
        if (!res.ok) throw new Error("Failed to fetch evaluation report");
        const json = await res.json();
        setData(json);
        // Set document title for professional PDF filename
        if (json.candidate?.name) {
          document.title = `Candidate Evaluation Report - ${json.candidate.name}`;
        }
      } catch (err: any) {
        setError(err.message);
      } finally {
        setIsLoading(false);
      }
    }
    if (candidateId && jobId) {
      fetchReport();
    }
  }, [candidateId, jobId]);

  // Feedback Integration States
  const [integrationModalOpen, setIntegrationModalOpen] = useState<'submit' | 'reject' | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [syncingCandidateId, setSyncingCandidateId] = useState<string | null>(null);

  const handleConfirmSubmit = async () => {
    if (candidateId) {
      setSyncingCandidateId(candidateId);
      try {
        const response = await fetch(`${API_BASE}/jobs/${jobId}/candidates/${candidateId}/feedback`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ feedback_type: 'Submit' })
        });

        if (response.ok) {
          // Optimistically update local state
          setData(prev => {
            if (!prev) return prev;
            return {
              ...prev,
              candidate: {
                ...prev.candidate,
                feedback_type: 'Submit',
                feedback_at: new Date().toISOString()
              }
            };
          });
          setIntegrationModalOpen(null);
        } else {
          console.error('Failed to sync submission with JobDiva');
          setIntegrationModalOpen(null);
        }
      } catch (error) {
        console.error('Error syncing submission:', error);
        setIntegrationModalOpen(null);
      } finally {
        setSyncingCandidateId(null);
      }
    }
  };

  const handleConfirmReject = async () => {
    if (candidateId && rejectReason) {
      setSyncingCandidateId(candidateId);
      try {
        const response = await fetch(`${API_BASE}/jobs/${jobId}/candidates/${candidateId}/feedback`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            feedback_type: 'Reject',
            reason: rejectReason
          })
        });

        if (response.ok) {
          // Optimistically update local state
          setData(prev => {
            if (!prev) return prev;
            return {
              ...prev,
              candidate: {
                ...prev.candidate,
                feedback_type: 'Reject',
                feedback_reason: rejectReason,
                feedback_at: new Date().toISOString()
              }
            };
          });
          setIntegrationModalOpen(null);
        } else {
          console.error('Failed to sync rejection with JobDiva');
          setIntegrationModalOpen(null);
        }
      } catch (error) {
        console.error('Error syncing rejection:', error);
        setIntegrationModalOpen(null);
      } finally {
        setSyncingCandidateId(null);
        setRejectReason('');
      }
    }
  };

  const getStatusType = (status: string): "success" | "danger" | "neutral" | "info" => {
    const s = String(status || "").toLowerCase();
    if (s.includes("pass") || s.includes("complete") || s.includes("success")) return "success";
    if (s.includes("pending") || s.includes("initi") || s.includes("progress")) return "neutral";
    if (s.includes("fail") || s.includes("reject") || s.includes("error")) return "danger";
    return "neutral";
  };

  const formatStatusLabel = (status: string): string => {
    const s = String(status || "").toLowerCase();
    if (!s) return "N/A";
    if (s === "completed") return "Completed";
    if (s === "passed") return "Pass";
    if (s === "failed") return "Fail";
    if (s === "pending" || s === "initiated") return "Pending";
    return status.charAt(0).toUpperCase() + status.slice(1);
  };

  if (isLoading) {
    return (
      <div className="max-w-[1100px] mx-auto p-8 space-y-8 bg-white min-h-screen">
        <Skeleton className="h-6 w-48" />
        <div className="flex justify-between items-end">
          <div className="space-y-2">
            <Skeleton className="h-10 w-64" />
            <Skeleton className="h-4 w-40" />
          </div>
          <div className="flex gap-2">
            <Skeleton className="h-10 w-32" />
            <Skeleton className="h-10 w-32" />
          </div>
        </div>
        <Skeleton className="h-[200px] w-full rounded-xl" />
        <Skeleton className="h-[100px] w-full rounded-xl" />
        <Skeleton className="h-[400px] w-full rounded-xl" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="max-w-[1100px] mx-auto p-8 flex flex-col items-center justify-center min-h-[60vh] space-y-4 bg-white">
        <AlertCircle className="w-16 h-16 text-[#dc2626]" />
        <h1 className="text-2xl font-bold text-[#0f172a]">Error Loading Report</h1>
        <p className="text-[#64748b]">{error || "Could not load report."}</p>
        <Button onClick={() => window.location.reload()}>Try Again</Button>
      </div>
    );
  }

  const { candidate, scores, job, pair } = data;

  const formatAvailability = (availability: string, interviewDate?: string) => {
    if (!availability || availability.toLowerCase().includes("no data available")) return "No data available";
    
    const lower = availability.toLowerCase();
    if (lower.includes("immedi")) return "Immediately";
    
    // Check for "X days" (e.g., "within 15 days", "15 days")
    const daysMatch = availability.match(/(\d+)\s*days/i);
    if (daysMatch) {
      if (!interviewDate) return "No data available";
      const days = parseInt(daysMatch[1]);
      const date = new Date(interviewDate);
      date.setDate(date.getDate() + days);
      return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    }
    
    return availability;
  };

  const candidateDetails = [
    { label: "Email", value: candidate.email || "No data available" },
    { label: "Phone", value: candidate.phone || "No data available" },
    { label: "Available", value: formatAvailability(candidate.availability, pair.interview?.completed_at) },
  ];

  return (
    <div className="min-h-screen bg-[#f8fafc] font-sans pb-20 text-[#1e293b]">
      <style jsx global>{`
        @media print {
          /* Hide sidebar and app navigation only for print/PDF */
          nav, aside, [class*="sidebar"], [class*="Sidebar"], 
          [class*="brand"], [class*="Logo"], [class*="logo"],
          [class*="tira"], [class*="Tira"],
          .no-print { 
            display: none !important; 
          }

          /* Force report to take full width in the PDF */
          .ml-64 { margin-left: 0 !important; }
          main { padding: 0 !important; margin: 0 !important; }
          html, body { background-color: white !important; margin: 0 !important; padding: 0 !important; }
          main { position: absolute !important; left: 0 !important; top: 0 !important; width: 100% !important; }
          .max-w-[1100px] { max-width: 100% !important; width: 100% !important; margin: 0 !important; padding: 0 !important; }
          
          /* Clean up card styling for professional PDF */
          .rounded-[12px], .rounded-xl { 
            border-radius: 0 !important; 
            border: 1px solid #e2e8f0 !important; 
            page-break-inside: avoid !important; 
            margin-bottom: 30px !important; 
          }
          /* Expand all scrollable content (transcripts, summaries, etc.) */
          .overflow-y-auto, [class*="scroll"], [class*="Scroll"] { 
            overflow: visible !important; 
            max-height: none !important; 
            height: auto !important;
          }
          
          .px-6, .p-6 { padding: 40px !important; }
          button, .btn, [role="button"] { display: none !important; }
        }
      `}</style>
      <div className="max-w-[1100px] mx-auto px-6 py-10 space-y-8">
        
        {/* Breadcrumb */}
        <div className="mb-2">
          <button
            onClick={() => router.back()}
            className="flex items-center gap-2 text-[#64748b] hover:text-[#1e293b] transition-colors text-[14px]"
          >
            <ArrowLeft className="w-4 h-4" />
            Back to Candidate Rankings
          </button>
        </div>

        {/* Page Header */}
        <div className="flex flex-col md:flex-row md:items-start justify-between gap-6">
          <div className="space-y-1">
            <h1 className="text-[32px] font-bold text-[#0f172a] tracking-tight leading-tight">Candidate Evaluation Report</h1>
            <p className="text-[#64748b] text-[14px]">
              Printable report for <span className="font-bold text-[#0f172a]">{candidate.name}</span>
            </p>
          </div>
          <div className="flex gap-3 no-print">
            {candidate.feedback_type ? (
              <div className={cn(
                "flex items-center gap-2 px-6 py-2 rounded-xl text-[14px] font-bold shadow-sm border",
                candidate.feedback_type === 'Submit' 
                  ? "bg-emerald-50 border-emerald-200 text-emerald-700" 
                  : "bg-rose-50 border-rose-200 text-rose-700"
              )}>
                {candidate.feedback_type === 'Submit' ? (
                  <><CircleCheck className="w-4 h-4" /> Submitted</>
                ) : (
                  <><Ban className="w-4 h-4" /> Rejected</>
                )}
              </div>
            ) : (
              <>
                <button 
                  onClick={() => window.print()}
                  className="flex items-center gap-2 px-4 py-2 bg-white border border-slate-200 rounded-lg text-sm font-medium text-slate-600 hover:bg-slate-50 transition-all shadow-sm"
                >
                  <Download className="w-4 h-4" />
                  Download PDF
                </button>
                <button 
                  onClick={() => setIntegrationModalOpen('submit')}
                  className="flex items-center gap-2 px-4 py-2 bg-[#10b981] rounded-lg text-sm font-semibold text-white hover:bg-[#059669] transition-all shadow-md shadow-emerald-100"
                >
                  <Send className="w-4 h-4" />
                  Submit
                </button>
                <button 
                  onClick={() => setIntegrationModalOpen('reject')}
                  className="flex items-center gap-2 px-4 py-2 bg-white border border-rose-200 rounded-lg text-sm font-medium text-rose-600 hover:bg-rose-50 transition-all shadow-sm"
                >
                  <Ban className="w-4 h-4" />
                  Reject
                </button>
              </>
            )}
          </div>
        </div>

        {/* Candidate Details Card */}
        <div className="bg-white rounded-[12px] border border-[#e2e8f0] shadow-[0_1px_3px_0_rgba(0,0,0,0.05)] overflow-hidden">
          <div className="px-6 py-4">
            <h3 className="text-[16px] font-bold text-[#0f172a] tracking-tight">Candidate Details</h3>
          </div>
          <div className="h-[1px] bg-[#f1f5f9]" />
          <div className="p-6 grid grid-cols-1 md:grid-cols-2 gap-x-12 gap-y-4">
            <div className="flex items-center gap-3">
              <User className="w-4 h-4 text-[#94a3b8] shrink-0" />
              <span className="text-[14px] font-bold text-[#0f172a]">{candidate.name || "No data available"}</span>
            </div>
            <div className="flex items-center gap-3">
              <Mail className="w-4 h-4 text-[#94a3b8] shrink-0" />
              {candidate.email ? (
                <a href={`mailto:${candidate.email}`} className="text-[14px] font-semibold text-[#4f46e5] hover:underline break-all">{candidate.email}</a>
              ) : (
                <span className="text-[14px] text-[#94a3b8] italic">No data available</span>
              )}
            </div>
            <div className="flex items-center gap-3">
              <Phone className="w-4 h-4 text-[#94a3b8] shrink-0" />
              <span className="text-[14px] font-medium text-[#334155]">{candidate.phone || "No data available"}</span>
            </div>
            <div className="flex items-center gap-3">
              <Calendar className="w-4 h-4 text-[#94a3b8] shrink-0" />
              <span className="text-[14px] text-[#334155] font-medium">Available: <strong className="text-[#0f172a] font-bold">{candidateDetails[2].value}</strong></span>
            </div>
          </div>
        </div>

        {/* Stage Results Card */}
        <div className="bg-white rounded-[12px] border border-[#e2e8f0] shadow-[0_1px_3px_0_rgba(0,0,0,0.05)] overflow-hidden">
          <div className="px-6 py-4">
            <h3 className="text-[16px] font-bold text-[#0f172a] tracking-tight">Stage Results</h3>
          </div>
          <div className="h-[1px] bg-[#f1f5f9]" />
          <div className="flex flex-col md:flex-row items-stretch">
            <div className="flex-1 p-6 space-y-3">
              <div className="text-[12px] font-bold text-[#94a3b8] uppercase tracking-widest">Resume Screening</div>
              <div className="flex items-center gap-4">
                <StatusPill status="Pass" type="success" />
                <div className="text-[14px] text-[#475569]">Score: <strong className="text-[#0f172a] ml-1 font-bold">{scores.resume_match_score || 0}/100</strong></div>
              </div>
            </div>
            <div className="w-[1px] bg-[#f1f5f9] my-4 hidden md:block" />
            <div className="flex-1 p-6 space-y-3">
              <div className="text-[12px] font-bold text-[#94a3b8] uppercase tracking-widest">ENGAGE (L1 Screen)</div>
              <div className="flex items-center gap-4">
                <StatusPill 
                  status={formatStatusLabel(scores.hard_filter_status || scores.engage_status)} 
                  type={getStatusType(scores.hard_filter_status || scores.engage_status)} 
                />
                <div className="text-[14px] text-[#475569]">Score: <strong className="text-[#0f172a] ml-1 font-bold">{scores.engage_score !== null ? `${scores.engage_score}/100` : "N/A"}</strong></div>
              </div>
            </div>
            <div className="w-[1px] bg-[#f1f5f9] my-4 hidden md:block" />
            <div className="flex-1 p-6 space-y-3">
              <div className="text-[12px] font-bold text-[#94a3b8] uppercase tracking-widest">Total Fit Score</div>
              <div className="flex items-center gap-4">
                <div className="text-[14px] text-[#475569]">Score: <strong className="text-[#0f172a] ml-1 font-bold">{scores.total_fit_score || 0}/100</strong></div>
              </div>
            </div>
          </div>
        </div>

        {/* Position Details Card */}
        <div className="bg-white rounded-[12px] border border-[#e2e8f0] shadow-[0_1px_3px_0_rgba(0,0,0,0.05)] overflow-hidden">
          <div className="px-6 py-4">
            <h3 className="text-[16px] font-bold text-[#0f172a] tracking-tight">Position Details</h3>
          </div>
          <div className="h-[1px] bg-[#f1f5f9]" />
          <div className="p-6 space-y-6">
            <div className="flex items-center gap-3 text-[#64748b] font-medium text-[16px]">
              <Building2 className="w-5 h-5 text-[#64748b] shrink-0" />
              {job.customer_name || "No data available"}
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-12 gap-y-4 text-[14px]">
              <div className="text-[#334155]">
                <strong className="text-[#0f172a]">Job Title:</strong> {job.title || "No data available"}
              </div>
              <div className="text-[#334155]">
                <strong className="text-[#0f172a]">Job Location:</strong> {job.job_location || "No data available"}
              </div>
              <div className="text-[#334155]">
                <strong className="text-[#0f172a]">JobDiva ID:</strong> {job.jobdiva_id || "No data available"}
              </div>
              <div className="text-[#334155]">
                <strong className="text-[#0f172a]">Pay Range:</strong> {job.pay_rate || "No data available"}
              </div>
            </div>
            
            <div className="space-y-3 pt-2">
              <h4 className="text-[14px] font-bold text-[#0f172a]">Job Summary:</h4>
              <div className="bg-[#f8fafc] rounded-xl p-6 border border-[#f1f5f9] text-[14px] text-[#475569] leading-relaxed font-medium max-h-[400px] overflow-y-auto">
                <AIPostingJobDescription text={job.ai_description || "No data available"} />
              </div>
            </div>
          </div>
        </div>



        {/* ENGAGE Section */}
        <div className="space-y-0.5">
          <div className="bg-[#4f46e5] px-6 py-4 rounded-t-xl text-white font-bold text-[15px] flex items-center justify-between">
            ENGAGE
            <Button variant="ghost" className="h-9 bg-white/15 hover:bg-white text-white hover:text-[#312e81] gap-2 font-bold text-[13px] px-5 border border-white/20 rounded-lg shadow-sm transition-all duration-200 group">
              <Headphones className="w-4 h-4 text-white group-hover:text-[#4f46e5]" /> Call Recordings <ChevronDown className="w-3.5 h-3.5 text-white/80 group-hover:text-[#4f46e5]" />
            </Button>
          </div>
          <div className="bg-white rounded-b-xl border border-[#e2e8f0] shadow-sm p-6 space-y-10">
            

            {/* Status Bar */}
            <div className="flex items-center gap-3 py-3 px-5 bg-[#f8fafc] rounded-lg border border-[#f1f5f9]">
              <Info className="w-4 h-4 text-[#64748b]" />
              <span className="text-[14px] font-bold text-[#334155]">Engage Status:</span>
              <StatusPill 
                status={formatStatusLabel(scores.hard_filter_status || scores.engage_status)} 
                type={getStatusType(scores.hard_filter_status || scores.engage_status)} 
              />
            </div>

            {/* Screening Hard Filters Result */}
            <div className="space-y-4">
              <h4 className="text-[14px] font-bold text-[#0f172a]">Screening Hard Filters Result</h4>
              <div className="space-y-2">
                {(() => {
                  const auditQuestions = pair.audit_payload?.questions || [];
                  const auditResponses = pair.audit_response?.questions || [];
                  
                  // Filter questions that have pass_criteria (these are the hard filters)
                  const hardFilters = auditQuestions.filter((q: any) => q.pass_criteria);
                  
                  if (hardFilters.length > 0) {
                    return hardFilters.map((q: any, i: number) => {
                      // Find the corresponding answer/status in the response
                      const response = auditResponses.find((r: any) => 
                        r.question_text === q.question_text || r.id === q.id
                      );
                      const status = (response?.status || "PENDING").toUpperCase();
                      
                      return (
                        <div key={i} className="flex items-center justify-between py-3 px-5 bg-white border border-[#e2e8f0] rounded-xl hover:border-slate-300 transition-all shadow-sm">
                          <span className="text-[14px] text-[#475569] font-medium leading-relaxed pr-8">{q.question_text}</span>
                          <StatusPill 
                            status={status === 'PASS' ? 'Pass' : status === 'FAIL' ? 'Fail' : 'Pending'} 
                            type={status === 'PASS' ? 'success' : status === 'FAIL' ? 'danger' : 'neutral'} 
                          />
                        </div>
                      );
                    });
                  }

                  // Fallback to existing logic if audit data is missing
                  const fallbackFilters = pair.evaluation?.hard_filters || pair.questions_answers || job.screen_questions || [];
                  
                  if (fallbackFilters.length > 0) {
                    return fallbackFilters.map((q: any, i: number) => {
                      const status = (q.pass_fail || q.status || "PENDING").toUpperCase();
                      return (
                        <div key={i} className="flex items-center justify-between py-3 px-5 bg-white border border-[#e2e8f0] rounded-xl hover:border-slate-300 transition-all shadow-sm">
                          <span className="text-[14px] text-[#475569] font-medium leading-relaxed pr-8">{q.question_text || q.question || q.name}</span>
                          <StatusPill 
                            status={status === 'PASS' ? 'Pass' : status === 'FAIL' ? 'Fail' : 'Pending'} 
                            type={status === 'PASS' ? 'success' : status === 'FAIL' ? 'danger' : 'neutral'} 
                          />
                        </div>
                      );
                    });
                  }

                  return (
                    <div className="text-center py-12 bg-[#f8fafc] rounded-xl border border-dashed border-[#e2e8f0] text-[#94a3b8] italic text-[14px]">
                      No screening filter data available yet.
                    </div>
                  );
                })()}
              </div>
            </div>

            {/* Conversations */}
            <div className="space-y-6 pt-4">
              <h4 className="text-[14px] font-bold text-[#0f172a]">Conversations:</h4>
              <div className="space-y-8 bg-[#f8fafc] rounded-2xl p-8 border border-[#f1f5f9] max-h-[600px] overflow-y-auto scrollbar-thin scrollbar-thumb-[#e2e8f0] scroll-smooth">
                {pair.transcriptions?.length > 0 ? (
                  pair.transcriptions.map((msg: any, i: number) => {
                    const isBot = msg.speaker_type === "bot" || msg.role === "assistant";
                    return (
                      <div key={i} className={cn("flex flex-col gap-2.5 relative", isBot ? "items-start pr-12" : "items-end pl-12")}>
                        <span className={cn("text-[11px] font-black uppercase tracking-widest", isBot ? "text-[#4f46e5]" : "text-[#94a3b8]")}>
                          {isBot ? "ASSISTANT (ALEX)" : "CANDIDATE"}
                        </span>
                        <div className={cn(
                          "p-6 rounded-[20px] text-[14px] leading-relaxed font-medium shadow-sm border",
                          isBot 
                            ? "bg-[#eef2ff] border-[#e0e7ff] text-[#312e81] rounded-tl-none" 
                            : "bg-white border-[#e2e8f0] text-[#1e293b] rounded-tr-none"
                        )}>
                          {msg.message_text || msg.text || msg.content}
                        </div>
                      </div>
                    );
                  })
                ) : (
                  <div className="text-center py-12 bg-[#f8fafc] rounded-xl border border-dashed border-[#e2e8f0] text-[#94a3b8] italic text-[14px]">
                    No conversation transcript available.
                  </div>
                )}
              </div>
            </div>

          </div>
        </div>
      </div>
      {/* Integration Modals */}
      {integrationModalOpen && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4 no-print">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg overflow-hidden border border-slate-200">
            {integrationModalOpen === 'submit' ? (
              <>
                <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
                  <h3 className="text-lg font-bold text-slate-900 flex items-center gap-2">
                    <ExternalLink className="w-5 h-5 text-indigo-600" />
                    Submit to JobDiva
                  </h3>
                  <button onClick={() => setIntegrationModalOpen(null)} className="text-slate-400 hover:text-slate-600">×</button>
                </div>
                <div className="p-6 space-y-4">
                  <p className="text-sm text-slate-500">
                    This action will initiate an <strong className="text-slate-900 font-semibold">external submission in JobDiva</strong> for:
                  </p>
                  <div className="bg-slate-50 p-4 rounded-xl border border-slate-100 space-y-3 text-sm text-slate-700">
                    <div className="flex items-center gap-2.5">
                      <User className="w-4 h-4 text-slate-400" />
                      <p><strong className="text-slate-900">Candidate:</strong> {candidate?.name || "Winci Zu"}</p>
                    </div>
                    <div className="flex items-center gap-2.5">
                      <Briefcase className="w-4 h-4 text-slate-400" />
                      <p><strong className="text-slate-900">Job:</strong> {job?.title} ({job?.jobdiva_id || job?.job_id || jobId})</p>
                    </div>
                    <div className="flex items-center gap-2.5">
                      <Building2 className="w-4 h-4 text-slate-400" />
                      <p><strong className="text-slate-900">Client:</strong> {job?.customer_name || "—"}</p>
                    </div>
                    <div className="flex items-center gap-2.5">
                      <Zap className="w-4 h-4 text-slate-400" />
                      <p><strong className="text-slate-900">Action:</strong> Create external submission record in JobDiva</p>
                    </div>
                  </div>
                </div>
                <div className="px-6 py-4 border-t border-slate-100 bg-slate-50 flex justify-end gap-3">
                  <button 
                    onClick={() => setIntegrationModalOpen(null)} 
                    className="px-4 py-2 text-sm font-semibold text-slate-600 hover:bg-slate-100 rounded-lg transition-all"
                  >
                    Cancel
                  </button>
                  <button
                    className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-bold rounded-lg shadow-md transition-all disabled:opacity-50"
                    onClick={handleConfirmSubmit}
                    disabled={!!syncingCandidateId}
                  >
                    {syncingCandidateId ? 'Syncing...' : 'Confirm & Submit to JobDiva'}
                  </button>
                </div>
              </>
            ) : (
              <>
                <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
                  <h3 className="text-lg font-bold text-slate-900 flex items-center gap-2">
                    <span className="w-5 h-5 rounded-full bg-rose-100 text-rose-600 flex items-center justify-center font-bold text-[11px]">✕</span>
                    Reject Candidate
                  </h3>
                  <button onClick={() => setIntegrationModalOpen(null)} className="text-slate-400 hover:text-slate-600">×</button>
                </div>
                <div className="p-6 space-y-4">
                  <p className="text-sm text-slate-500">
                    Please provide a reason for rejecting <strong className="text-slate-900 font-semibold">{candidate?.name || "Winci Zu"}</strong>.
                  </p>
                  <div className="space-y-2">
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-widest">Rejection Reason</label>
                    <select
                      className="w-full h-11 px-3 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-rose-500/20 focus:border-rose-500/50"
                      value={rejectReason}
                      onChange={e => setRejectReason(e.target.value)}
                    >
                      <option value="" disabled>Select a reason...</option>
                      <option value="Skills do not meet requirements">Skills do not meet requirements</option>
                      <option value="Communication skills">Communication skills</option>
                      <option value="Domain experience mismatch">Domain experience mismatch</option>
                      <option value="More qualified candidates identified">More qualified candidates identified</option>
                      <option value="Overqualified for the role">Overqualified for the role</option>
                      <option value="Compensation expectations exceed budget">Compensation expectations exceed budget</option>
                      <option value="Not aligned with employment type (W2 / C2C / 1099)">Not aligned with employment type (W2 / C2C / 1099)</option>
                      <option value="Work authorization / visa constraints">Work authorization / visa constraints</option>
                      <option value="Not comfortable with background check / drug test">Not comfortable with background check / drug test</option>
                      <option value="Not local and not open to relocation">Not local and not open to relocation</option>
                      <option value="Open to remote only">Open to remote only</option>
                      <option value="Not available within required timeline">Not available within required timeline</option>
                      <option value="Accepted another offer">Accepted another offer</option>
                      <option value="Candidate withdrew interest">Candidate withdrew interest</option>
                      <option value="Career gap concern">Career gap concern</option>
                      <option value="Job Hopping (short-term engagements throughout or in the last 5-7 years)">Job Hopping (short-term engagements throughout or in the last 5-7 years)</option>
                      <option value="Fake candidate — Multiple profiles/resumes; misrepresentation of past experience">Fake candidate — Multiple profiles/resumes; misrepresentation of past experience</option>
                      <option value="Already submitted to same client / hiring manager by another vendor">Already submitted to same client / hiring manager by another vendor</option>
                      <option value="Previously rejected by client">Previously rejected by client</option>
                      <option value="Not eligible for rehire">Not eligible for rehire</option>
                      <option value="Past performance concern (Internal note as per past Pyramid client feedback)">Past performance concern (Internal note as per past Pyramid client feedback)</option>
                    </select>
                  </div>
                </div>
                <div className="px-6 py-4 border-t border-slate-100 bg-slate-50 flex justify-end gap-3">
                  <button 
                    onClick={() => setIntegrationModalOpen(null)} 
                    className="px-4 py-2 text-sm font-semibold text-slate-600 hover:bg-slate-100 rounded-lg transition-all"
                  >
                    Cancel
                  </button>
                  <button
                    className="px-4 py-2 bg-rose-600 hover:bg-rose-700 text-white text-sm font-bold rounded-lg shadow-md transition-all disabled:opacity-50"
                    onClick={handleConfirmReject}
                    disabled={!rejectReason || !!syncingCandidateId}
                  >
                    {syncingCandidateId ? 'Syncing...' : 'Confirm Rejection'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function StatusPill({ status, type }: { status: string; type: "success" | "danger" | "neutral" | "info" }) {
  const themes = {
    success: "bg-[#e8fbf0] text-[#107d4f] border-[#b2f0d1]",
    danger: "bg-[#fff1f2] text-[#be123c] border-[#fecdd3]",
    neutral: "bg-[#fffbeb] text-[#b45309] border-[#fde68a]",
    info: "bg-[#eef2ff] text-[#4338ca] border-[#c7d2fe]",
  };
  
  return (
    <span className={cn(
      "inline-flex items-center gap-1.5 px-3 py-1 rounded-full font-extrabold tracking-widest border text-[11px] shadow-sm",
      themes[type]
    )}>
      <Circle className="w-2 h-2 fill-current" />
      {status === "Pass" ? "Pass" : status}
    </span>
  );
}

function AIPostingJobDescription({ text }: { text: string }) {
  const renderInline = (content: string) => {
    const parts = content.split(/(\[.*?\]\(.*?\)+|\*\*.*?\*\*|\*(?!\*).*?\*(?!\*))/g);
    return parts.map((part, i) => {
      if (part.startsWith('[') && part.includes('](') && part.endsWith(')')) {
        const match = part.match(/\[(.*?)\]\((.*?)\)/);
        if (match) {
          return (
            <a key={i} href={match[2]} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:underline">
              {match[1]}
            </a>
          );
        }
      } else if (part.startsWith('**') && part.endsWith('**')) {
        return <strong key={i} className="font-bold text-slate-900">{part.slice(2, -2)}</strong>;
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

      const isHeader = /^\*\*[A-Z\s]+\*\*$/.test(trimmedLine) || /^[A-Z\s]{3,25}$/.test(trimmedLine);
      if (isHeader) {
        const title = trimmedLine.replace(/\*\*/g, '').trim();
        return (
          <div key={index} className="text-[14px] font-bold text-slate-900 mt-5 mb-2 first:mt-0 uppercase tracking-tight">
            {title}
          </div>
        );
      }

      if (trimmedLine.startsWith('•') || trimmedLine.startsWith('-')) {
        const content = trimmedLine.replace(/^[•-]\s*/, '').trim();
        return (
          <div key={index} className="flex gap-2.5 ml-1 my-1.5 items-start">
            <span className="text-slate-400 mt-1">•</span>
            <div className="flex-1 text-[14px]">{renderInline(content)}</div>
          </div>
        );
      }

      return (
        <div key={index} className="mb-2 text-slate-600 leading-relaxed text-[14px]">
          {renderInline(trimmedLine)}
        </div>
      );
    });
  };

  return <div className="text-[14px] font-medium leading-relaxed">{formatLines(text)}</div>;
}
