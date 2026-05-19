"""Sourcing quality toggles.

Single source of truth for pipeline leniency flags. Edit the constants
below to change behavior; changes take effect on next process restart.
Imported directly by services — no env-var indirection, no .env
duplication, no hidden defaults.

Each toggle corresponds to a hypothesis from the 2026-05-14 sourcing
audit. Flip individual flags to widen the candidate funnel without
giving up scoring or downstream filters.
"""


# ─────────────────────────────────────────────────────────────────────────
# #2 — Profile-only candidates
# ─────────────────────────────────────────────────────────────────────────
# When True, candidates without a resume body are appended to the result
# set instead of being dropped after the CandidatesResumesDetail rescue
# pass. They still show up with `resume_missing=True` so downstream can
# downweight them, but they're not silently culled.
#
# Default True (2026-05-14): the existing rescue at jobdiva.py:914 only
# fires when the *entire* result set is empty. When some candidates have
# resumes and some don't, the ones without get dropped — including the
# job 26-11245 targets (sohitha716, vsne1519, adarshkt2025) whose JobAgent
# records have `resume_id` but empty `resume_text` until the backfill
# fetch completes. Flipping this to True closes that gap.
INCLUDE_PROFILE_ONLY = True


# ─────────────────────────────────────────────────────────────────────────
# #4 — Strip server-side YOE clauses
# ─────────────────────────────────────────────────────────────────────────
# When True, every `OVER N YRS` clause is removed from the boolean string
# before sending to JobDiva. JobDiva's server-side YOE parse is unreliable
# (off-by-years vs. the resume's actual content), and including these
# clauses can also trip the "no-match → return the full broken pool"
# fallback that produces 4,000+ irrelevant candidates. Defer all YOE
# enforcement to our own scorer.
STRIP_YEARS_FROM_BOOLEAN = False


# ─────────────────────────────────────────────────────────────────────────
# #5 — Skip pre-LLM YOE heuristic for JobDiva sources
# ─────────────────────────────────────────────────────────────────────────
# When True, the regex-based YOE pre-check at Stage 2 is skipped for
# candidates whose `source` starts with "JobDiva". JobDiva's `experience_years`
# field is often a constant default (4) populated from the job title alone,
# which causes real candidates to be dropped before their resume is parsed.
# Real YOE check still runs at Stage 5 against the LLM-extracted value.
SKIP_JOBDIVA_YOE_PRECHECK = False


# ─────────────────────────────────────────────────────────────────────────
# #3 — Stage-5 post-LLM filter ratio
# ─────────────────────────────────────────────────────────────────────────
# Threshold for `_filter_assessment(enforce_years=True)` — candidate passes
# when `matched_required / total_required >= REQUIRED_MATCH_RATIO`.
# Lower = more lenient. Range 0.0–1.0. 0.5 was the historical default but
# kills senior candidates whose resume doesn't enumerate every keyword.
#
# Default 0.3 (2026-05-14): a senior with a matching title and 2 of 6
# required skills should reach the recruiter and let them decide, not
# get filtered by a binary gate. Surfaced borderline candidates still
# carry the `missing` list so the UI can score-degrade them.
REQUIRED_MATCH_RATIO = 0.3


# ─────────────────────────────────────────────────────────────────────────
# Fast-path TalentSearch (skip blocking CandidatesDetail, hydrate async)
# ─────────────────────────────────────────────────────────────────────────
# When True, `_search_talent_pool` and the JobAgent sibling path skip the
# inline `_fetch_candidate_details_batch` call. The thin TalentSearch
# record is returned/streamed as-is, scored locally on the cheap signals
# (title, location, snippet-derived skills, recency), and the UI renders
# in seconds instead of minutes.
#
# CandidatesDetail then runs in the background, page-by-page, paced to
# respect JobDiva's rate limit. Each page emits per-candidate "detail"
# SSE patches that hydrate already-rendered rows in place.
#
# Default True (2026-05-18): the blocking-detail path frequently stalls
# under 429s. The fast path is reversible per-request via this flag.
FAST_PATH_SKIP_DETAIL_IN_TALENT_SEARCH = True

# How many candidates per background CandidatesDetail page. Each page
# runs serially; within a page we cap concurrency separately. Small
# pages give the UI quick incremental hydration; larger pages amortize
# per-batch overhead. 25 ≈ first visible Step-5 page.
FAST_PATH_DETAIL_BACKGROUND_PAGE_SIZE = 25

# Total candidates we'll background-hydrate. The long tail of the
# locally-sorted result is left thin — recruiters never reach it, and
# burning rate budget there blocks the rows that matter.
FAST_PATH_DETAIL_BACKGROUND_MAX_CANDIDATES = 100

# Sleep (seconds) between hydration pages to spread JobDiva load.
FAST_PATH_DETAIL_BACKGROUND_PAGE_DELAY_S = 1.0
