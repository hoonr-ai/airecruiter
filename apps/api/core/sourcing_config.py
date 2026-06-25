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
STRIP_YEARS_FROM_BOOLEAN = True


# ─────────────────────────────────────────────────────────────────────────
# #5 — Skip pre-LLM YOE heuristic for JobDiva sources
# ─────────────────────────────────────────────────────────────────────────
# When True, the regex-based YOE pre-check at Stage 2 is skipped for
# candidates whose `source` starts with "JobDiva". JobDiva's `experience_years`
# field is often a constant default (4) populated from the job title alone,
# which causes real candidates to be dropped before their resume is parsed.
# Real YOE check still runs at Stage 5 against the LLM-extracted value.
SKIP_JOBDIVA_YOE_PRECHECK = True


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
# JobDiva — bypass the Stage-5 pass gate entirely
# ─────────────────────────────────────────────────────────────────────────
# When True, JobDiva-sourced candidates (Applicants + Talent Search) are
# always emitted with `assessment.passes = True`, regardless of what
# `_filter_assessment(enforce_years=True)` returned. `matched`/`missing`
# are still computed and travel on the wire as `screening_summary`, so
# the UI can render a leniency badge — but no candidate is rejected.
#
# Rationale: JobDiva's own ranking (recency for Applicants, JobAgent rank
# for Talent Search) is the source of truth for ordering; our match
# scorer is a rough estimation, not a filter. Other sources (LinkedIn,
# Dice, Exa) still honor REQUIRED_MATCH_RATIO.
JOBDIVA_BYPASS_PASS_GATE = True


# ─────────────────────────────────────────────────────────────────────────
# JobDiva — max candidates fetched per source before enrichment
# ─────────────────────────────────────────────────────────────────────────
# Hard upper bound applied to each JobDiva source (Applicants, Talent /
# JobAgent) after the recency / api_rank sort, before the enrichment +
# per-candidate upsert path runs. Raised from the original 100 HOTFIX bound
# so Step-5 surfaces more of the agentsearch results — candidates should not
# be silently truncated away. The pre-truncation sort is preserved, so if the
# cap is ever hit the strongest-ranked candidates still survive.
#
# Trade-off: higher = more sourced but more DB/enrichment throughput. The
# original cap existed to prevent pool contention during auto-sync when a job
# returned 500+ records; 500 keeps a guardrail while honoring "don't drop
# candidates". Set to None to disable the cap entirely (reintroduces the
# original contention risk).
JOBDIVA_SOURCE_CAP = 500


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
# runs serially; within a page we cap concurrency separately. JobDiva's
# CandidatesDetail accepts up to 100 candidateIds per call, so one page
# = one batch request — amortizes per-batch overhead and minimizes
# request count against the rate limit.
FAST_PATH_DETAIL_BACKGROUND_PAGE_SIZE = 100

# Total candidates we'll background-hydrate. The long tail of the
# locally-sorted result is left thin — recruiters never reach it, and
# burning rate budget there blocks the rows that matter.
FAST_PATH_DETAIL_BACKGROUND_MAX_CANDIDATES = 100

# Sleep (seconds) between hydration pages to spread JobDiva load.
FAST_PATH_DETAIL_BACKGROUND_PAGE_DELAY_S = 1.0


# ─────────────────────────────────────────────────────────────────────────
# JobAgentSearch resumeCount
# ─────────────────────────────────────────────────────────────────────────
# JobAgentSearch latency scales with resumeCount — measured 2026-06-04 on
# real jobs: jobId 32364764 took 13s @ rc=100 vs 110s @ rc=400; jobId
# 32344914 took 1.6s @ rc=100 vs 3.8s @ rc=400. 300 gives the recruiter the
# next tranche of JobAgent matches without going all the way back to the
# slowest observed 400-result calls.
JOBAGENT_RESUME_COUNT = 300


# ─────────────────────────────────────────────────────────────────────────
# TalentSearch pagination
# ─────────────────────────────────────────────────────────────────────────
# Step-5 still sends page_size=100 for the visible UI batch. The backend now
# fans out TalentSearch pages inside one search request so JobDiva contributes
# more candidates without requiring the recruiter to click "next".
JOBDIVA_TALENTSEARCH_PAGE_SIZE = 100
JOBDIVA_TALENTSEARCH_TOTAL_COUNT = 250


# ─────────────────────────────────────────────────────────────────────────
# CandidatesDetail batch — concurrency + retry
# ─────────────────────────────────────────────────────────────────────────
# JobDiva rate-limits (429) bursts of concurrent CandidatesDetail requests.
# Measured 2026-06-04: firing 4 chunks at once via asyncio.gather, 3 of 4
# came back 429. Re-measured 2026-06-05 (job 26-17171): even 2 concurrent
# chunks 429'd ALL the time — 0/150 ids returned with concurrency=2 + the
# [1,3,6]s backoff. JobDiva's limiter only tolerates serialized, spaced
# requests, so default to 1-at-a-time with an inter-request gap and a longer
# backoff. This trades a little latency (mostly in background hydration,
# which is already paced) for actually getting the records back.
CANDIDATES_DETAIL_CONCURRENCY = 1

# Backoff (seconds) before each CandidatesDetail chunk retry. Length also
# bounds the retry count (len == max retries after the first attempt).
CANDIDATES_DETAIL_RETRY_BACKOFF_S = [2.0, 5.0, 10.0, 20.0]

# Pace successive CandidatesDetail requests by holding the concurrency slot
# for this long after each request, so we don't burst past JobDiva's limiter.
CANDIDATES_DETAIL_CHUNK_DELAY_S = 1.5

# Policy: a JobDiva candidate is never hidden from Step 5 just for being
# outside the search radius / in a different state. When True, the JobDiva
# talent-pool LocationGate (`_filter_by_state`) KEEPS out-of-radius candidates
# (flagged `location_out_of_radius`, with `distance_miles`) so they surface
# at a lower location-rubric score and the recruiter can narrow via the
# location chip / MIN MATCH — instead of dropping them before they render.
# Only positive non-US evidence is still hard-dropped. Set False to restore
# the old hard radius filter.
JOBDIVA_LOCATION_SOFT_KEEP = True


# ─────────────────────────────────────────────────────────────────────────
# Exa Agent API (Websets 2.0) — agentic deep-search pass for LinkedIn-Exa
# ─────────────────────────────────────────────────────────────────────────
# When True, every Step-5 search that includes the "Exa" source also fires
# an Exa Agent run (`exa.beta.agent.runs.create`) in parallel. The Agent
# discovers additional LinkedIn profiles missed by Pass A's keyword search
# AND enriches Pass A's hits (passed via `input.data`) with structured
# fields: last_activity, follower_count, last 2 companies, fit_rationale.
# Results land as a second wave of `candidate` / `candidate_detail` events
# tagged with source "LinkedIn-DeepSearch". Disable to skip the cost.
#
# Requires `exa_py>=2.13.0` and an Exa account with the agent beta enabled.
import os as _os
# Accept the common truthy variants. Strict "true" was tripping operators
# who set `EXA_AGENT_ENABLED=1` or `=yes` and then wondered why Pass B
# was silently skipped on every search.
EXA_AGENT_ENABLED = _os.getenv("EXA_AGENT_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on", "y", "t"
}

# Agent effort level → cost cap per 1k searches:
#   low    → $25/1k    — fails our 4-field schema (budget too tight to scrape)
#   medium → $100/1k   — fit_rationale lands, but follower_count + last_activity
#                        are null on ~70% of candidates (budget_reached before
#                        the agent reaches the LinkedIn followers section)
#   high   → $500/1k   — DEFAULT. Reliably populates all four fields.
#   xhigh  → $2000/1k  — overkill for our schema; reserved for one-off deep dives
#   auto   → Exa picks (no fixed cap)
# Also read directly by exa_service.deep_research_candidates() so a deploy
# can be tuned without a process restart.
EXA_AGENT_EFFORT = _os.getenv("EXA_AGENT_EFFORT", "high").strip().lower() or "high"

# Cap on URL count placed into the Agent's `input.data` array. Larger
# batches let the Agent share search context across profiles but increase
# the cost cap proportionally. Read directly by exa_service.
try:
    EXA_AGENT_MAX_INPUT = int(_os.getenv("EXA_AGENT_MAX_INPUT", "25").strip() or "25")
except ValueError:
    EXA_AGENT_MAX_INPUT = 25

# Hard timeout for poll_until_finished, in seconds. `effort=low` usually
# finishes in 30–60s; `effort=xhigh` can hit 5min+. Read directly by exa_service.
try:
    EXA_AGENT_TIMEOUT = int(_os.getenv("EXA_AGENT_TIMEOUT", "180").strip() or "180")
except ValueError:
    EXA_AGENT_TIMEOUT = 180

# Concurrency cap (semaphore) for in-flight Agent runs. Exa enforces a
# concurrency limit of 1/5 of account QPS — default account is ~10 QPS, so
# ≥3 simultaneous runs will start 429ing. Default 1 (serialize per process).
try:
    EXA_AGENT_CONCURRENCY = int(_os.getenv("EXA_AGENT_CONCURRENCY", "1").strip() or "1")
except ValueError:
    EXA_AGENT_CONCURRENCY = 1
