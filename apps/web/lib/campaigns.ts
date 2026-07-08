// Campaign API client + shared types.
//
// A Campaign groups multiple jobs under shared common properties (employment
// type, recruiter emails, screening level, job boards, bot intro) plus a
// reusable JD/rubric/screening-questions template. The backend router is
// mounted under /api (see apps/api/routers/campaigns.py), so all calls go to
// `${API_BASE}/api/campaigns` — this avoids colliding with the /campaigns
// frontend pages.

import { API_BASE } from "@/lib/api";

export type ScreeningLevel = "L1" | "L1.5" | "L2";
export type EmploymentType = "W2" | "1099" | "C2C" | "Full-Time";

export const EMPLOYMENT_TYPES: EmploymentType[] = ["W2", "1099", "C2C", "Full-Time"];

export const SCREENING_LEVELS: { value: ScreeningLevel; label: string; hint: string }[] = [
  { value: "L1", label: "L1", hint: "Basic Screen" },
  { value: "L1.5", label: "L1.5", hint: "Standard Screen" },
  { value: "L2", label: "L2", hint: "Deep Screen" },
];

export const JOB_BOARDS = ["LinkedIn", "Indeed", "Dice", "Monster", "CareerBuilder"];

export interface CampaignChildJob {
  job_id: string;
  jobdiva_id?: string;
  title?: string;
  enhanced_title?: string;
  customer_name?: string;
  status?: string;
  screening_level?: string;
  processing_status?: string;
  pair_launched_at?: string | null;
  created_at?: string | null;
  candidates_launched?: number;
  candidates_sourced?: number;
  city?: string;
  state?: string;
  location_type?: string;
  employment_type?: string;
  pay_rate?: string;
  openings?: string | number;
}

export interface BulkAddJobResult {
  jobdiva_id: string;
  job_id?: string;
  ref?: string;
  title?: string | null;
  fetched?: boolean;
  ok?: boolean;
  error?: string;
}

export interface BulkAddResponse {
  requested: number;
  added: number;
  fetched_from_jobdiva: number;
  results: BulkAddJobResult[];
}

export interface Campaign {
  campaign_id: string;
  name: string;
  customer_name?: string | null;
  recruiter_emails: string[];
  selected_employment_types: string[];
  screening_level: string;
  recruiter_notes?: string | null;
  work_authorization?: string | null;
  selected_job_boards: string[];
  bot_introduction?: string | null;
  template_enhanced_title?: string | null;
  template_ai_description?: string | null;
  template_rubric?: Record<string, unknown> | null;
  template_screen_questions?: unknown[];
  template_sourcing_filters?: Record<string, unknown> | null;
  pair_enabled: boolean;
  status: string;
  user_session?: string | null;
  created_at?: string;
  updated_at?: string;
  job_count?: number;
  jobs?: CampaignChildJob[];
}

export interface CampaignCreatePayload {
  campaign_id?: string;
  name: string;
  customer_name?: string;
  recruiter_emails?: string[];
  selected_employment_types?: string[];
  screening_level?: string;
  recruiter_notes?: string;
  work_authorization?: string;
  selected_job_boards?: string[];
  bot_introduction?: string;
  // Template fields (populated by the campaign wizard in Phase 3)
  template_enhanced_title?: string;
  template_ai_description?: string;
  template_rubric?: Record<string, unknown> | null;
  template_screen_questions?: unknown[];
  template_sourcing_filters?: Record<string, unknown> | null;
}

export interface AddJobPayload {
  jobdiva_id?: string;
  title?: string;
  description?: string;
  customer_name?: string;
  screening_level?: string;
  selected_job_boards?: string[];
}

const CAMPAIGNS_BASE = `${API_BASE}/api/campaigns`;

// A synthetic-email guard matching the backend/wizard rule: JobDiva placeholder
// addresses must never be used as real recruiter contacts.
export function isValidRecruiterEmail(email: string): boolean {
  const e = email.trim().toLowerCase();
  if (!e || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)) return false;
  if (e.endsWith("@jobdiva.com") || e.endsWith("@noemail.pair.ai")) return false;
  return true;
}

async function json<T>(res: Response, label: string): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${label}${text ? `: ${text}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export async function listCampaigns(includeArchived = false): Promise<Campaign[]> {
  const res = await fetch(`${CAMPAIGNS_BASE}?include_archived=${includeArchived}&view=summary`, {
    cache: "no-store",
  });
  const data = await json<{ campaigns: Campaign[] }>(res, "GET /api/campaigns");
  return data.campaigns ?? [];
}

export async function getCampaign(campaignId: string): Promise<Campaign> {
  const res = await fetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}`, {
    cache: "no-store",
  });
  return json<Campaign>(res, `GET /api/campaigns/${campaignId}`);
}

export async function createCampaign(
  payload: CampaignCreatePayload,
): Promise<{ campaign_id: string; campaign: Campaign }> {
  const res = await fetch(CAMPAIGNS_BASE, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return json<{ campaign_id: string; campaign: Campaign }>(res, "POST /api/campaigns");
}

export async function updateCampaign(
  campaignId: string,
  payload: CampaignCreatePayload,
): Promise<{ campaign_id: string; campaign: Campaign }> {
  const res = await fetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return json<{ campaign_id: string; campaign: Campaign }>(res, `PUT /api/campaigns/${campaignId}`);
}

export async function deleteCampaign(campaignId: string): Promise<void> {
  const res = await fetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}`, {
    method: "DELETE",
  });
  await json<unknown>(res, `DELETE /api/campaigns/${campaignId}`);
}

export async function addJobToCampaign(
  campaignId: string,
  payload: AddJobPayload,
): Promise<{ job_id: string; jobdiva_id: string; ref: string }> {
  const res = await fetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return json<{ job_id: string; jobdiva_id: string; ref: string }>(
    res,
    `POST /api/campaigns/${campaignId}/jobs`,
  );
}

export async function bulkAddJobsToCampaign(
  campaignId: string,
  jobdivaIds: string[],
): Promise<BulkAddResponse> {
  const res = await fetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}/jobs/bulk`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jobdiva_ids: jobdivaIds }),
  });
  return json<BulkAddResponse>(res, `POST /api/campaigns/${campaignId}/jobs/bulk`);
}

// =====================================================
// AI template generation — reuses the SAME backend endpoints the jobs wizard
// calls, so the campaign template (JD / rubric / screening questions) is
// produced by identical logic without touching the jobs wizard code.
// =====================================================

export interface RubricRow {
  value?: string;
  minYears?: number;
  recent?: boolean;
  matchType?: string;
  required?: string;
  [k: string]: unknown;
}

export interface Rubric {
  titles?: RubricRow[];
  skills?: RubricRow[];
  soft_skills?: RubricRow[];
  education?: Array<Record<string, unknown>>;
  domain?: Array<Record<string, unknown>>;
  customer_requirements?: Array<Record<string, unknown>>;
  other_requirements?: Array<Record<string, unknown>>;
  total_years?: number;
  [k: string]: unknown;
}

export interface TemplateQuestion {
  id?: number;
  question_text: string;
  pass_criteria?: string;
  category?: string;
  order_index?: number;
  is_default?: boolean;
  is_hard_filter?: boolean;
  [k: string]: unknown;
}

const AI_BASE = `${API_BASE}/api/v1/ai-generation`;

// Campaign holds L1|L1.5|L2; the screening-question generator wants
// light|medium|intensive (mirrors what the jobs wizard sends).
export function screeningLevelToDepth(level: string): "light" | "medium" | "intensive" {
  if (level === "L1") return "light";
  if (level === "L2") return "intensive";
  return "medium";
}

export async function generateJobDescription(input: {
  jobTitle: string;
  jobNotes?: string;
  jobDescription?: string;
  workArrangement?: string;
}): Promise<string> {
  const res = await fetch(`${AI_BASE}/jobs/new/generate-description`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jobTitle: input.jobTitle,
      jobNotes: input.jobNotes ?? "",
      jobDescription: input.jobDescription ?? "",
      workArrangement: input.workArrangement ?? "",
    }),
  });
  const data = await json<{ description: string }>(res, "POST generate-description");
  return data.description ?? "";
}

export async function generateRubric(input: {
  jobTitle: string;
  jobDescription: string;
  jobNotes?: string;
  customerName?: string;
}): Promise<Rubric> {
  // jobId/jobdivaId left blank on purpose: the endpoint skips persisting to the
  // job_* satellite tables when jobId is empty, so this returns a fresh rubric
  // for the campaign template without writing per-job rows.
  const res = await fetch(`${AI_BASE}/jobs/generate-rubric`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jobId: "",
      jobdivaId: "",
      jobTitle: input.jobTitle,
      jobDescription: input.jobDescription,
      jobNotes: input.jobNotes ?? "",
      customerName: input.customerName ?? "",
    }),
  });
  return json<Rubric>(res, "POST generate-rubric");
}

export async function generateScreeningQuestions(input: {
  jobTitle: string;
  rubric: Rubric;
  screeningLevel: string;
  jobDescription?: string;
  customerName?: string;
}): Promise<TemplateQuestion[]> {
  const res = await fetch(`${AI_BASE}/jobs/new/screening-questions/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jobTitle: input.jobTitle,
      rubric: input.rubric,
      screeningLevel: screeningLevelToDepth(input.screeningLevel),
      jobDescription: input.jobDescription ?? "",
      customerName: input.customerName ?? "",
    }),
  });
  const data = await json<{ questions: TemplateQuestion[] }>(res, "POST screening-questions/generate");
  return data.questions ?? [];
}
