"""Engagement package: audits."""

from fastapi import HTTPException
from typing import Optional, List, Tuple
from core.auth import UserIdentity

from . import provisioning


# ---------------------------------------------------------------------------
# JobDiva profile-identity audit (admin)
# ---------------------------------------------------------------------------
# A JobDiva-sourced row's own candidate_id IS the person's JobDiva profile id.
# If its stored `jobdiva_candidate_id` says something else, JobDiva has (or
# had) a second profile for that person — the duplicates the pre-
# createJobApplication provisioner minted, or a fresh breach of the invariant
# (see _report_jobdiva_profile_invariant). The audit lists the pairs so they
# can be merged in JobDiva; the repair re-stamps the real id so every reader
# (feedback-note push, passed email, applicant sync) stops routing to the
# duplicate. Neither touches JobDiva itself.

_JOBDIVA_PROFILE_AUDIT_WHERE = """
              lower(sc.source) LIKE 'jobdiva%%'
              AND sc.candidate_id ~ '^[0-9]+$'
              AND sc.data->>'jobdiva_candidate_id' IS NOT NULL
              AND sc.data->>'jobdiva_candidate_id' <> sc.candidate_id"""


def _require_admin(user: UserIdentity) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


async def _audit_job_ids(job_id: Optional[str]) -> List[str]:
    """Every stored spelling of ``job_id`` (as given, numeric PK, reference) for an
    admin audit's job scope; ``[]`` means no job filter."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return []
    numeric_job_id, ref_job_id = await provisioning._resolve_provisioning_job_ids(job_id)
    return [job_id] + [str(x) for x in (numeric_job_id, ref_job_id) if x and str(x) != job_id]


def _audit_job_filter_sql(job_ids: List[str], alias: str) -> str:
    """``" AND <alias>.jobdiva_id = ANY(%s)"`` for a scoped audit, ``""`` for all jobs.
    The matching param is ``[job_ids]`` (one list param) or none -- see callers."""
    return f" AND {alias}.jobdiva_id = ANY(%s)" if job_ids else ""


async def _jobdiva_profile_audit_job_filter(job_id: Optional[str]) -> Tuple[str, list]:
    """SQL fragment + params scoping a profile-audit query (alias ``sc``) to one job, or none."""
    job_ids = await _audit_job_ids(job_id)
    return _audit_job_filter_sql(job_ids, "sc"), ([job_ids] if job_ids else [])


# ---------------------------------------------------------------------------
# Applicant-origin audit (admin): the "everyone became a JobDiva applicant" twins
# ---------------------------------------------------------------------------
# Launch PAIR records a job application in JobDiva for every person it
# provisions, so JobDiva's applicant list then contains the Exa/LinkedIn/Dice
# people too. Until the applicant sync matched them back to their origin row
# (services/auto_assign_service.py `_build_candidate_lookup_index` built its
# index for a job that did not exist), each one was re-imported as a second
# row: `(job, <JobDiva profile id>, 'JobDiva-Applicants')` next to the origin
# row `(job, 'exa_...', 'LinkedIn-Exa')` that carries that same profile id in
# data.jobdiva_candidate_id. The re-launch of a JobDiva-labelled copy of a
# provisioned person produced the same shape under a TalentSearch/JobAgent
# label. Every reader then favoured the JobDiva-labelled twin.
#
# The audit lists the pairs; the repair folds the twin's engage state into the
# origin row (which keeps its origin `source`) and deletes the twin. Neither
# touches JobDiva.

# A twin `t` of an origin row `o`: same job, a JobDiva-labelled row whose own
# candidate_id is the JobDiva profile id the origin stores. Correlated on both
# aliases so it can sit inside a subquery over `t` for a given `o`.
_TWIN_OF_ORIGIN = """t.jobdiva_id = o.jobdiva_id
               AND t.id <> o.id
               AND t.candidate_id <> o.candidate_id
               AND t.candidate_id = o.data->>'jobdiva_candidate_id'
               AND lower(t.source) LIKE 'jobdiva%%'
               AND t.candidate_id ~ '^[0-9]+$'"""

_APPLICANT_TWIN_JOIN = f"""
              FROM sourced_candidates t
              JOIN sourced_candidates o
                ON {_TWIN_OF_ORIGIN}
               AND lower(o.source) NOT LIKE 'jobdiva%%'
             WHERE TRUE"""

# Engage bookkeeping a twin may hold from its own (duplicate) auto-launch;
# folded into the origin row only where the origin has nothing.
_APPLICANT_TWIN_FOLD_KEYS = (
    "engage_status", "engage_interview_id", "engage_score", "engage_updated_at",
    "engage_last_response", "engage_hard_filter_status", "engage_hard_filter_reason",
    "engage_passed_email_sent", "engage_candidate_score", "engage_total_score",
)
