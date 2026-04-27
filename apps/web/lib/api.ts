// Central API client. Keeps the FastAPI base URL + JSON boilerplate in one
// place. Streaming and multipart calls pass `API_BASE` directly; the `req`
// helper handles the JSON case.

import { trackEvent } from "@/lib/analytics";

export const API_BASE = process.env.NEXT_PUBLIC_API_URL!;

type JsonInit = Omit<RequestInit, "body" | "headers"> & {
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
};

async function req<T>(path: string, init: JsonInit = {}): Promise<T> {
  const { body, headers, ...rest } = init;
  const method = (rest.method || "GET").toUpperCase();
  const started = typeof performance !== "undefined" ? performance.now() : Date.now();
  let trackedError = false;

  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...rest,
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(headers || {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    const ended = typeof performance !== "undefined" ? performance.now() : Date.now();
    const durationMs = Math.round((ended - started) * 100) / 100;

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      trackedError = true;
      trackEvent("api_request_error", {
        path,
        method,
        status: res.status,
        duration_ms: durationMs,
      });
      throw new Error(`${res.status} ${path}${text ? `: ${text}` : ""}`);
    }

    trackEvent("api_request_success", {
      path,
      method,
      status: res.status,
      duration_ms: durationMs,
    });
    return res.json() as Promise<T>;
  } catch (error: any) {
    if (!trackedError) {
      const ended = typeof performance !== "undefined" ? performance.now() : Date.now();
      const durationMs = Math.round((ended - started) * 100) / 100;
      trackEvent("api_request_exception", {
        path,
        method,
        duration_ms: durationMs,
        message: error?.message || "unknown_error",
      });
    }
    throw error;
  }
}

export const api = {
  jobs: {
    fetch: (body: { job_id: string }) =>
      req<any>(`/jobs/fetch`, { method: "POST", body }),
    save: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/save`, { method: "POST", body }),
    saveStep: (jobId: string, step: number, body: unknown) =>
      req<any>(`/jobs/${jobId}/save-step?step=${step}`, { method: "POST", body }),
    monitor: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/monitor`, { method: "POST", body }),
    publish: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/publish`, { method: "POST", body }),
    createExternal: (body: unknown) =>
      req<any>(`/jobs/external/create`, { method: "POST", body }),
    getDraft: (jobId: string) => req<any>(`/jobs/${jobId}/draft`),
    getMonitoredData: (jobId: string) => req<any>(`/jobs/${jobId}/monitored-data`),
    updateBasicInfo: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/basic-info`, { method: "PUT", body }),
  },
  candidates: {
    save: (body: unknown) =>
      req<any>(`/candidates/save`, { method: "POST", body }),
    getResume: (candidateId: string) =>
      req<any>(`/candidates/${candidateId}/resume`),
    analyze: (body: unknown) =>
      req<any>(`/candidates/analyze`, { method: "POST", body }),
    // Streaming endpoint — callers need the raw Response for a ReadableStream.
    searchStreamUrl: `${API_BASE}/candidates/search`,
  },
  manualCandidates: {
    add: (jobRef: string, body: unknown) =>
      req<any>(`/jobs/${jobRef}/manual-candidate`, { method: "POST", body }),
    // Multipart upload — callers pass FormData directly.
    bulkUploadUrl: (jobRef: string) => `${API_BASE}/jobs/${jobRef}/bulk-resumes`,
  },
  chat: {
    send: (body: unknown) => req<any>(`/chat`, { method: "POST", body }),
  },
};
