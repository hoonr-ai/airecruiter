"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { 
  Clock, 
  Mail, 
  MessageSquare, 
  Phone, 
  CheckCircle2, 
  XCircle, 
  AlertCircle,
  Activity,
  Calendar,
  User,
  ExternalLink,
  ChevronRight,
  Info
} from "lucide-react";
import { api } from "@/lib/api";
import { shouldShowQuestionsCompleted } from "@/lib/activityTimeline";

const formatActivityDate = (dateString: string) => {
  try {
    const date = new Date(dateString);
    return new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      hour12: true,
    }).format(date);
  } catch (e) {
    return dateString;
  }
};

interface ActivityLog {
  id: number;
  phase: string;
  activity_type: string;
  activity_subtype?: string;
  status: string;
  details?: any;
  timestamp: string;
}

interface UserActivityLogModalProps {
  isOpen: boolean;
  onClose: () => void;
  interviewId: string;
  candidateName: string;
}

export function UserActivityLogModal({
  isOpen,
  onClose,
  interviewId,
  candidateName,
}: UserActivityLogModalProps) {
  const [logs, setLogs] = useState<ActivityLog[]>([]);
  const [resolvedQuestionsCompleted, setResolvedQuestionsCompleted] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const extractQuestionsCompleted = (payload: any): number | null => {
    const data = payload?.data ?? payload;
    const candidateValues = [
      data?.summary?.questions_completed,
      data?.questions_completed,
      data?.interview?.questions_completed,
    ];
    for (const value of candidateValues) {
      if (typeof value === "number" && Number.isFinite(value)) {
        return value;
      }
    }
    return null;
  };


  useEffect(() => {
    if (isOpen && interviewId) {
      loadLogs();
    }
  }, [isOpen, interviewId]);

  const loadLogs = async () => {
    try {
      setLoading(true);
      setError(null);
      const [activityResult, evaluationResult, scoreSummaryResult] = await Promise.allSettled([
        api.engagement.getActivityLogs(interviewId),
        api.engagement.getInterviewEvaluation(interviewId),
        api.engagement.getInterviewScoreSummary(interviewId),
      ]);

      const fromEvaluation =
        evaluationResult.status === "fulfilled"
          ? extractQuestionsCompleted(evaluationResult.value)
          : null;
      const fromScoreSummary =
        scoreSummaryResult.status === "fulfilled"
          ? extractQuestionsCompleted(scoreSummaryResult.value)
          : null;
      setResolvedQuestionsCompleted(
        typeof fromEvaluation === "number"
          ? fromEvaluation
          : (typeof fromScoreSummary === "number" ? fromScoreSummary : null)
      );

      if (activityResult.status === "fulfilled" && activityResult.value.success) {
        setLogs(activityResult.value.data.activities || []);
      } else {
        const activityError =
          activityResult.status === "fulfilled"
            ? activityResult.value.message
            : null;
        setError(activityError || "Failed to load activity logs");
      }
    } catch (err: any) {
      setError(err.message || "An error occurred while fetching activity logs");
    } finally {
      setLoading(false);
    }
  };

  const getIcon = (type: string, subtype?: string) => {
    if (type === "communication_sent") {
      if (subtype === "email") return <Mail className="w-4 h-4" />;
      if (subtype === "sms") return <MessageSquare className="w-4 h-4" />;
      return <Mail className="w-4 h-4" />;
    }
    if (type === "call_initiated" || type === "call_attempt" || type.startsWith("call_status_")) return <Phone className="w-4 h-4" />;
    if (type === "phase_transition") return <ChevronRight className="w-4 h-4" />;
    if (type === "token_assigned" || type === "token_retry") return <ExternalLink className="w-4 h-4" />;
    if (type === "interview_partial_completed") return <AlertCircle className="w-4 h-4" />;
    if (type.includes("started") || type.includes("joined")) return <Activity className="w-4 h-4" />;
    if (type.includes("completed") || type.includes("finished")) return <CheckCircle2 className="w-4 h-4" />;
    return <Info className="w-4 h-4" />;
  };

  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case "success":
      case "completed":
      case "sent":
        return "text-emerald-600 bg-emerald-50 border-emerald-100";
      case "failed":
      case "error":
        return "text-rose-600 bg-rose-50 border-rose-100";
      case "pending":
      case "initiated":
        return "text-amber-600 bg-amber-50 border-amber-100";
      case "partial":
      case "started":
        return "text-sky-600 bg-sky-50 border-sky-100";
      default:
        return "text-slate-600 bg-slate-50 border-slate-100";
    }
  };

  const formatActivityType = (type: string) => {
    const labels: Record<string, string> = {
      communication_sent: "Communication Sent",
      call_initiated: "Call Initiated",
      call_status_no_answer: "Call No Answer",
      call_status_busy: "Call Busy",
      call_status_failed: "Call Failed",
      call_status_canceled: "Call Canceled",
      call_status_unknown: "Call Status Updated",
      phase_transition: "Phase Updated",
      token_assigned: "Interview Link Created",
      token_retry: "Interview Link Recreated",
      interview_started_web: "Interview Launched",
      interview_started_call: "Interview Started By Call",
      interview_partial_completed: "Interview Partially Completed",
      interview_completed: "Interview Completed",
    };
    if (labels[type]) return labels[type];

    return type
      .split("_")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-2xl bg-white rounded-[24px] shadow-2xl border border-slate-100 p-0 overflow-hidden flex flex-col max-h-[85vh]">
        <div className="sr-only">
          <DialogTitle>Activity Logs for {candidateName}</DialogTitle>
          <DialogDescription>Timeline of all interactions and status changes for {candidateName}.</DialogDescription>
        </div>

        {/* Header */}
        <div className="px-8 py-6 bg-gradient-to-r from-slate-50 to-white border-b border-slate-100">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-indigo-600 flex items-center justify-center shadow-lg shadow-indigo-100">
                <Activity className="w-6 h-6 text-white" />
              </div>
              <div>
                <h2 className="text-xl font-bold text-slate-900 tracking-tight">{candidateName}</h2>
                <div className="flex items-center gap-2 mt-1 text-sm text-slate-500 font-medium">
                  <span className="flex items-center gap-1">
                    <Calendar className="w-3.5 h-3.5" />
                    Activity Timeline
                  </span>
                  <span className="w-1 h-1 rounded-full bg-slate-300" />
                  <span className="text-indigo-600">ID: {interviewId}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-8 py-6">
          {loading ? (
            <div className="flex flex-col items-center justify-center py-20 space-y-4">
              <div className="w-10 h-10 border-4 border-indigo-600/20 border-t-indigo-600 rounded-full animate-spin" />
              <p className="text-slate-500 font-medium">Fetching history...</p>
            </div>
          ) : error ? (
            <div className="flex flex-col items-center justify-center py-12 px-6 text-center">
              <div className="w-16 h-16 bg-rose-50 rounded-full flex items-center justify-center mb-4">
                <AlertCircle className="w-8 h-8 text-rose-500" />
              </div>
              <h3 className="text-lg font-bold text-slate-900">Unable to load logs</h3>
              <p className="text-slate-500 mt-2 max-w-xs">{error}</p>
              <Button 
                onClick={loadLogs} 
                variant="outline" 
                className="mt-6 border-slate-200 hover:bg-slate-50 rounded-xl"
              >
                Try Again
              </Button>
            </div>
          ) : logs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <div className="w-20 h-20 bg-slate-50 rounded-full flex items-center justify-center mb-4">
                <Clock className="w-10 h-10 text-slate-300" />
              </div>
              <h3 className="text-lg font-bold text-slate-900">No activity yet</h3>
              <p className="text-slate-500 mt-2">Interactions will appear here as they happen.</p>
            </div>
          ) : (
            <div className="relative">
              {/* Timeline Line */}
              <div className="absolute left-4 top-0 bottom-0 w-0.5 bg-slate-100" />

              <div className="space-y-8 relative">
                {logs.map((log, index) => {
                  const questionsCompletedValue =
                    log.activity_type === "interview_completed" && typeof resolvedQuestionsCompleted === "number"
                      ? resolvedQuestionsCompleted
                      : typeof log.details?.questions_completed === "number"
                        ? log.details.questions_completed
                        : log.activity_type === "questionnaire_submitted" && typeof log.details?.answer_count === "number"
                          ? log.details.answer_count
                          : null;
                  return (
                  <div key={log.id || index} className="flex gap-6 group">
                    {/* Icon Point */}
                    <div className={`relative z-10 w-9 h-9 rounded-full border-4 border-white shadow-sm flex items-center justify-center shrink-0 ${getStatusColor(log.status)} transition-transform group-hover:scale-110`}>
                      {getIcon(log.activity_type, log.activity_subtype)}
                    </div>

                    {/* Content Card */}
                    <div className="flex-1 pt-1">
                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                        <h4 className="font-bold text-slate-900">
                          {formatActivityType(log.activity_type)}
                          {log.activity_subtype && (
                            <span className="ml-2 text-xs font-medium text-slate-400 uppercase tracking-wider">
                              • {log.activity_subtype}
                            </span>
                          )}
                        </h4>
                        <time className="text-xs font-bold text-slate-400 uppercase tracking-widest whitespace-nowrap">
                          {formatActivityDate(log.timestamp)}
                        </time>
                      </div>

                      <div className="mt-2 bg-slate-50/50 rounded-2xl border border-slate-100 p-4 transition-colors group-hover:bg-slate-50 group-hover:border-slate-200">
                        <div className="flex items-center gap-2 mb-2">
                          <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full uppercase tracking-widest border ${getStatusColor(log.status)}`}>
                            {log.status}
                          </span>
                          <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">
                            Phase: {log.phase}
                          </span>
                        </div>

                        {log.details && (
                          <div className="text-sm text-slate-600 leading-relaxed font-medium">
                            {log.activity_type === "communication_sent" && log.details.subject && (
                              <p className="text-slate-900 font-bold mb-1">Subject: {log.details.subject}</p>
                            )}
                            {log.activity_type === "communication_sent" && log.details.message_type && (
                              <p className="text-slate-700">
                                Type: {String(log.details.message_type).replace(/_/g, " ")}
                              </p>
                            )}
                            {log.activity_type === "phase_transition" && (
                              <p className="text-slate-900">
                                Phase changed from {log.details.old_phase || "unknown"} to {log.details.new_phase || log.phase}.
                              </p>
                            )}
                            {(log.activity_type === "token_assigned" || log.activity_type === "token_retry") && (
                              <p className="text-slate-900">
                                Candidate interview link was {log.activity_type === "token_retry" ? "recreated" : "created"}.
                              </p>
                            )}
                            {log.activity_type === "interview_started_web" && (
                              <p className="text-slate-900">
                                Candidate launched the web interview
                                {log.details.launch_context === "partial_reminder_resume"
                                  ? " from a partial reminder"
                                  : ""}.
                              </p>
                            )}
                            {log.activity_type === "interview_partial_completed" && (
                              <p className="text-slate-900">
                                Candidate left after partially completing the interview.
                              </p>
                            )}
                            {log.activity_type === "interview_completed" && (
                              <p className="text-slate-900">
                                Candidate completed the interview successfully.
                              </p>
                            )}
                            {shouldShowQuestionsCompleted(log.activity_type) &&
                             questionsCompletedValue !== null && (
                              <p className="text-slate-700">
                                Questions completed: {questionsCompletedValue}
                              </p>
                            )}
                            {log.details.session_started_at && (
                              <p className="text-slate-700">
                                Interview launched: {formatActivityDate(log.details.session_started_at)}
                              </p>
                            )}
                            {typeof log.details.status === "string" && log.activity_type.startsWith("call_status_") && (
                              <p className="text-slate-700">
                                Call status: {log.details.status.replace(/_/g, " ")}
                              </p>
                            )}
                            {log.details.message && <p className="italic text-slate-500">"{log.details.message.substring(0, 150)}{log.details.message.length > 150 ? '...' : ''}"</p>}
                            {log.details.content && <p className="italic text-slate-500">"{log.details.content.substring(0, 150)}{log.details.content.length > 150 ? '...' : ''}"</p>}
                            {log.details.phone_number && <p className="flex items-center gap-1.5"><Phone className="w-3.5 h-3.5 text-slate-400" /> {log.details.phone_number}</p>}
                            {log.details.error && <p className="text-rose-500 mt-1 bg-rose-50/50 p-2 rounded-lg border border-rose-100/50 text-xs font-semibold">{log.details.error}</p>}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-8 py-4 bg-white border-t border-slate-100 flex items-center justify-end">
          <Button
            onClick={onClose}
            variant="ghost"
            className="text-slate-400 hover:text-slate-700 text-sm font-bold px-6 rounded-xl h-11"
          >
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
