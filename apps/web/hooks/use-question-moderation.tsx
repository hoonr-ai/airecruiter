"use client";

// AI policy check for recruiter-written screening questions (job wizard Step 4
// and the campaigns questions editor). Debounced per row while typing, flushed
// on blur; verdicts are keyed by normalized question text so reordering rows
// keeps warnings attached and identical questions share one check.
//
// Fails OPEN: a moderation outage yields no warning rather than blocking the
// recruiter — the backend applies the same policy server-side at generation
// time only, so this is a guardrail, not a gate.

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { API_BASE, authFetch } from "@/lib/api";

// Question categories produced by our own generator/front-matter — trusted,
// never moderated. Everything else (job wizard "other", campaigns "custom",
// unknown imports) counts as recruiter-added. Single source of truth for both
// editors — a category added to one list but not the other would make the two
// surfaces silently disagree on what gets checked.
const TRUSTED_QUESTION_CATEGORIES = ["default", "logistics", "work-arrangement", "role-specific", "intro"];

export const isRecruiterAddedQuestion = (category: string | null | undefined): boolean =>
    !TRUSTED_QUESTION_CATEGORIES.includes(String(category || "").toLowerCase());

export interface QuestionPolicyVerdict {
    ok: boolean;
    flags: string[];
    reason: string;
    checked: boolean;
}

export type QuestionModerationState = QuestionPolicyVerdict | "checking";

const MIN_CHECK_LENGTH = 12;
const DEBOUNCE_MS = 1200;

const normalizeQuestionText = (t: string) => t.trim().replace(/\s+/g, " ").toLowerCase();

const FAIL_OPEN: QuestionPolicyVerdict = { ok: true, flags: [], reason: "", checked: false };

export function useQuestionModeration(jobTitle?: string) {
    const [verdicts, setVerdicts] = useState<Record<string, QuestionModerationState>>({});
    // Ref mirror so schedule/run callbacks read current state without stale
    // closures (rows fire checks from event handlers, not effects).
    const verdictsRef = useRef<Record<string, QuestionModerationState>>({});
    const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
    const jobTitleRef = useRef(jobTitle);
    jobTitleRef.current = jobTitle;

    const setEntry = useCallback((key: string, value: QuestionModerationState) => {
        verdictsRef.current = { ...verdictsRef.current, [key]: value };
        setVerdicts(verdictsRef.current);
    }, []);

    const runCheck = useCallback(async (text: string) => {
        const norm = normalizeQuestionText(text);
        if (norm.length < MIN_CHECK_LENGTH) return;
        const existing = verdictsRef.current[norm];
        // Skip only when a check is in flight or a REAL verdict exists. A
        // fail-open entry (checked: false — transient API/LLM outage) must be
        // retried on the next edit/blur, not suppressed for the whole session.
        if (existing === "checking") return;
        if (existing && existing.checked) return;
        setEntry(norm, "checking");
        try {
            const res = await authFetch(`${API_BASE}/api/v1/ai-generation/screening-questions/moderate`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    questions: [{ key: norm, question_text: text.trim() }],
                    job_title: jobTitleRef.current || "",
                }),
            });
            const data = res.ok ? await res.json() : null;
            const v = data?.results?.[0];
            setEntry(
                norm,
                v
                    ? {
                        ok: v.ok !== false,
                        flags: Array.isArray(v.flags) ? v.flags : [],
                        reason: typeof v.reason === "string" ? v.reason : "",
                        checked: v.checked !== false,
                    }
                    : FAIL_OPEN,
            );
        } catch {
            setEntry(norm, FAIL_OPEN);
        }
    }, [setEntry]);

    // Debounced while typing — one timer per row so parallel edits don't
    // cancel each other.
    const scheduleCheck = useCallback((rowKey: string, text: string) => {
        if (timers.current[rowKey]) clearTimeout(timers.current[rowKey]);
        if (normalizeQuestionText(text).length < MIN_CHECK_LENGTH) return;
        timers.current[rowKey] = setTimeout(() => void runCheck(text), DEBOUNCE_MS);
    }, [runCheck]);

    // Immediate — for blur.
    const flushCheck = useCallback((rowKey: string, text: string) => {
        if (timers.current[rowKey]) clearTimeout(timers.current[rowKey]);
        void runCheck(text);
    }, [runCheck]);

    const verdictFor = useCallback(
        (text: string): QuestionModerationState | undefined => verdicts[normalizeQuestionText(text)],
        [verdicts],
    );

    // Pending debounce timers must not fire after the editor unmounts.
    useEffect(() => {
        const pending = timers.current;
        return () => {
            Object.values(pending).forEach(clearTimeout);
        };
    }, []);

    return { verdictFor, scheduleCheck, flushCheck };
}

const SERIOUS_FLAGS = new Set(["nsfw", "rude", "discriminatory", "sensitive_personal_data"]);

const FLAG_LABELS: Record<string, string> = {
    nsfw: "NSFW",
    rude: "rude",
    discriminatory: "discriminatory",
    sensitive_personal_data: "sensitive personal data",
    nonsensical: "doesn't make sense",
    off_topic: "off-topic",
};

// Warning banner rendered under a flagged question row. Renders nothing while
// the check is pending or when the question passes.
export function QuestionPolicyWarning({ verdict }: { verdict: QuestionModerationState | undefined }) {
    if (!verdict || verdict === "checking" || verdict.ok || verdict.flags.length === 0) return null;
    const serious = verdict.flags.some(f => SERIOUS_FLAGS.has(f));
    const flagLabel = verdict.flags.map(f => FLAG_LABELS[f] || f).join(", ");
    return (
        <div
            className={`mt-1.5 flex items-start gap-1.5 rounded-md border px-2.5 py-1.5 text-[11.5px] leading-snug ${
                serious
                    ? "border-rose-200 bg-rose-50 text-rose-700"
                    : "border-amber-200 bg-amber-50 text-amber-800"
            }`}
        >
            <AlertTriangle className="w-3.5 h-3.5 mt-[1px] shrink-0" />
            <span>
                <span className="font-semibold">
                    This question doesn&apos;t follow company policy norms{flagLabel ? ` (${flagLabel})` : ""}.
                </span>{" "}
                {verdict.reason}
            </span>
        </div>
    );
}
