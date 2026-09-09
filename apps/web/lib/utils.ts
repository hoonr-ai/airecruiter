import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

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
  const isFrontMatter = categoryLower === "default" || categoryLower === "logistics" || categoryLower === "work-arrangement";

  const isNewOpps = textLower.includes("exploring new job opportunities");
  const isOnsiteHybrid = textLower.includes("follows an onsite") || textLower.includes("hybrid work arrangement") || textLower.includes("onsite work arrangement");
  
  const isRoleSpecific = orderIndex > 9 || !isFrontMatter;

  if (isBooleanMode) {
    if (isNewOpps || isOnsiteHybrid) return "HARD_FILTER";
    if (isFrontMatter) return hasPassCriteria ? "HARD_FILTER" : "INFO_ONLY";
    if (isRoleSpecific) return isHardFilterInDb ? "HARD_FILTER" : "INFO_ONLY";
    return "INFO_ONLY";
  } else {
    if (isNewOpps || isOnsiteHybrid) return "HARD_FILTER";
    if (isFrontMatter) return hasPassCriteria ? "HARD_FILTER" : "INFO_ONLY";
    return "SCORED";
  }
}
