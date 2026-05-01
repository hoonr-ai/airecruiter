"use client";

import { useState, useEffect } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  Download,
  CheckCircle2,
  XCircle,
  Mail,
  Phone,
  MapPin,
  Calendar,
  Briefcase,
  Building2,
  FileText,
  User,
  MessageSquare,
  Loader2,
  AlertCircle,
  Clock,
  Printer,
  ChevronDown,
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
  };
  scores: {
    resume_match_score: number;
    engage_score: number;
    engage_status: string;
    hard_filter_status: string;
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
        const res = await fetch(`${API_BASE}/candidates/${encodeURIComponent(candidateId as string)}/evaluation-report?job_id=${jobId}`);
        if (!res.ok) throw new Error("Failed to fetch evaluation report");
        const json = await res.json();
        setData(json);
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

  if (isLoading) {
    return (
      <div className="max-w-[1000px] mx-auto p-8 space-y-8">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-[600px] w-full rounded-xl" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="max-w-[1000px] mx-auto p-8 flex flex-col items-center justify-center min-h-[60vh] space-y-4">
        <AlertCircle className="w-16 h-16 text-rose-500" />
        <h1 className="text-2xl font-bold text-slate-900">Error Loading Report</h1>
        <p className="text-slate-500">{error || "Could not load report."}</p>
        <Button onClick={() => window.location.reload()}>Try Again</Button>
      </div>
    );
  }

  const { candidate, scores, job, pair } = data;

  return (
    <div className="max-w-[1100px] mx-auto px-6 py-8 space-y-8 font-sans bg-white">
      {/* Back Link */}
      <button
        onClick={() => router.back()}
        className="flex items-center gap-2 text-slate-400 hover:text-slate-600 transition-colors text-sm font-medium"
      >
        <ArrowLeft className="w-4 h-4" />
        Back to Candidate Rankings
      </button>

      {/* Header */}
      <div className="flex justify-between items-start border-b border-slate-100 pb-6">
        <div>
          <h1 className="text-3xl font-bold text-slate-900 tracking-tight">Candidate Evaluation Report</h1>
          <p className="text-slate-500 text-sm mt-1">Printable report for <span className="font-semibold">{candidate.name}</span></p>
        </div>
        <div className="flex gap-3">
          <Button className="bg-emerald-500 hover:bg-emerald-600 text-white font-bold h-11 px-8 gap-2">
            <CheckCircle2 className="w-4 h-4" /> Submit
          </Button>
          <Button variant="outline" className="text-rose-500 border-rose-500 hover:bg-rose-50 font-bold h-11 px-8 gap-2">
            <XCircle className="w-4 h-4" /> Reject
          </Button>
        </div>
      </div>

      {/* Candidate Details Section */}
      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden shadow-sm">
        <div className="bg-slate-50/50 px-6 py-4 border-b border-slate-100">
          <h3 className="text-[14px] font-bold text-slate-900 uppercase tracking-wider">Candidate Details</h3>
        </div>
        <div className="p-8 grid grid-cols-1 md:grid-cols-2 gap-y-6 gap-x-12">
          <DetailItem icon={User} label="Name" value={candidate.name} isPrimary />
          <DetailItem icon={Mail} label="Email" value={candidate.email} href={`mailto:${candidate.email}`} />
          <DetailItem icon={Phone} label="Phone" value={candidate.phone} />
          <DetailItem icon={Calendar} label="Available" value={candidate.availability || "Immediately"} />
        </div>
      </div>

      {/* Stage Results Section */}
      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden shadow-sm">
        <div className="bg-slate-50/50 px-6 py-4 border-b border-slate-100">
          <h3 className="text-[14px] font-bold text-slate-900 uppercase tracking-wider">Stage Results</h3>
        </div>
        <div className="p-8 grid grid-cols-2 gap-12">
          <div className="space-y-4">
            <h4 className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">RESUME SCREENING</h4>
            <div className="flex items-center gap-4">
              <StatusPill status="Pass" color="emerald" />
              <div className="text-sm font-bold text-slate-700">Score: {scores.resume_match_score}</div>
            </div>
          </div>
          <div className="space-y-4">
            <h4 className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">ENGAGE (L1 SCREEN)</h4>
            <div className="flex items-center gap-4">
              <StatusPill status={scores.engage_status === "Completed" ? "Pass" : "Completed"} color="emerald" />
              <div className="text-sm font-bold text-slate-700">Score: {scores.engage_score}</div>
            </div>
          </div>
        </div>
      </div>

      {/* Position Details Section */}
      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden shadow-sm">
        <div className="bg-slate-50/50 px-6 py-4 border-b border-slate-100">
          <h3 className="text-[14px] font-bold text-slate-900 uppercase tracking-wider">Position Details</h3>
        </div>
        <div className="p-8 space-y-8">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-y-6 gap-x-12">
            <DetailItem icon={Building2} label="Company" value={job.customer_name} />
            <DetailItem icon={Briefcase} label="Job Title" value={job.title} />
            <DetailItem icon={MapPin} label="Job Location" value={job.job_location} />
            <DetailItem icon={FileText} label="JobDiva ID" value={job.jobdiva_id} />
            <DetailItem icon={Clock} label="Pay Range" value={job.pay_rate} />
          </div>
          <div className="space-y-3">
            <h4 className="text-[12px] font-bold text-slate-900 uppercase">Job Summary:</h4>
              <AIPostingJobDescription text={job.ai_description} />
          </div>
        </div>
      </div>

      {/* Resume-Screening Section */}
      <div className="bg-white rounded-xl border border-indigo-600 overflow-hidden shadow-md">
        <div className="bg-indigo-600 px-6 py-4">
          <h3 className="text-[15px] font-bold text-white uppercase tracking-wider">Resume-Screening</h3>
        </div>
        <div className="p-8 space-y-6">
          <h4 className="text-[13px] font-bold text-slate-900 uppercase">Hand Filters Result</h4>
          <div className="border border-slate-200 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 border-b border-slate-200">
                <tr>
                  <th className="text-left py-3 px-4 font-bold text-slate-600 text-[11px] uppercase tracking-wider">Filter Name</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {job.resume_match_filters?.map((filter: any, i: number) => (
                  <tr key={i} className="hover:bg-slate-50/50 transition-colors">
                    <td className="py-4 px-4 text-slate-700 font-medium leading-normal">{filter.value || filter}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center gap-3 pt-2">
            <span className="text-sm font-bold text-slate-700">Overall Hard Filter Status:</span>
            <StatusPill status="Pass" color="emerald" />
          </div>
        </div>
      </div>

      {/* ENGAGE Section */}
      <div className="bg-white rounded-xl border border-indigo-600 overflow-hidden shadow-md">
        <div className="bg-indigo-600 px-6 py-4 flex justify-between items-center">
          <h3 className="text-[15px] font-bold text-white uppercase tracking-wider">ENGAGE</h3>
        </div>
        <div className="p-8 space-y-10">
          
          {/* Evaluation Table */}
          <div className="space-y-4">
            <h4 className="text-[13px] font-bold text-slate-900 uppercase tracking-tight">Evaluation:</h4>
            <div className="border border-slate-200 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <tbody className="divide-y divide-slate-100">
                  {pair.evaluation && Object.entries(pair.evaluation).filter(([k, v]) => (typeof v === 'string' || typeof v === 'number') && !['candidate_id', 'interview_id'].includes(k)).length > 0 ? (
                    Object.entries(pair.evaluation)
                      .filter(([k, v]) => (typeof v === 'string' || typeof v === 'number') && !['candidate_id', 'interview_id'].includes(k))
                      .map(([field, value]: [string, any], i) => (
                        <tr key={i} className="group hover:bg-slate-50/50 transition-colors">
                          <td className="py-3 px-4 text-slate-500 font-medium w-1/2">{field}</td>
                          <td className="py-3 px-4 text-slate-900 font-bold">{String(value)}</td>
                        </tr>
                      ))
                  ) : (
                    <tr>
                      <td className="py-8 px-4 text-center text-slate-400 italic" colSpan={2}>No evaluation data available.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* Engage Status */}
          <div className="flex items-center gap-3">
            <span className="text-sm font-bold text-slate-700">Engage Status:</span>
            <StatusPill status="Pass" color="emerald" />
          </div>

          {/* Screening Conditions Result */}
          <div className="space-y-4">
            <h4 className="text-[13px] font-bold text-slate-900 uppercase">Screening Conditions Result</h4>
            <div className="border border-slate-200 rounded-lg overflow-hidden space-y-[1px] bg-slate-200 shadow-sm">
              {(pair.evaluation?.hard_filters || pair.questions_answers || job.screen_questions || []).map((q: any, i: number) => {
                const status = (q.pass_fail || q.status || "PENDING").toUpperCase();
                const isPass = status === "PASS";
                return (
                  <div key={i} className="flex justify-between items-center py-3 px-5 bg-white group hover:bg-slate-50 transition-colors">
                    <span className="text-sm font-medium text-slate-700">{q.question_text || q.label || q}</span>
                    <span className={cn(
                      "px-3 py-1 rounded-md text-[10px] font-black uppercase tracking-widest",
                      isPass ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"
                    )}>
                      {status}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Conversations */}
          <div className="space-y-6">
            <h4 className="text-[13px] font-bold text-slate-900 uppercase">Conversations:</h4>
            <div className="space-y-6">
              {pair.transcriptions?.length > 0 ? (
                pair.transcriptions.map((msg: any, i: number) => {
                  const isBot = msg.speaker_type === "bot" || msg.role === "assistant";
                  return (
                    <div key={i} className={cn("flex flex-col gap-2", isBot ? "items-start" : "items-end")}>
                      <span className={cn("text-[10px] font-black uppercase tracking-widest", isBot ? "text-indigo-600" : "text-slate-400")}>
                        {isBot ? "ALEX (ASSISTANT)" : "CANDIDATE"}
                      </span>
                      <div className={cn(
                        "max-w-[85%] p-5 rounded-2xl text-[14px] leading-relaxed font-medium shadow-sm border",
                        isBot 
                          ? "bg-indigo-50 border-indigo-100 text-indigo-900 rounded-tl-none" 
                          : "bg-white border-slate-200 text-slate-800 rounded-tr-none"
                      )}>
                        {msg.message_text || msg.text || msg.content}
                      </div>
                    </div>
                  );
                })
              ) : (
                <div className="text-center py-12 text-slate-400 italic">No conversation transcript available.</div>
              )}
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}

function DetailItem({ icon: Icon, label, value, href, isPrimary }: { icon: any; label: string; value: string | number; href?: string; isPrimary?: boolean }) {
  return (
    <div className="flex items-center gap-4">
      <div className="w-10 h-10 rounded-full bg-slate-50 flex items-center justify-center text-slate-400 shrink-0">
        <Icon className="w-5 h-5" />
      </div>
      <div className="flex flex-col">
        <span className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">{label}</span>
        {href ? (
          <a href={href} className="text-sm font-bold text-indigo-600 hover:underline">{value}</a>
        ) : (
          <span className={cn("text-sm font-bold", isPrimary ? "text-slate-900 text-base" : "text-slate-700")}>{value || "—"}</span>
        )}
      </div>
    </div>
  );
}

function StatusPill({ status, color }: { status: string; color: "emerald" | "rose" }) {
  const colors = {
    emerald: "bg-emerald-500 text-white",
    rose: "bg-rose-500 text-white",
  };
  return (
    <span className={cn(
      "px-3 py-1 rounded-full text-[11px] font-bold flex items-center gap-1.5 uppercase tracking-wider",
      colors[color]
    )}>
      <div className="w-1.5 h-1.5 rounded-full bg-white" />
      {status}
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

  return <div className="text-[13px] font-medium leading-relaxed bg-slate-50 p-6 rounded-lg border border-slate-100">{formatLines(text)}</div>;
}
