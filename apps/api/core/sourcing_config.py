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
INCLUDE_PROFILE_ONLY = False


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
REQUIRED_MATCH_RATIO = 0.5
