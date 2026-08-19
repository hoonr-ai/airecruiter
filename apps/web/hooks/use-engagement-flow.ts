"use client";

import { API_BASE, authFetch } from "@/lib/api";
import { logger } from "@/lib/logger";

// Shared engagement API calls. Previously duplicated across
// app/candidates/page.tsx and components/sourced-candidates-view.tsx with
// identical fetch bodies and identical localhost fallbacks. The hook owns
// the endpoints and response shapes; callers keep their own loading/error
// state because those surfaces differ (modal vs inline).

export type GeneratePayloadInput = {
  candidateIds: string[];
  jobId: string;
};

export type GeneratePayloadResult = {
  payload: string;
};

export type SendBulkInterviewInput = {
  payload: string;
  realCandidateIds: string[];
  isInitialLaunch?: boolean;
  dryRun?: boolean;
  // Wizard "Launch PAIR" sets this so the recruiter launch email fires.
  // Manual rankings Engage clicks leave it false (the recruiter is already
  // looking at the screen — no need to notify themselves).
  notifyRecruiters?: boolean;
  // Fire job posting email once (typically on final successful batch).
  sendJobPostingEmail?: boolean;
  appBaseUrl?: string;
};

export type SendBulkInterviewResult = {
  success: boolean;
  message?: string;
  bulk_id?: string;
  data?: Array<{
    interview_id: string;
    candidate_name: string;
    candidate_email: string;
    links?: Record<string, string>;
    session_token?: string;
    created_at?: string;
  }>;
  // Candidates the backend dropped because engage_status='sent' already.
  // Populated when staged Launch PAIR retries a batch with already-launched ids.
  skipped_already_sent?: string[];
};

export type LatestInterviewResult = {
  success: boolean;
  interview_id?: string;
};

// Backend-owned Launch PAIR: the browser sends candidate IDs + options ONCE and
// consumes a single SSE stream of aggregated progress, instead of looping
// generate->send->status per batch. Résumés do NOT ride this call (they are
// persisted separately via /candidates/save), so the request body is small.
export type LaunchInput = {
  jobId: string;
  candidateIds: string[];
  isInitialLaunch?: boolean;
  notifyRecruiters?: boolean;
  sendJobPostingEmail?: boolean;
  appBaseUrl?: string;
  batchSize?: number;
};

// A candidate the BACKEND excluded at launch time (employed by hiring
// client, offer extended, current Pyramid employee, …). Distinct from the
// FE-predicted skips: the server may know company data the FE didn't see.
export type LaunchExcludedCandidate = {
  candidate_id: string;
  name?: string;
  reason: string;
};

export type LaunchEvent =
  | { type: "start"; total_candidates: number; total_batches: number; batch_size: number }
  | {
      type: "batch";
      index: number;
      status: "completed" | "failed";
      sent?: number;
      // Candidates PAIR accepted but returned no interview_id for. They are NOT
      // counted as sent and remain retryable on a subsequent launch.
      no_interview?: number;
      already_sent?: number;
      // Count of server-side exclusions in this batch (see done.excluded_candidates).
      excluded?: number;
      bulk_id?: string;
      error?: string;
      candidate_ids?: string[];
    }
  | { type: "error"; status_code?: number; message?: string }
  | {
      type: "done";
      aborted: boolean;
      totals: { sent: number; already_sent: number; failed_batches: number; no_interview?: number; excluded?: number };
      skipped_already_sent: string[];
      excluded_candidates?: LaunchExcludedCandidate[];
      failed_candidate_ids: string[];
    };

export function useEngagementFlow() {
  async function generatePayload(input: GeneratePayloadInput): Promise<GeneratePayloadResult> {
    const res = await authFetch(`${API_BASE}/api/v1/engagement/engage/generate-payload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_ids: input.candidateIds, job_id: input.jobId }),
    });
    if (!res.ok) {
      logger.error("engagement.generate_payload.failed", { status: res.status, candidateIds: input.candidateIds });
      throw new Error("Failed to generate payload");
    }
    return res.json();
  }

  async function sendBulkInterview(input: SendBulkInterviewInput): Promise<SendBulkInterviewResult> {
    // Validate JSON up-front so the caller gets a clear error before the
    // network request fires.
    try {
      JSON.parse(input.payload);
    } catch {
      throw new Error("Invalid JSON format in payload");
    }
    const res = await authFetch(`${API_BASE}/api/v1/engagement/engage/send-bulk-interview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        payload: input.payload,
        real_candidate_ids: input.realCandidateIds,
        is_initial_launch: input.isInitialLaunch ?? false,
        dry_run: input.dryRun ?? false,
        notify_recruiters: input.notifyRecruiters ?? false,
        send_job_posting_email: input.sendJobPostingEmail,
        app_base_url: input.appBaseUrl || (typeof window !== "undefined" ? window.location.origin : ""),
      }),
    });
    let data: any = null;
    try {
      data = await res.json();
    } catch {
      data = { success: false, message: "Invalid JSON response from server" };
    }
    
    if (!res.ok) {
      const errorMsg = data?.message || (typeof data?.detail === 'string' ? data.detail : "API request failed");
      logger.error("engagement.send_bulk.failed", { status: res.status, message: errorMsg });
      // Ensure we return an object even on error
      return (data || { success: false, message: errorMsg }) as SendBulkInterviewResult;
    }
    return (data || { success: true, message: "Success" }) as SendBulkInterviewResult;
  }

  // Single-call launch. POSTs candidate IDs to /engage/launch and streams
  // aggregated progress back via SSE. Uses a streaming fetch reader (not
  // EventSource) because EventSource cannot POST a body or set auth headers.
  // `onEvent` is invoked for every parsed SSE event; resolves when the stream
  // ends.
  async function launch(input: LaunchInput, onEvent: (evt: LaunchEvent) => void): Promise<void> {
    const res = await authFetch(`${API_BASE}/api/v1/engagement/engage/launch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: input.jobId,
        candidate_ids: input.candidateIds,
        is_initial_launch: input.isInitialLaunch ?? false,
        notify_recruiters: input.notifyRecruiters ?? false,
        send_job_posting_email: input.sendJobPostingEmail,
        app_base_url: input.appBaseUrl || (typeof window !== "undefined" ? window.location.origin : ""),
        batch_size: input.batchSize,
      }),
    });
    if (!res.ok || !res.body) {
      let msg = "";
      try {
        const j = await res.json();
        msg = j?.message || (typeof j?.detail === "string" ? j.detail : "");
      } catch {
        /* non-JSON error body */
      }
      if (!msg) msg = res.ok ? "Launch request failed (empty response)" : `Launch request failed (HTTP ${res.status})`;
      logger.error("engagement.launch.failed", { status: res.status, message: msg });
      throw new Error(msg);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const emit = (rawEvent: string) => {
      const jsonStr = rawEvent
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("");
      if (!jsonStr) return;
      try {
        onEvent(JSON.parse(jsonStr) as LaunchEvent);
      } catch {
        logger.warn("engagement.launch.parse_error", { jsonStr });
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sepIdx: number;
      // SSE events are separated by a blank line.
      while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, sepIdx);
        buffer = buffer.slice(sepIdx + 2);
        emit(rawEvent);
      }
    }
    // Flush any trailing event not terminated by a blank line.
    if (buffer.trim()) emit(buffer);
  }

  async function latestInterviewById(candidateId: string): Promise<LatestInterviewResult> {
    try {
      const res = await authFetch(`${API_BASE}/api/v1/engagement/latest-interview/by-id/${candidateId}`);
      if (!res.ok) return { success: false };
      return (await res.json()) as LatestInterviewResult;
    } catch (e) {
      logger.warn("engagement.latest_interview.error", {
        candidateId,
        message: (e as Error)?.message,
      });
      return { success: false };
    }
  }

  return { generatePayload, sendBulkInterview, launch, latestInterviewById };
}
