// Shared job-wizard types.
//
// Promoted out of app/jobs/new/page.tsx so both the existing 5-step jobs
// wizard and the new 3-step campaign wizard can share the same step
// components + state container without either importing the other's page.

export type Step = 1 | 2 | 3 | 4 | 5;
export type ScreeningLevel = "L0.5" | "L1" | "L1.5" | "L2";
export type RegenerateDifficulty = "easy" | "medium" | "hard";
export type EmploymentType = "W2" | "1099" | "C2C" | "Full-Time";
export type WizardMode = "edit" | "source" | "view";

export type ScreenQuestion = {
  id: number;
  question_text: string;
  pass_criteria: string;
  is_default: boolean;
  category: string;
  order_index: number;
  // 4.3: onsite/hybrid arrangement must knock candidates out automatically
  // when they say no. Persisted per-question so non-default recruiter-authored
  // questions can also be marked as hard filters.
  is_hard_filter?: boolean;
};
