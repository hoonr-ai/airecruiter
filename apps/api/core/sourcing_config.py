"""Sourcing quality toggles.

Single source of truth for pipeline leniency flags. Edit the constants
below to change behavior; changes take effect on next process restart.
Imported directly by services — no env-var indirection, no .env
duplication, no hidden defaults.

Each toggle corresponds to a hypothesis from the 2026-05-14 sourcing
audit. Flip individual flags to widen the candidate funnel without
giving up scoring or downstream filters.
"""

import os as _os


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
# 32344914 took 1.6s @ rc=100 vs 3.8s @ rc=400. The UI fetches candidates in
# 150-result tranches: 150 on the first search, then the next 150 only when the
# recruiter clicks "Search more". JobAgent has no offset parameter, so the
# second tranche requests 300 and slices off the first 150 locally.
JOBAGENT_RESUME_COUNT = 150
JOBAGENT_MAX_RESUME_COUNT = 300

# Quick-first tranche for the initial Step-5 search (offset 0 only).
# Because latency scales with resumeCount, a small extra JobAgentSearch
# call returns in seconds and paints the top-N rows (the agent response
# carries their resume text inline) while the full-batch call is still in
# flight. The full call re-returns those N ranks (JobAgent has no offset);
# the shared seen_ids dedup keeps them from re-emitting or re-enriching.
# 0 disables the quick phase. Skipped for headless runs (bypass_screening)
# and "Search more" tranches (offset>0), where first paint doesn't matter.
JOBAGENT_QUICK_FIRST_COUNT = 20


# ─────────────────────────────────────────────────────────────────────────
# TalentSearch pagination
# ─────────────────────────────────────────────────────────────────────────
# The v2 TalentSearch contract IGNORES pageNumber/pageSize and returns the
# full filtered set in one response (live probe 2026-07-19). One fetch is
# capped at MAX_TOTAL_COUNT; the recruiter's 150-result tranches ("Search
# more") are sliced from it locally via jobdiva_offset/jobdiva_batch_size.
JOBDIVA_TALENTSEARCH_PAGE_SIZE = 150
JOBDIVA_TALENTSEARCH_TOTAL_COUNT = 150
JOBDIVA_TALENTSEARCH_MAX_TOTAL_COUNT = 300

# Max terms sent in the v2 TalentSearch `skills` array. The server ANDs
# every element, so a long must-list zeroes out; the fetcher also retries
# with the first two terms when the full AND returns nothing. Remaining
# skill filtering happens client-side in the scorer.
JOBDIVA_TALENT_MAX_SKILL_TERMS = 4

# Second TalentSearch pull with titleSearch=<job title>. Recall lever:
# surfaces candidates whose resume wording doesn't contain the skill terms
# but whose title matches (live probe: titleSearch is honored stand-alone
# and geo-filters correctly; it adds nothing when sent WITH skills, hence
# a separate pull merged by candidateId).
JOBDIVA_TALENT_TITLE_PULL_ENABLED = True

# Max must-have skills ANDed into the generated sourcing boolean (the string
# recruiters paste into JobDiva's JobAgent criteria, and the one sent to
# Unipile/Exa). Every AND multiplies the constraint, so past a handful the
# search returns nothing — the ranked overflow is demoted into a preferred OR
# group instead of dropped, where it still lifts recall without gating it.
# Kept in step with JOBDIVA_TALENT_MAX_SKILL_TERMS so the boolean a recruiter
# reads and the structured TalentSearch pull constrain to the same depth.
JOBDIVA_BOOLEAN_MUST_SKILL_CAP = 4

# Max distinct titleSearch pulls per TalentSearch. Recruiters' hand-written
# agent strings OR several role variants; the structured titleSearch field
# carries exactly one string, so we approximate the OR with up to N separate
# pulls (primary title chip first, then its selected similar titles, then
# further title chips), merged by candidateId. Each pull is one API call.
JOBDIVA_TALENT_TITLE_PULL_MAX_TITLES = 3

# Minimum match_score for a JobDiva-TalentSearch row to stay visible on
# Step 5. TalentSearch queries are machine-generated (top skills AND'd +
# title pulls) and the long tail below this bar is noise recruiters have to
# wade through. Sub-threshold rows are removed via a `dropped` patch after
# scoring. Applies ONLY to JobDiva-TalentSearch: JobAgent results reflect
# recruiter-authored criteria inside JobDiva and are never dropped, and
# unscoreable rows (detail_failed → "Limited data") are always kept.
JOBDIVA_TALENTSEARCH_MIN_SCORE = 60

# Company keywords for the NO-CONTACT list (services/no_contact.py).
# Candidates whose CURRENT or LAST employer loosely matches any of these
# are still shown on Step 5 but greyed out: never LLM-scored, never
# persisted, unselectable, and blocked server-side at /candidates/save and
# the PAIR launch gate. Matching is deliberately loose ("Kaiser" catches
# "Kaiser Permanente", "Citi Bank", one-typo variants) — see
# matches_no_contact_company for the exact rules. Admins get a read-only
# view of this list in the app; adding/removing companies is done by
# editing THIS tuple (code-managed by design for now).
NO_CONTACT_COMPANIES = (
    "Kaiser",
    "Citibank",
    "Intuit",
)

# Minimum match_score for EXTERNAL-source rows (LinkedIn-Exa,
# LinkedIn-Unipile, LinkedIn-DeepSearch, Dice, VettedDB) to be shown /
# launchable. Same rationale as JOBDIVA_TALENTSEARCH_MIN_SCORE — these
# queries are machine-generated and their sub-threshold tail is noise —
# and these sources previously had NO score gate at all, so a
# 0%-match profile stayed selectable and could be launched. Unscoreable
# rows (match_score None) are kept, mirroring the TalentSearch gate.
# Exempt: JobDiva-JobAgent (recruiter-authored criteria inside JobDiva)
# and JobDiva-Applicants (real applicants to this job are never dropped).
EXTERNAL_SOURCE_MIN_SCORE = 60
EXTERNAL_MIN_SCORE_EXEMPT_SOURCES = ("JobDiva-JobAgent", "JobDiva-Applicants")

# Hard-drop rows whose location is CONFIRMED outside the job's location
# (state/province mismatch, or a real measured distance beyond the radius)
# for every source except JobDiva-JobAgent / JobDiva-Applicants. Unknown or
# unparseable candidate locations are NOT confirmed mismatches — those stay
# soft-kept per the existing policy. JobAgent rows keep the soft
# out-of-radius badge instead of dropping (JobDiva's own matcher is
# trusted; the recruiter narrows via the UI chips).
EXTERNAL_LOCATION_CONFIRMED_MISMATCH_DROP = True

# Scoring-time location veto for JobDiva-JobAgent rows. JobAgent results
# follow the criteria/boolean the recruiter authored inside JobDiva — which
# may deliberately reach beyond the job's radius (relocators, nearby metros)
# — so a confirmed out-of-radius / state-mismatch must NOT zero their
# match_score the way it does for machine-queried sources. False (default):
# JobAgent rows keep their rubric score plus the out-of-radius badge and
# distance, and the recruiter filters via the UI chips. True restores the
# old hard-zero. Every other source keeps the location hard gate either way.
JOBAGENT_LOCATION_HARD_VETO = False

# High-level scoring for JobDiva-JobAgent results. The JobAgent criteria
# are authored by recruiters inside JobDiva and its matcher pre-ranks the
# results, so the expensive per-candidate LLM skills-match adds little —
# skip it and score on the cheap signals (title/skills/location/rank floor).
# The UI labels these rows "JobDiva agent search" with a high-level score.
JOBAGENT_HIGH_LEVEL_SCORING = True

# ─────────────────────────────────────────────────────────────────────────
# Sample-first search flow (Step 5 "show me 2 per source before committing")
# ─────────────────────────────────────────────────────────────────────────
# When SearchCriteria.search_mode == "sample", each selected source probes a
# small ranked pool (SAMPLE_MODE_POOL_SIZE), fully enriches + scores it, and
# emits only the first `sample_per_source` rows that clear the normal quality
# gates — so the recruiter can approve source quality before paying for the
# full run. Pool > per-source cap because gate failures (score floor,
# dedup, no-resume) must not leave a source looking empty when it isn't.
SAMPLE_MODE_POOL_SIZE = int(_os.getenv("SAMPLE_MODE_POOL_SIZE", "8").strip() or "8")

# ─────────────────────────────────────────────────────────────────────────
# Per-candidate processing concurrency
# ─────────────────────────────────────────────────────────────────────────
# Width of the per-source processing fan-out. External sources
# (Unipile/Exa/Dice) spend their time in profile fetch + LLM extraction +
# (full mode) contact enrichment; JobDiva talent pools in resume fetch + LLM
# extraction. Both were hard-coded at 5, which serialized 150-candidate pools
# into >10-minute searches. The genuinely rate-limited stages keep their own
# tighter bounds (JOBDIVA_RESUME_FETCH_CONCURRENCY below,
# CANDIDATES_DETAIL_CONCURRENCY, the global LLM semaphore), so this outer
# width mostly controls how many candidates are in flight per source.
EXTERNAL_PROCESS_CONCURRENCY = int(
    _os.getenv("EXTERNAL_PROCESS_CONCURRENCY", "12").strip() or "12"
)
JOBDIVA_ENRICH_CONCURRENCY = int(
    _os.getenv("JOBDIVA_ENRICH_CONCURRENCY", "12").strip() or "12"
)

# Dedicated bound for JobDiva get_candidate_resume calls inside the
# progressive enricher. The old Semaphore(5) implicitly bounded these; now
# that the outer width is 12 we keep the JobDiva-facing fetch at the
# historically safe width so the wider LLM fan-out can't burst JobDiva's
# rate limiter (see CANDIDATES_DETAIL_CONCURRENCY history below).
JOBDIVA_RESUME_FETCH_CONCURRENCY = int(
    _os.getenv("JOBDIVA_RESUME_FETCH_CONCURRENCY", "5").strip() or "5"
)


# Per-search result cap for Unipile LinkedIn Recruiter searches. Protects
# the attached LinkedIn accounts from rate/abuse flags; enforced inside
# unipile_service.search_candidates regardless of the caller's page size.
UNIPILE_SEARCH_LIMIT = 100

# Max number of skills sent to LinkedIn Recruiter as hard MUST_HAVE (ANDed)
# requirements per search. LinkedIn ANDs every must-have together, so a high
# count collapses results — the wizard marks most skills "Must Have", so
# without this cap a 5-skill job matched almost nobody. Extra must-haves and
# all preferred terms are sent as CAN_HAVE (OR) instead, which still boosts
# LinkedIn's ranking. Enforced in unipile_service._search_candidates_once.
UNIPILE_MUST_HAVE_SKILL_CAP = 2


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

import os as _os_geo

def _env_bool_geo(var: str, default: bool) -> bool:
    raw = _os_geo.getenv(var)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

# Send zipCode + withinMiles on JobDiva TalentSearch. SETTLED by the live
# probe 2026-07-19 (scripts/jobdiva_payload_variants_probe.py): with the
# correct v2 top-level body shape the structured radius IS honored — 98.8%
# of returned candidates in-radius vs 8.1% unfiltered. Guardrails: the
# radius is sent with 2x headroom (so the UI's BEYOND-radius soft-keep
# bucket still gets the near-miss band; only far-away noise is cut
# server-side), and the zip is skipped entirely for multi-location-chip
# searches (one anchor can't represent an OR of locations) and for Remote
# jobs.
JOBDIVA_ZIP_RADIUS_ENABLED = _env_bool_geo("JOBDIVA_ZIP_RADIUS_ENABLED", True)

# DEAD FLAG (kept for env compat): the boolean geo dialect rewrite is moot
# since the v2 TalentSearch payload no longer sends boolean strings at all —
# and the probe showed the server never parsed the dialect anyway (arms
# C/C2 identical to control). Only scripts/jobdiva_zip_radius_probe.py
# still exercises the translator path.
JOBDIVA_BOOLEAN_ZIP_DIALECT_ENABLED = _env_bool_geo(
    "JOBDIVA_BOOLEAN_ZIP_DIALECT_ENABLED", False
)


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

# Exa Agent as the sourcing-time contact fallback for LinkedIn-sourced
# candidates. ZoomInfo can't match by LinkedIn URL (name→personId only) and
# Apollo credits run dry, so URL-only LinkedIn/Exa candidates were streaming
# in with no contact info until the recruiter clicked enrich. When True,
# `enrich_contact_for_sourcing` falls through to the Exa Agent contact run
# (still gated by EXA_CONTACT_ENRICH_ENABLED + EXA_API_KEY) for LinkedIn-*
# sources. Capped per job by EXA_SOURCING_CONTACT_CAP to bound spend
# (~$0.115/run).
EXA_SOURCING_CONTACT_FALLBACK = _os.getenv(
    "EXA_SOURCING_CONTACT_FALLBACK", "true"
).strip().lower() in {"1", "true", "yes", "on", "y", "t"}

try:
    EXA_SOURCING_CONTACT_CAP = int(
        _os.getenv("EXA_SOURCING_CONTACT_CAP", "25").strip() or "25"
    )
except ValueError:
    EXA_SOURCING_CONTACT_CAP = 25

# Restrict the sourcing-time Exa fallback to candidates with NO contact at all.
# Exa is the expensive provider (~$0.115/run vs a ZoomInfo/Apollo API call), so
# it should buy us a candidate we could not otherwise reach — not top up a
# candidate we can already contact. Without this, any LinkedIn candidate missing
# only a phone reached Exa, and because Apollo runs out of credits and ZoomInfo's
# name match is accuracy-gated, that double-miss is the COMMON path rather than
# an edge case: effectively every phone-less LinkedIn row billed an Exa run.
# When True, a candidate who already has an email or a phone is left to the
# cheap providers (now including ZoomInfo-by-email, see
# ZOOMINFO_SOURCING_EMAIL_LOOKUP) and to recruiter-initiated on-demand
# enrichment, which is a deliberate click and may still use Exa.
EXA_SOURCING_CONTACT_ONLY_WHEN_NO_CONTACT = _os.getenv(
    "EXA_SOURCING_CONTACT_ONLY_WHEN_NO_CONTACT", "true"
).strip().lower() in {"1", "true", "yes", "on", "y", "t"}

# Try ZoomInfo's match-by-EMAIL lookup at sourcing time when the candidate
# already has an email but no phone. ZoomInfo cannot match a LinkedIn URL, but it
# CAN match an email, so this is the cheap way to fill exactly the gap that used
# to fall through to Exa. The on-demand path has always done this
# (routers/candidates.py); the sourcing path was missing the step.
ZOOMINFO_SOURCING_EMAIL_LOOKUP = _os.getenv(
    "ZOOMINFO_SOURCING_EMAIL_LOOKUP", "true"
).strip().lower() in {"1", "true", "yes", "on", "y", "t"}

# Lifetime ceiling on sourcing-time Exa contact runs per job, per worker.
# EXA_SOURCING_CONTACT_CAP above is a PER-RUN budget — it is reset at the start
# of every search so a job that filled it once isn't starved forever. That reset
# also means it bounds nothing cumulatively: Step 5 re-runs are one click, and at
# ~$0.115/run a job could bill the per-run cap over and over. This cap is NOT
# reset between runs, so total sourcing-time Exa spend on one job stays bounded
# (~100 x $0.115 ≈ $11.50 worst case). Recruiter-initiated on-demand enrichment
# is a separate path and is not affected when this trips.
try:
    EXA_SOURCING_CONTACT_LIFETIME_CAP = int(
        _os.getenv("EXA_SOURCING_CONTACT_LIFETIME_CAP", "100").strip() or "100"
    )
except ValueError:
    EXA_SOURCING_CONTACT_LIFETIME_CAP = 100


# ─────────────────────────────────────────────────────────────────────────
# Launch-time employer resolution (services/employer_resolution.py)
# ─────────────────────────────────────────────────────────────────────────
# Policy (2026-09-02): whenever a candidate reaching a PAIR launch has no
# confident employer data (no extracted company_experience and no explicit
# current_company), fetch their resume and parse it BEFORE outreach — every
# time — and attach JobDiva's CandidatesProfileDetail work history as
# corroboration. JobDiva exposes no structured employer field anywhere else
# (live-probed 2026-09-02), so without this the client-employee and
# no-contact gates run blind on most JobDiva rows.
EMPLOYER_RESOLUTION_ENABLED = _os.getenv(
    "EMPLOYER_RESOLUTION_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on", "y", "t"}

# Concurrent resume parses per launch. The extraction pipeline is
# resume-hash cached, so repeat candidates cost nothing; this bounds the
# fresh-parse burst against the LLM.
try:
    EMPLOYER_RESOLUTION_CONCURRENCY = int(
        _os.getenv("EMPLOYER_RESOLUTION_CONCURRENCY", "6").strip() or "6"
    )
except ValueError:
    EMPLOYER_RESOLUTION_CONCURRENCY = 6

# Per-candidate ceiling on one resume parse (crisp + extract LLM calls).
try:
    EMPLOYER_RESOLUTION_PER_CANDIDATE_TIMEOUT_S = float(
        _os.getenv("EMPLOYER_RESOLUTION_PER_CANDIDATE_TIMEOUT_S", "45").strip() or "45"
    )
except ValueError:
    EMPLOYER_RESOLUTION_PER_CANDIDATE_TIMEOUT_S = 45.0

# Overall wall-clock budget for the resolution pass of ONE launch. Parses
# that don't start before the budget runs out are skipped (the launch
# proceeds on stored signals and reports those candidates as
# employer-unverified); anything parsed is persisted, so the next launch
# starts warmer.
try:
    EMPLOYER_RESOLUTION_BUDGET_S = float(
        _os.getenv("EMPLOYER_RESOLUTION_BUDGET_S", "180").strip() or "180"
    )
except ValueError:
    EMPLOYER_RESOLUTION_BUDGET_S = 180.0

# Hard cap on candidates resolved per launch (defence against a pathological
# batch; normal launches are ≤ LAUNCH_BATCH_SIZE anyway).
try:
    EMPLOYER_RESOLUTION_MAX_CANDIDATES = int(
        _os.getenv("EMPLOYER_RESOLUTION_MAX_CANDIDATES", "300").strip() or "300"
    )
except ValueError:
    EMPLOYER_RESOLUTION_MAX_CANDIDATES = 300

# Ask every PAIR interview "which company do you currently work for?" (one
# extra pre-screen question appended at payload build; see
# services/stated_employer.py). The answer comes back on the PairBot webhook,
# is persisted as data.stated_current_employer, and re-runs the no-contact +
# hiring-client checks — the post-launch backstop for candidates whose
# employer the launch-time resolution pass could not verify.
EMPLOYER_QUESTION_ENABLED = _os.getenv(
    "EMPLOYER_QUESTION_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on", "y", "t"}

# A resume older than this many months makes its parsed "Present" weak
# evidence: launch candidates whose employer signals rest on such a resume
# classify as employer_verification "verified_stale" (surfaced in the launch
# report next to unverified/profile_only — advisory, never blocking).
# JobDiva's resume DATEUPDATED is fetched per launch (one batched
# CandidatesResumesDetail call, services/employer_resolution.py
# stamp_resume_freshness). 0 disables the freshness check entirely.
try:
    EMPLOYER_STALE_RESUME_MONTHS = int(
        _os.getenv("EMPLOYER_STALE_RESUME_MONTHS", "12").strip() or "12"
    )
except ValueError:
    EMPLOYER_STALE_RESUME_MONTHS = 12
