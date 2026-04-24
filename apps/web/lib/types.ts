// Shared domain types used across the web app. Mirrors the shapes returned
// by the FastAPI backend (apps/api/models.py + routers/*). Hand-written, not
// auto-generated — keep in sync when backend models change.

export type Source =
  | "JobDiva"
  | "JobDiva-TalentSearch"
  | "LinkedIn"
  | "LinkedIn-Unipile"
  | "LinkedIn-Exa"
  | "Dice"
  | "Vetted"
  | "upload-resume";

export type MatchType = "must" | "can" | "exclude";

export type Priority = "Required" | "Preferred";

export interface TitleCriterion {
  value: string;
  match_type: MatchType;
  years: number;
  recent: boolean;
  similar_terms: string[];
}

export interface SkillCriterion {
  value: string;
  match_type: MatchType;
  years: number;
  recent: boolean;
  similar_terms: string[];
}

export interface LocationCriterion {
  value: string;
  radius: string;
}

export interface ResumeMatchFilter {
  category: string;
  value: string;
  active: boolean;
}

export interface CompanyExperience {
  company?: string;
  title?: string;
  start_date?: string;
  end_date?: string;
  duration?: string;
  description?: string;
}

export interface Education {
  institution?: string;
  degree?: string;
  year?: string | number;
  field?: string;
}

export interface Certification {
  name?: string;
  issuer?: string;
  year?: string | number;
}

export interface CandidateUrls {
  linkedin?: string;
  github?: string;
  portfolio?: string;
  website?: string;
  [key: string]: string | undefined;
}

export interface Candidate {
  candidate_id?: string;
  id?: string;
  name?: string;
  first_name?: string;
  last_name?: string;
  email?: string;
  phone?: string;
  headline?: string;
  title?: string;
  current_title?: string;
  location?: string;
  city?: string;
  state?: string;
  years_experience?: number;
  experience_years?: number;
  skills?: (string | { name: string; years?: number })[];
  resume_text?: string;
  resume_id?: string;
  profile_url?: string;
  image_url?: string;
  source?: Source | string;
  match_score?: number;
  match_percentage?: number;
  missing_skills?: string[];
  explainability?: string[];
  status?: string;
  is_selected?: boolean;
  open_to_work?: boolean;
  company_experience?: CompanyExperience[];
  education?: Education[];
  candidate_education?: Education[];
  certifications?: Certification[];
  candidate_certification?: Certification[];
  urls?: CandidateUrls;
  enhanced_info?: Record<string, unknown>;
  jobdiva_candidate_id?: string;
  jobdiva_resume_id?: string;
  recruiter_candidate_id?: string | null;
  data?: Record<string, unknown>;
  raw?: Record<string, unknown>;
  // Frontend-only fields
  is_qualified?: boolean;
  score_breakdown?: Record<string, unknown>;
}

export interface RubricCriterion {
  name: string;
  weight: number;
  required?: boolean;
  description?: string;
}

export interface Rubric {
  criteria?: RubricCriterion[];
  [key: string]: unknown;
}

export interface ScreenQuestion {
  question: string;
  answer_type?: string;
  required?: boolean;
  [key: string]: unknown;
}

export interface Job {
  job_id?: string;
  id?: string;
  jobdiva_id?: string;
  title?: string;
  description?: string;
  ai_description?: string;
  customer_name?: string;
  recruiter_notes?: string;
  work_authorization?: string;
  city?: string;
  state?: string;
  location?: string;
  priority?: string;
  program_duration?: string;
  max_allowed_submittals?: number;
  pay_rate?: string;
  openings?: number;
  start_date?: string;
  posted_date?: string;
  status?: string;
  current_step?: number;
  selected_employment_types?: string[];
  recruiter_emails?: string[];
  screening_level?: string;
  selected_job_boards?: string[];
  rubric?: Rubric;
  bot_introduction?: string;
  screen_questions?: ScreenQuestion[];
  sourcing_filters?: SearchCriteria;
  resume_match_filters?: ResumeMatchFilter[];
  [key: string]: unknown;
}

export interface SearchCriteria {
  job_id?: string;
  titles?: TitleCriterion[];
  title_criteria?: TitleCriterion[];
  skill_criteria?: SkillCriterion[];
  locations?: LocationCriterion[];
  keywords?: string[];
  companies?: string[];
  resume_match_filters?: ResumeMatchFilter[];
  location_type?: string;
  sources?: Source[] | string[];
  open_to_work?: boolean;
  boolean_string?: string;
  page?: number;
  limit?: number;
}
