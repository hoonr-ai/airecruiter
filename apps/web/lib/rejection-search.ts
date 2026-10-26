// Keyword search behind RejectionReasonSelect's dropdown. Pulled out of that
// component (a .tsx file, which Node's native TS loader can't import — it
// only strips types, it doesn't transpile JSX) so this has unit coverage
// without a renderer, same rationale as notifications-refresh-guard.ts.

// PR #840 review: a query shorter than this can't match as a substring
// ANYWHERE in a reason-word — only as a prefix of one. Without this, a
// 2-letter query like "ab" scored as a full match (every "word" found)
// against "Not comfortable with background check / drug test", purely
// because "ab" sits in the middle of "comfortable" — a false positive with
// nothing to do with what the recruiter was looking for.
const MIN_SUBSTRING_WORD_LENGTH = 3;
// Same idea for the last-resort acronym branch below: 1-2 random letters
// are likely to all appear somewhere in most acronyms by chance.
const MIN_ACRONYM_QUERY_LENGTH = 3;

function wordsOf(text: string): string[] {
  return text.toLowerCase().split(/[\s/,()._–—-]+/).filter(Boolean);
}

/** True if `word` (lowercase) is how a recruiter would plausibly be typing
 * toward some word in `reasonWords`: either a prefix of it (matches any
 * query length — "w2" should still find "W2"), or, once it's long enough to
 * not be a coincidence, a substring anywhere within it (so "over" still
 * finds "overqualified", a single word in the reason text with no space). */
function wordMatches(word: string, reasonWords: string[]): boolean {
  return reasonWords.some((rw) => rw.startsWith(word) || (word.length >= MIN_SUBSTRING_WORD_LENGTH && rw.includes(word)));
}

export function scoreMatch(reason: string, query: string): number {
  const r = reason.toLowerCase();
  const q = query.toLowerCase().trim();
  if (!q) return 0;

  if (r === q) return 100;
  if (r.startsWith(q)) return 90;

  const queryWords = q.split(/\s+/).filter(Boolean);
  const reasonWords = wordsOf(reason);

  if (queryWords.every((w) => wordMatches(w, reasonWords))) return 70;
  const matchedWords = queryWords.filter((w) => wordMatches(w, reasonWords)).length;
  if (matchedWords > 0) return 40 + (matchedWords / queryWords.length) * 20;

  // Acronym fallback: query letters match the first letters of the reason's
  // own words, e.g. "wvc" for "Work / Visa Constraints".
  const acronym = reasonWords.map((w) => w[0] ?? "").join("");
  if (acronym.startsWith(q)) return 30;
  if (q.length >= MIN_ACRONYM_QUERY_LENGTH && q.split("").every((ch) => acronym.includes(ch))) return 15;

  return 0;
}

export function smartFilter(reasons: readonly string[], query: string): string[] {
  return reasons
    .map((r) => ({ r, score: scoreMatch(r, query) }))
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score)
    .map(({ r }) => r);
}
