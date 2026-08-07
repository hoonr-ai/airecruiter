// Campaign API client + shared types.
//
// A Campaign groups multiple jobs under shared common properties (employment
// type, recruiter emails, screening level, job boards, bot intro) plus a
// reusable JD/rubric/screening-questions template. The backend router is
// mounted under /api (see apps/api/routers/campaigns.py), so all calls go to
// `${API_BASE}/api/campaigns` — this avoids colliding with the /campaigns
// frontend pages.

import { API_BASE, authFetch } from "@/lib/api";

export type ScreeningLevel = "L0.5" | "L1" | "L1.5" | "L2";
export type EmploymentType = "W2" | "1099" | "C2C" | "Full-Time";

export const EMPLOYMENT_TYPES: EmploymentType[] = ["W2", "1099", "C2C", "Full-Time"];

export const SCREENING_LEVELS: { value: ScreeningLevel; label: string; hint: string }[] = [
  { value: "L0.5", label: "L0.5", hint: "Boolean Screen" },
  { value: "L1", label: "L1", hint: "Basic Screen" },
  { value: "L1.5", label: "L1.5", hint: "Standard Screen" },
  { value: "L2", label: "L2", hint: "Deep Screen" },
];

export function formatScreeningLevel(level?: string | null): string {
  if (!level) return "—";
  const found = SCREENING_LEVELS.find((s) => s.value === level);
  return found ? `${found.label} · ${found.hint}` : level;
}

export const JOB_BOARDS = ["LinkedIn", "Indeed", "Dice", "Monster", "CareerBuilder"];

const ROLE_RESPONSIBILITIES_QUESTION = "What is your current or most recent role and key responsibilities?";
const ROLE_RESPONSIBILITIES_MATCH_FRAGMENT = "current or most recent role";

function isBooleanScreeningLevel(level?: string): boolean {
  return (level ?? "").trim().toLowerCase() === "l0.5";
}

export function isRoleResponsibilitiesQuestion(text?: string): boolean {
  return (text ?? "").trim().toLowerCase().includes(ROLE_RESPONSIBILITIES_MATCH_FRAGMENT);
}

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
  outreach_delay_mins?: number | null;
  phase1_6hr_reminder_hours?: number | null;
  phase1_to_phase2_hours?: number | null;
  phase2_to_phase3_hours?: number | null;
  phase1_6hr_call_delay_mins?: number | null;
  phase2_call_delay_mins?: number | null;
  phase3_call_delay_mins?: number | null;
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
  outreach_delay_mins?: number | null;
  phase1_6hr_reminder_hours?: number | null;
  phase1_to_phase2_hours?: number | null;
  phase2_to_phase3_hours?: number | null;
  phase1_6hr_call_delay_mins?: number | null;
  phase2_call_delay_mins?: number | null;
  phase3_call_delay_mins?: number | null;
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
  const res = await authFetch(`${CAMPAIGNS_BASE}?include_archived=${includeArchived}&view=summary`, {
    cache: "no-store",
  });
  const data = await json<{ campaigns: Campaign[] }>(res, "GET /api/campaigns");
  return data.campaigns ?? [];
}

export async function getCampaign(campaignId: string): Promise<Campaign> {
  const res = await authFetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}`, {
    cache: "no-store",
  });
  return json<Campaign>(res, `GET /api/campaigns/${campaignId}`);
}

export async function createCampaign(
  payload: CampaignCreatePayload,
): Promise<{ campaign_id: string; campaign: Campaign }> {
  const res = await authFetch(CAMPAIGNS_BASE, {
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
  const res = await authFetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return json<{ campaign_id: string; campaign: Campaign }>(res, `PUT /api/campaigns/${campaignId}`);
}

export async function deleteCampaign(campaignId: string): Promise<void> {
  const res = await authFetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}`, {
    method: "DELETE",
  });
  await json<unknown>(res, `DELETE /api/campaigns/${campaignId}`);
}

export async function addJobToCampaign(
  campaignId: string,
  payload: AddJobPayload,
): Promise<{ job_id: string; jobdiva_id: string; ref: string }> {
  const res = await authFetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}/jobs`, {
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
  const res = await authFetch(`${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}/jobs/bulk`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jobdiva_ids: jobdivaIds }),
  });
  return json<BulkAddResponse>(res, `POST /api/campaigns/${campaignId}/jobs/bulk`);
}

export async function removeJobFromCampaign(
  campaignId: string,
  jobId: string,
  action: "detach" | "delete" = "detach",
): Promise<void> {
  const res = await authFetch(
    `${CAMPAIGNS_BASE}/${encodeURIComponent(campaignId)}/jobs/${encodeURIComponent(jobId)}?action=${action}`,
    {
      method: "DELETE",
    },
  );
  await json<unknown>(res, `DELETE /api/campaigns/${campaignId}/jobs/${jobId}`);
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

// Campaign holds L0.5|L1|L1.5|L2; the screening-question generator wants
// l0.5|light|medium|intensive (mirrors what the jobs wizard sends).
export function screeningLevelToDepth(level: string): "l0.5" | "light" | "medium" | "intensive" {
  if (level === "L0.5") return "l0.5";
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
  const res = await authFetch(`${AI_BASE}/jobs/new/generate-description`, {
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
  const res = await authFetch(`${AI_BASE}/jobs/generate-rubric`, {
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
  difficultyMode?: string;
  leniencyMode?: boolean;
  workArrangement?: string;
  city?: string;
  totalYears?: number;
}): Promise<TemplateQuestion[]> {
  const isRemote =
    /(?:remote|wfh|virtual|telecommute)/i.test(input.workArrangement ?? "") ||
    /(?:remote|wfh|virtual|telecommute)/i.test(input.city ?? "") ||
    /(?:remote|wfh|virtual|telecommute)/i.test(input.jobTitle ?? "");
  const arrangementLabel = (input.workArrangement ?? "").toLowerCase().includes("hybrid")
    ? "a hybrid"
    : "an onsite";

  const isBoolean = isBooleanScreeningLevel(input.screeningLevel);
  const defaultQs: Array<{ text: string; criteria: string; is_hard_filter?: boolean }> = [
    {
      text: "Are you open to exploring new job opportunities?",
      criteria: "Must be open to new job opportunities",
    },
    { text: "What is your current location?", criteria: "" },
  ];
  if (!isBoolean) {
    defaultQs.splice(1, 0, {
      text: ROLE_RESPONSIBILITIES_QUESTION,
      criteria: "",
    });
  }
  if (!isRemote) {
    defaultQs.push({
      text: `This role follows ${arrangementLabel} work arrangement${input.city ? ` based in ${input.city}` : ""
        }. Are you open to working in this setup?`,
      criteria: `Must be open to ${arrangementLabel} work arrangement`,
    });
  }
  defaultQs.push(
    { text: "What is your earliest availability to start a new role?", criteria: "" },
    { text: "What is your expected compensation for this role?", criteria: "" },
    {
      text: "Which types of working arrangements are you open to and eligible for? Select all that apply: W2 Employee, Subcontractor to Pyramid through your current employer, Independent Contractor",
      criteria: "",
    },
    {
      text: "Are you authorized to work indefinitely for any employer in the United States?",
      criteria: "",
    },
    {
      text: "Will you now or in the future require visa sponsorship to continue working in the United States?",
      criteria: "",
    }
  );

  const defaults: TemplateQuestion[] = defaultQs.map((q, index) => ({
    id: index + 1,
    question_text: q.text,
    pass_criteria: q.criteria,
    is_default: true,
    category: "default",
    order_index: index,
    is_hard_filter: !!q.is_hard_filter,
  }));

  return defaults;
}

export function getDefaultCampaignScreeningQuestions(screeningLevel: string = "L1.5"): TemplateQuestion[] {
  const defaultQs = [
    {
      text: "Are you open to exploring new job opportunities?",
      criteria: "Must be open to new job opportunities",
      category: "default",
      is_hard_filter: false,
    },
    {
      text: "What is your current or most recent role and key responsibilities?",
      criteria: "",
      category: "default",
      is_hard_filter: false,
    },
    { text: "What is your current location?", criteria: "", category: "default", is_hard_filter: false },
    {
      text: "This role follows an onsite/hybrid work arrangement based in the job location. Are you open to working in this setup?",
      criteria: "Must be open to onsite/hybrid work arrangement",
      category: "work-arrangement",
      is_hard_filter: true,
    },
    { text: "What is your earliest availability to start a new role?", criteria: "", category: "logistics", is_hard_filter: false },
    { text: "What is your expected compensation for this role?", criteria: "", category: "logistics", is_hard_filter: false },
    {
      text: "Which types of working arrangements are you open to and eligible for? Select all that apply: W2 Employee, Subcontractor to Pyramid through your current employer, Independent Contractor",
      criteria: "",
      category: "logistics",
      is_hard_filter: false,
    },
    {
      text: "Are you authorized to work indefinitely for any employer in the United States?",
      criteria: "",
      category: "logistics",
      is_hard_filter: false,
    },
    {
      text: "Will you now or in the future require visa sponsorship to continue working in the United States?",
      criteria: "",
      category: "logistics",
      is_hard_filter: false,
    },
  ];

  const normalizedDefaults = isBooleanScreeningLevel(screeningLevel)
    ? defaultQs.filter((q) => !isRoleResponsibilitiesQuestion(q.text))
    : defaultQs;

  return normalizedDefaults.map((q, index) => ({
    id: index + 1,
    question_text: q.text,
    pass_criteria: q.criteria,
    is_default: true,
    category: q.category,
    order_index: index,
    is_hard_filter: !!q.is_hard_filter,
  }));
}
