export const TRUSTED_QUESTION_CATEGORIES = ["default", "logistics", "work-arrangement", "role-specific", "intro"];

export const isRecruiterAddedQuestion = (category: string | null | undefined): boolean =>
    !TRUSTED_QUESTION_CATEGORIES.includes(String(category || "").toLowerCase());

export const canEditQuestionType = (q: { category?: string | null; is_default?: boolean; is_locked?: boolean }): boolean => {
    return isRecruiterAddedQuestion(q.category) && !q.is_default && !q.is_locked;
};
