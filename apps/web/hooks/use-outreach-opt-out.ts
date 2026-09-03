"use client";

import { useCallback, useMemo } from "react";

import { API_BASE, authFetch } from "@/lib/api";
import { logger } from "@/lib/logger";

// Do-not-contact calls against pair's own backend, which forwards to pair-bot
// and mirrors the suppression into pair's local DNC list. See
// apps/api/routers/outreach_optout.py, whose docstring carries the rationale.
// pair-bot's own contract lives in OPT_OUT_API.md, owned by the pair-bot team.
//
// These deliberately do NOT go through lib/api's `req`, which flattens a
// failure into `Error("422 /path: {\"detail\":\"…\"}")`. The message the
// backend returns is the thing the recruiter has to read — both on success
// (it carries pair-bot's "across N interviews" count) and on failure (a 502
// says the queued calls were NOT cancelled and the stop must be retried).

export type OptOutInput = {
  candidateId?: string;
  email?: string;
  phone?: string;
  interviewId?: number;
  reason?: string;
  // Omitted = all three channels, which is what "stop contacting me" means.
  channels?: Array<"email" | "sms" | "call">;
  // Omitted = pair's own tenant. "global" opts them out of every product.
  scope?: "curate" | "global";
};

export type OptOutResult = {
  success: boolean;
  // pair-bot's wording, passed through untouched. Render it verbatim.
  message: string;
  data?: {
    suppressed?: Array<{ contact_type: string; contact_value: string }>;
    channels?: string[];
    scope?: string;
    enforced_globally?: string[];
    cancelled?: number;
    interview_ids?: number[];
  };
  local?: {
    dnc_phone_added?: boolean;
    candidates_stopped?: number;
    // True when the contact is suppressed in pair once the call is done,
    // whether or not this call is what did it (an idempotent re-click changes
    // no rows). The backend keys its recruiter-facing wording on this.
    locally_suppressed?: boolean;
    dnc_phone_removed?: boolean;
    dnc_phone_retained_other_source?: boolean;
    candidates_released?: number;
    error?: string | null;
  };
};

export type OptOutStatusResult = {
  success: boolean;
  email?: string | null;
  phone?: string | null;
  pairbot: {
    message?: string;
    error?: string;
    data?: {
      scope?: string;
      suppressed_channels?: string[];
      enforced_across_tenants?: boolean;
      records?: Array<Record<string, unknown>>;
    };
  };
  local: { dnc_listed?: boolean; stopped_rows?: number; error?: string | null };
};

/** Turn a non-2xx response into an Error carrying only the backend's `detail`. */
async function throwBackendDetail(res: Response, fallback: string): Promise<never> {
  let detail = "";
  try {
    const body = await res.json();
    detail =
      typeof body?.detail === "string"
        ? body.detail
        : typeof body?.message === "string"
          ? body.message
          : "";
  } catch {
    // Non-JSON body (a proxy error page, say) — the status is all we have.
  }
  throw new Error(detail || `${fallback} (HTTP ${res.status})`);
}

export function useOutreachOptOut() {
  // Stable identities: callers list these in useEffect/useCallback deps, and a
  // fresh closure per render would re-fire the status read on every keystroke.
  const stopOutreach = useCallback(async (input: OptOutInput): Promise<OptOutResult> => {
    const res = await authFetch(`${API_BASE}/api/v1/outreach/opt-out`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        candidate_id: input.candidateId,
        email: input.email,
        phone: input.phone,
        interview_id: input.interviewId,
        reason: input.reason,
        channels: input.channels,
        scope: input.scope,
      }),
    });
    if (!res.ok) {
      logger.error("stop_outreach_failed", { status: res.status });
      await throwBackendDetail(res, "Could not stop outreach");
    }
    return res.json();
  }, []);

  const resumeOutreach = useCallback(async (input: {
    candidateId?: string;
    email?: string;
    phone?: string;
    reason?: string;
    scope?: "curate" | "global";
  }): Promise<OptOutResult> => {
    const res = await authFetch(`${API_BASE}/api/v1/outreach/opt-in`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        candidate_id: input.candidateId,
        email: input.email,
        phone: input.phone,
        reason: input.reason,
        scope: input.scope,
      }),
    });
    if (!res.ok) {
      logger.error("resume_outreach_failed", { status: res.status });
      await throwBackendDetail(res, "Could not resume outreach");
    }
    return res.json();
  }, []);

  const getOptOutStatus = useCallback(async (input: {
    candidateId?: string;
    email?: string;
    phone?: string;
    interviewId?: number;
  }): Promise<OptOutStatusResult> => {
    const params = new URLSearchParams();
    if (input.candidateId) params.set("candidate_id", input.candidateId);
    if (input.email) params.set("email", input.email);
    if (input.phone) params.set("phone", input.phone);
    if (input.interviewId !== undefined) params.set("interview_id", String(input.interviewId));
    const res = await authFetch(`${API_BASE}/api/v1/outreach/opt-out?${params.toString()}`);
    if (!res.ok) await throwBackendDetail(res, "Could not read opt-out status");
    return res.json();
  }, []);

  return useMemo(
    () => ({ stopOutreach, resumeOutreach, getOptOutStatus }),
    [stopOutreach, resumeOutreach, getOptOutStatus]
  );
}
