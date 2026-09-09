"use client";

/**
 * QuestionFilterBadge
 *
 * Renders the correct badge (Hard Filter / Info-Only / Scored) for a
 * screening question based on its computed filter type.
 *
 * Used by:
 *  - apps/web/app/jobs/new/page.tsx (job wizard Step 4)
 *  - apps/web/components/campaigns/ScreeningQuestionsEditor.tsx
 *
 * The `filterType` value should come from `getQuestionFilterType()` in
 * @/lib/utils so that the display stays in sync with backend logic.
 */

type FilterType = "HARD_FILTER" | "INFO_ONLY" | "SCORED";

interface QuestionFilterBadgeProps {
  filterType: FilterType;
}

export function QuestionFilterBadge({ filterType }: QuestionFilterBadgeProps) {
  if (filterType === "HARD_FILTER") {
    return (
      <span className="bg-red-50 text-red-700 text-[10px] font-bold px-2 py-0.5 rounded-full border border-red-200 whitespace-nowrap mb-1">
        Hard Filter
      </span>
    );
  }
  if (filterType === "INFO_ONLY") {
    return (
      <span className="bg-blue-50 text-blue-700 text-[10px] font-bold px-2 py-0.5 rounded-full border border-blue-200 whitespace-nowrap mb-1">
        Info-Only
      </span>
    );
  }
  if (filterType === "SCORED") {
    return (
      <span className="bg-orange-50 text-orange-700 text-[10px] font-bold px-2 py-0.5 rounded-full border border-orange-200 whitespace-nowrap mb-1">
        Scored
      </span>
    );
  }
  return null;
}
