import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// ---------------------------------------------------------------------------
// Always-on hard-filter detection phrases (mirrors backend constants in
// apps/api/routers/engagement.py – keep in sync if question text changes)
// ---------------------------------------------------------------------------
const PHRASE_NEW_OPPS = "exploring new job opportunities";
const PHRASES_ONSITE_HYBRID = [
  "follows an onsite",
  "hybrid work arrangement",
  "onsite work arrangement",
];

export function getQuestionFilterType(
  questionText: string,
  passCriteria: string,
  category: string,
  orderIndex: number,
  isBooleanMode: boolean,
  isHardFilterInDb: boolean
): "HARD_FILTER" | "INFO_ONLY" | "SCORED" {
  const textLower = (questionText || "").toLowerCase();
  const hasPassCriteria = Boolean(passCriteria && passCriteria.trim());
  const categoryLower = (category || "default").toLowerCase();
  const isFrontMatter =
    categoryLower === "default" ||
    categoryLower === "logistics" ||
    categoryLower === "work-arrangement";

  const isNewOpps = textLower.includes(PHRASE_NEW_OPPS);
  const isOnsiteHybrid = PHRASES_ONSITE_HYBRID.some((p) => textLower.includes(p));

  // Mirrors _sanitize_pre_screen_questions_for_pair in engagement.py
  if (isNewOpps || isOnsiteHybrid) return "HARD_FILTER";
  if (isFrontMatter) return hasPassCriteria ? "HARD_FILTER" : "INFO_ONLY";
  if (isBooleanMode) return isHardFilterInDb ? "HARD_FILTER" : "INFO_ONLY";
  return "SCORED";
}
