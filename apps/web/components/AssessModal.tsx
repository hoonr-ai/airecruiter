"use client";

import { useState, useEffect } from "react";
import { API_BASE } from "@/lib/api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  CheckCircle2,
  XCircle,
  Clock,
  AlertCircle,
  MessageSquare,
  BarChart3,
  FileText,
  Radio,
  Loader2,
  Copy,
  Check,
  User,
  Bot,
  Download,
} from "lucide-react";

interface AssessModalProps {
  open: boolean;
  onClose: () => void;
  interviewId: string | null;
  candidateName: string;
}

interface AssessmentData {
  interview: any;
  evaluation: any;
  transcriptions: any[];
  outreach: any;
}

export function AssessModal({
  open,
  onClose,
  interviewId,
  candidateName,
}: AssessModalProps) {
  const [data, setData] = useState<AssessmentData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (open && interviewId) {
      fetchAssessmentData();
    }
    if (!open) {
      setData(null);
      setError(null);
    }
  }, [open, interviewId]);

  const fetchAssessmentData = async () => {
    if (!interviewId) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/engagement/assess/${interviewId}`
      );
      if (!response.ok) throw new Error("Failed to fetch assessment data");
      const result = await response.json();
      setData({
        interview: result.interview,
        evaluation: result.evaluation,
        transcriptions: result.transcriptions || [],
        outreach: result.outreach,
      });
    } catch (err: any) {
      setError(err.message || "Failed to load assessment");
    } finally {
      setLoading(false);
    }
  };

  const copyInterviewId = () => {
    if (interviewId) {
      navigator.clipboard.writeText(interviewId);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const getStatusConfig = (status: string | undefined) => {
    switch (status?.toLowerCase()) {
      case "completed":
        return {
          icon: <CheckCircle2 className="w-4 h-4" />,
          color: "bg-emerald-100 text-emerald-700 border-emerald-200",
          label: "Completed",
        };
      case "in_progress":
      case "in-progress":
      case "initiated":
        return {
          icon: <Clock className="w-4 h-4" />,
          color: "bg-blue-100 text-blue-700 border-blue-200",
          label: "In Progress",
        };
      case "failed":
        return {
          icon: <XCircle className="w-4 h-4" />,
          color: "bg-rose-100 text-rose-700 border-rose-200",
          label: "Failed",
        };
      default:
        return {
          icon: <AlertCircle className="w-4 h-4" />,
          color: "bg-amber-100 text-amber-700 border-amber-200",
          label: status || "Pending",
        };
    }
  };

  const getScoreColor = (score: number, max: number = 10) => {
    const pct = (score / max) * 100;
    if (pct >= 80) return "bg-emerald-500";
    if (pct >= 60) return "bg-amber-500";
    return "bg-rose-500";
  };

  // ---- No interview state ----
  if (!interviewId) {
    return (
      <Dialog open={open} onOpenChange={onClose}>
        <DialogContent className="sm:max-w-[500px]">
          <DialogHeader>
            <DialogTitle className="text-lg font-bold text-slate-900">
              Assessment â€” {candidateName}
            </DialogTitle>
          </DialogHeader>
          <div className="flex flex-col items-center justify-center py-12 text-center gap-3">
            <div className="w-16 h-16 bg-slate-50 rounded-full flex items-center justify-center">
              <AlertCircle className="w-8 h-8 text-slate-300" />
            </div>
            <h3 className="text-[16px] font-bold text-slate-900">
              No Interview Data
            </h3>
            <p className="text-[13px] text-slate-500 max-w-[300px]">
              This candidate hasn&apos;t been engaged yet. Click the Engage
              button first to send an interview.
            </p>
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[740px] max-h-[85vh] p-0 overflow-hidden">
        {/* Header */}
        <div className="px-6 pt-6 pb-4 border-b border-slate-100">
          <DialogHeader>
            <div className="flex items-center justify-between">
              <DialogTitle className="text-lg font-bold text-slate-900">
                Assessment - {candidateName}
              </DialogTitle>
              {data?.interview && (
                <Badge
                  className={`${getStatusConfig(data.interview.status).color} border text-[11px] font-bold flex items-center gap-1.5 px-2.5 py-1`}
                >
                  {getStatusConfig(data.interview.status).icon}
                  {getStatusConfig(data.interview.status).label}
                </Badge>
              )}
            </div>
          </DialogHeader>

          {/* Interview ID badge */}
          <div className="flex items-center gap-2 mt-3">
            <span className="text-[11px] text-slate-400 font-medium uppercase tracking-wide">
              Interview ID:
            </span>
            <code className="text-[12px] bg-slate-50 border border-slate-200 px-2 py-0.5 rounded font-mono text-slate-600">
              {interviewId}
            </code>
            <button
              onClick={copyInterviewId}
              className="p-1 rounded hover:bg-slate-100 transition-colors"
              title="Copy Interview ID"
            >
              {copied ? (
                <Check className="w-3.5 h-3.5 text-emerald-500" />
              ) : (
                <Copy className="w-3.5 h-3.5 text-slate-400" />
              )}
            </button>
          </div>
        </div>

        {/* Body */}
        {loading ? (
          <div className="flex flex-col items-center justify-center py-20 gap-3">
            <Loader2 className="w-8 h-8 text-[#6366f1] animate-spin" />
            <p className="text-[13px] text-slate-500 font-medium">
              Loading assessment data...
            </p>
          </div>
        ) : error ? (
          <div className="flex flex-col items-center justify-center py-20 gap-3 text-center">
            <XCircle className="w-10 h-10 text-rose-300" />
            <p className="text-[14px] font-bold text-slate-900">
              Failed to Load
            </p>
            <p className="text-[12px] text-slate-500 max-w-[300px]">{error}</p>
            <Button
              size="sm"
              onClick={fetchAssessmentData}
              className="mt-2 bg-[#6366f1] hover:bg-[#4f46e5] text-white"
            >
              Retry
            </Button>
          </div>
        ) : data ? (
          <Tabs defaultValue="overview" className="flex flex-col h-full">
            <TabsList className="mx-6 mt-3 bg-slate-100 rounded-lg p-1 h-10">
              <TabsTrigger
                value="overview"
                className="text-[12px] font-bold gap-1.5 data-[state=active]:bg-white data-[state=active]:shadow-sm rounded-md"
              >
                <BarChart3 className="w-3.5 h-3.5" />
                Overview
              </TabsTrigger>
              <TabsTrigger
                value="evaluation"
                className="text-[12px] font-bold gap-1.5 data-[state=active]:bg-white data-[state=active]:shadow-sm rounded-md"
              >
                <FileText className="w-3.5 h-3.5" />
                Evaluation
              </TabsTrigger>
              <TabsTrigger
                value="transcript"
                className="text-[12px] font-bold gap-1.5 data-[state=active]:bg-white data-[state=active]:shadow-sm rounded-md"
              >
                <MessageSquare className="w-3.5 h-3.5" />
                Transcript
              </TabsTrigger>
              <TabsTrigger
                value="outreach"
                className="text-[12px] font-bold gap-1.5 data-[state=active]:bg-white data-[state=active]:shadow-sm rounded-md"
              >
                <Radio className="w-3.5 h-3.5" />
                Outreach
              </TabsTrigger>
            </TabsList>

            {/* ===== TAB 1: Overview ===== */}
            <TabsContent value="overview" className="px-6 pb-6 mt-0">
              <ScrollArea className="max-h-[400px] pr-3">
                <div className="space-y-5 pt-4">
                  {/* Score Card â€” use interview score or fall back to evaluation summary */}
                  {(() => {
                    const score =
                      data.interview?.overall_score ??
                      data.evaluation?.summary?.overall_score;
                    if (score === undefined || score === null) return null;
                    return (
                      <div className="bg-gradient-to-br from-indigo-50 to-white border border-indigo-100 rounded-xl p-5">
                        <div className="flex items-center justify-between">
                          <div>
                            <p className="text-[11px] text-indigo-400 font-bold uppercase tracking-wide">
                              Overall Score
                            </p>
                            <p className="text-[42px] font-black text-slate-900 leading-tight mt-1">
                              {score}
                              <span className="text-[18px] text-slate-400 font-medium">
                                /10
                              </span>
                            </p>
                            {data.evaluation?.summary && (
                              <p className="text-[12px] text-slate-500 mt-1">
                                {data.evaluation.summary.questions_completed}/{data.evaluation.summary.total_questions} questions completed
                                {data.evaluation.summary.average_score !== undefined && (
                                  <> Â· avg <span className="font-semibold">{data.evaluation.summary.average_score}</span></>
                                )}
                              </p>
                            )}
                          </div>
                          <div
                            className={`w-20 h-20 rounded-full flex items-center justify-center text-white font-black text-[15px] shadow-lg ${getScoreColor(score)}`}
                          >
                            {score >= 7.5
                              ? "PASS"
                              : score >= 5
                                ? "AVG"
                                : "FAIL"}
                          </div>
                        </div>
                      </div>
                    );
                  })()}

                  {/* Progress */}
                  <div className="grid grid-cols-2 gap-4">
                    <div className="bg-white border border-slate-200 rounded-xl p-4">
                      <p className="text-[11px] text-slate-400 font-bold uppercase tracking-wide">
                        Questions
                      </p>
                      <p className="text-[22px] font-black text-slate-900 mt-1">
                        {data.interview?.questions_completed ?? "â€”"}
                        <span className="text-[14px] text-slate-400 font-medium">
                          /{data.interview?.total_questions ?? "â€”"}
                        </span>
                      </p>
                    </div>
                    <div className="bg-white border border-slate-200 rounded-xl p-4">
                      <p className="text-[11px] text-slate-400 font-bold uppercase tracking-wide">
                        Role
                      </p>
                      <p className="text-[14px] font-bold text-slate-900 mt-1 truncate">
                        {data.interview?.role_position || "â€”"}
                      </p>
                    </div>
                  </div>

                  {/* Candidate Info */}
                  <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-2">
                    <p className="text-[11px] text-slate-400 font-bold uppercase tracking-wide">
                      Candidate Details
                    </p>
                    <div className="grid grid-cols-2 gap-3 text-[13px]">
                      <div>
                        <span className="text-slate-400">Name:</span>{" "}
                        <span className="font-medium text-slate-700">
                          {data.interview?.person_name || candidateName}
                        </span>
                      </div>
                      <div>
                        <span className="text-slate-400">Email:</span>{" "}
                        <span className="font-medium text-slate-700">
                          {data.interview?.person_email || "â€”"}
                        </span>
                      </div>
                      <div>
                        <span className="text-slate-400">Phone:</span>{" "}
                        <span className="font-medium text-slate-700">
                          {data.interview?.person_phone || "â€”"}
                        </span>
                      </div>
                      <div>
                        <span className="text-slate-400">Created:</span>{" "}
                        <span className="font-medium text-slate-700">
                          {data.interview?.created_at
                            ? new Date(
                                data.interview.created_at
                              ).toLocaleDateString("en-US", {
                                month: "short",
                                day: "numeric",
                                year: "numeric",
                              })
                            : "â€”"}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </ScrollArea>
            </TabsContent>

            {/* ===== TAB 2: Evaluation ===== */}
            <TabsContent value="evaluation" className="px-0 pb-0 mt-0">
              <ScrollArea className="h-[calc(85vh-220px)]">
                <div className="space-y-4 px-6 pt-4 pb-6">
                  {(() => {
                    const evalData = data.evaluation;

                    // Overall score â€” try several possible field paths
                    const overallScore =
                      data.interview?.overall_score ??
                      evalData?.summary?.overall_score ??
                      evalData?.overall_score ??
                      evalData?.score;

                    // Questions completed / total
                    const qCompleted =
                      evalData?.summary?.questions_completed ??
                      evalData?.questions_completed ??
                      data.interview?.questions_completed;
                    const qTotal =
                      evalData?.summary?.total_questions ??
                      evalData?.total_questions ??
                      data.interview?.total_questions;

                    // Questions array â€” PAIR API may use different keys
                    const questions: any[] =
                      (Array.isArray(evalData?.questions) && evalData.questions.length ? evalData.questions : null) ??
                      (Array.isArray(evalData?.evaluations) && evalData.evaluations.length ? evalData.evaluations : null) ??
                      (Array.isArray(evalData?.question_scores) && evalData.question_scores.length ? evalData.question_scores : null) ??
                      (Array.isArray(evalData?.question_evaluations) && evalData.question_evaluations.length ? evalData.question_evaluations : null) ??
                      (Array.isArray(evalData) && evalData.length ? evalData : null) ??
                      [];

                    const scoreLabel = (s: number) =>
                      s >= 8 ? "Excellent" : s >= 6 ? "Good" : s >= 4 ? "Average" : "Needs Improvement";

                    const scoreTextColor = (s: number) =>
                      s >= 8 ? "text-emerald-600" : s >= 6 ? "text-amber-600" : "text-rose-600";

                    return (
                      <>
                        {/* Section header */}
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <FileText className="w-4 h-4 text-[#6366f1]" />
                            <h3 className="text-[15px] font-bold text-slate-900">Evaluation & Results</h3>
                          </div>
                          {qCompleted != null && qTotal != null && (
                            <span className="text-[11px] font-semibold bg-indigo-50 text-indigo-600 border border-indigo-100 px-3 py-1 rounded-full">
                              {qCompleted} / {qTotal} Questions Answered
                            </span>
                          )}
                        </div>

                        {/* Overall score card */}
                        {overallScore != null && (
                          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 flex items-center justify-between">
                            <div>
                              <p className="text-[13px] font-bold text-slate-800">Overall Score</p>
                              <p className="text-[12px] text-slate-500 mt-0.5">Average performance across all questions</p>
                            </div>
                            <div className="text-right">
                              <p className={`text-[28px] font-black leading-none ${scoreTextColor(overallScore)}`}>
                                {overallScore}
                                <span className="text-[14px] text-slate-400 font-medium">/10.0</span>
                              </p>
                              <p className={`text-[11px] font-semibold mt-1 ${scoreTextColor(overallScore)}`}>
                                {scoreLabel(overallScore)}
                              </p>
                            </div>
                          </div>
                        )}

                        {/* Per-question cards */}
                        {questions.length > 0 ? (
                          questions.map((q: any, idx: number) => {
                            const questionText =
                              q.question ?? q.question_text ?? q.text ?? q.prompt ?? `Question ${idx + 1}`;
                            const answerText =
                              q.answer ?? q.candidate_answer ?? q.response ?? q.candidate_response ??
                              q.transcribed_answer ?? q.user_response ?? q.speech_text ?? q.text_response;
                            const score = q.score ?? q.question_score ?? q.rating ?? q.points;
                            const feedback = q.feedback ?? q.ai_feedback ?? q.comment;
                            const category = q.category ?? q.type;

                            return (
                              <div
                                key={q.question_id ?? q.id ?? idx}
                                className="bg-white border border-slate-200 rounded-xl overflow-hidden"
                              >
                                {/* Question + score row */}
                                <div className="px-4 pt-4 pb-3 flex items-start justify-between gap-4">
                                  <div className="flex items-start gap-3 flex-1 min-w-0">
                                    <div className="shrink-0 w-7 h-7 rounded-full bg-[#6366f1] text-white flex items-center justify-center text-[12px] font-black mt-0.5">
                                      {idx + 1}
                                    </div>
                                    <div className="min-w-0">
                                      {category && (
                                        <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-0.5">
                                          {category}
                                        </span>
                                      )}
                                      <p className="text-[13.5px] font-semibold text-slate-900 leading-snug">
                                        {questionText}
                                      </p>
                                    </div>
                                  </div>
                                  {score != null && (
                                    <span className={`shrink-0 text-[20px] font-black ${scoreTextColor(score)}`}>
                                      {score}
                                      <span className="text-[13px] text-slate-400 font-normal">/10</span>
                                    </span>
                                  )}
                                </div>

                                {/* Candidate response */}
                                <div className="px-4 pb-4">
                                  <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-2">
                                    Candidate&apos;s Response
                                  </p>
                                  <div className="bg-slate-50 border border-slate-200 rounded-lg px-4 py-3">
                                    <p className={`text-[13px] leading-relaxed italic ${
                                      answerText ? "text-slate-700" : "text-slate-400"
                                    }`}>
                                      {answerText ? `"${answerText}"` : "No response recorded."}
                                    </p>
                                  </div>

                                  {feedback && (
                                    <div className="mt-3 bg-indigo-50 border border-indigo-100 rounded-lg px-4 py-3">
                                      <div className="flex items-center gap-1.5 mb-1">
                                        <Bot className="w-3 h-3 text-indigo-400" />
                                        <p className="text-[10px] text-indigo-500 font-bold uppercase tracking-widest">
                                          AI Feedback
                                        </p>
                                      </div>
                                      <p className="text-[12.5px] text-indigo-700 leading-relaxed">{feedback}</p>
                                    </div>
                                  )}
                                </div>
                              </div>
                            );
                          })
                        ) : (
                          <div className="text-center py-12 text-slate-400 text-[13px]">
                            {evalData
                              ? "No question-level data returned by the API yet."
                              : "No evaluation data available yet."}
                          </div>
                        )}
                      </>
                    );
                  })()}
                </div>
              </ScrollArea>
            </TabsContent>

            {/* ===== TAB 3: Transcript ===== */}
            <TabsContent value="transcript" className="px-6 pb-6 mt-0">
              <div className="flex items-center justify-between mb-2 pt-4">
                <p className="text-[11px] text-slate-400 font-bold uppercase tracking-wide">
                  Conversation Log
                </p>
                {data.transcriptions.length > 0 && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-[11px] gap-1.5 border-slate-200 text-slate-600 hover:bg-slate-50"
                    onClick={() => {
                      window.open(`${API_BASE}/api/v1/engagement/interviews/${interviewId}/transcriptions/download`, "_blank");
                    }}
                  >
                    <Download className="w-3.5 h-3.5" />
                    Download PDF
                  </Button>
                )}
              </div>
              <ScrollArea className="h-[500px] pr-3">
                <div className="space-y-3 pt-4">
                  {data.transcriptions.length > 0 ? (
                    data.transcriptions.map((msg: any, idx: number) => (
                      <div
                        key={msg.id || idx}
                        className={`flex gap-3 ${
                          msg.speaker_type === "candidate"
                            ? "flex-row-reverse"
                            : ""
                        }`}
                      >
                        {/* Avatar */}
                        <div
                          className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${
                            msg.speaker_type === "candidate"
                              ? "bg-[#6366f1]/10"
                              : "bg-slate-100"
                          }`}
                        >
                          {msg.speaker_type === "candidate" ? (
                            <User className="w-4 h-4 text-[#6366f1]" />
                          ) : (
                            <Bot className="w-4 h-4 text-slate-500" />
                          )}
                        </div>

                        {/* Message bubble */}
                        <div
                          className={`max-w-[75%] rounded-2xl px-4 py-2.5 ${
                            msg.speaker_type === "candidate"
                              ? "bg-[#6366f1] text-white rounded-br-md"
                              : "bg-slate-100 text-slate-700 rounded-bl-md"
                          }`}
                        >
                          <p className="text-[12.5px] leading-relaxed">
                            {msg.message_text}
                          </p>
                          {msg.timestamp && (
                            <p
                              className={`text-[10px] mt-1 ${
                                msg.speaker_type === "candidate"
                                  ? "text-white/60"
                                  : "text-slate-400"
                              }`}
                            >
                              {new Date(msg.timestamp).toLocaleTimeString(
                                "en-US",
                                {
                                  hour: "2-digit",
                                  minute: "2-digit",
                                }
                              )}
                            </p>
                          )}
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="text-center py-10 text-slate-400 text-[13px]">
                      No transcript available yet.
                    </div>
                  )}
                </div>
              </ScrollArea>
            </TabsContent>

            {/* ===== TAB 4: Outreach ===== */}
            <TabsContent value="outreach" className="px-6 pb-6 mt-0">
              <ScrollArea className="max-h-[400px] pr-3">
                <div className="space-y-4 pt-4">
                  {data.outreach ? (
                    <>
                      {/* Current phase */}
                      <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
                        <p className="text-[11px] text-slate-400 font-bold uppercase tracking-wide">
                          Current Phase
                        </p>
                        <p className="text-[16px] font-bold text-slate-900 mt-1 capitalize">
                          {data.outreach.outreach?.outreach_phase?.replace(
                            "_",
                            " "
                          ) || "â€”"}
                        </p>
                        <p className="text-[12px] text-slate-500 mt-0.5">
                          Status:{" "}
                          <span className="font-medium capitalize">
                            {data.outreach.outreach?.outreach_status || "â€”"}
                          </span>
                        </p>
                      </div>

                      {/* Communication timeline */}
                      {data.outreach.communications &&
                        data.outreach.communications.length > 0 && (
                          <div>
                            <p className="text-[11px] text-slate-400 font-bold uppercase tracking-wide mb-3">
                              Communication Timeline
                            </p>
                            <div className="space-y-3 relative">
                              {/* Timeline line */}
                              <div className="absolute left-[15px] top-2 bottom-2 w-[2px] bg-slate-200" />

                              {data.outreach.communications.map(
                                (comm: any, idx: number) => (
                                  <div
                                    key={idx}
                                    className="flex items-start gap-3 relative"
                                  >
                                    {/* Dot */}
                                    <div
                                      className={`w-[10px] h-[10px] rounded-full mt-1.5 z-10 border-2 shrink-0 ml-[10px] ${
                                        comm.status === "delivered"
                                          ? "bg-emerald-500 border-emerald-200"
                                          : comm.status === "failed"
                                            ? "bg-rose-500 border-rose-200"
                                            : "bg-amber-500 border-amber-200"
                                      }`}
                                    />
                                    {/* Content */}
                                    <div className="bg-white border border-slate-200 rounded-lg p-3 flex-1">
                                      <div className="flex items-center justify-between">
                                        <div className="flex items-center gap-2">
                                          <Badge
                                            variant="outline"
                                            className="text-[10px] font-bold capitalize px-1.5 py-0"
                                          >
                                            {comm.channel}
                                          </Badge>
                                          <span className="text-[11px] text-slate-500 capitalize">
                                            {comm.step?.replace(/_/g, " ") ||
                                              comm.phase}
                                          </span>
                                        </div>
                                        <Badge
                                          className={`text-[10px] font-bold border px-1.5 py-0 ${
                                            comm.status === "delivered"
                                              ? "bg-emerald-50 text-emerald-600 border-emerald-200"
                                              : comm.status === "failed"
                                                ? "bg-rose-50 text-rose-600 border-rose-200"
                                                : "bg-amber-50 text-amber-600 border-amber-200"
                                          }`}
                                        >
                                          {comm.status}
                                        </Badge>
                                      </div>
                                      {comm.sent_at && (
                                        <p className="text-[10px] text-slate-400 mt-1">
                                          Sent:{" "}
                                          {new Date(
                                            comm.sent_at
                                          ).toLocaleString("en-US", {
                                            month: "short",
                                            day: "numeric",
                                            hour: "2-digit",
                                            minute: "2-digit",
                                          })}
                                        </p>
                                      )}
                                    </div>
                                  </div>
                                )
                              )}
                            </div>
                          </div>
                        )}
                    </>
                  ) : (
                    <div className="text-center py-10 text-slate-400 text-[13px]">
                      No outreach data available yet.
                    </div>
                  )}
                </div>
              </ScrollArea>
            </TabsContent>
          </Tabs>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
