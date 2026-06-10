"use client";

import { API_BASE } from "@/lib/api";
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

export function useEngagementFlow() {
  async function generatePayload(input: GeneratePayloadInput): Promise<GeneratePayloadResult> {
    const res = await fetch(`${API_BASE}/api/v1/engagement/engage/generate-payload`, {
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
    const res = await fetch(`${API_BASE}/api/v1/engagement/engage/send-bulk-interview`, {
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

  async function latestInterviewById(candidateId: string): Promise<LatestInterviewResult> {
    try {
      const res = await fetch(`${API_BASE}/api/v1/engagement/latest-interview/by-id/${candidateId}`);
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

  return { generatePayload, sendBulkInterview, latestInterviewById };
}
