export const TRUSTED_QUESTION_CATEGORIES = ["default", "logistics", "work-arrangement", "role-specific", "intro"];

export const isRecruiterAddedQuestion = (category: string | null | undefined): boolean =>
    !TRUSTED_QUESTION_CATEGORIES.includes(String(category || "").toLowerCase());

/**
 * Returns true when a question's type (scored/hard_filter/info_only) may be
 * changed by the recruiter. Only "other" or "custom" questions (i.e. those
 * explicitly typed by the recruiter) have an open type. System questions
 * (is_default or is_locked) are always fixed so the dropdown is hidden for
 * them.
 * Note: questions loaded from saved data set is_locked via resolveLockedFlag
 * (which also inspects question_text), so is_default not being persisted for
 * pre-existing rows is safe — is_locked alone covers those.
 */
export const canEditQuestionType = (q: { category?: string | null; is_default?: boolean; is_locked?: boolean }): boolean => {
    const cat = String(q.category || "").toLowerCase();
    return (cat === "other" || cat === "custom") && !q.is_default && !q.is_locked;
};
