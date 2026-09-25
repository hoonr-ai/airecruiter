"""Repair the JobDiva profiles Launch PAIR created blank.

Until the 2026-09-25 fix, every profile PAIR created (people JobDiva did not
have: LinkedIn-Unipile / LinkedIn-Exa / LinkedIn-DeepSearch ...) got a 0-byte
résumé, JobDiva's ``Auto_…`` email and no phone or address -- PAIR never sent
the résumé file and wrote the phone into a field JobDiva ignores (see
services/profile_resume.py and docs/jobdiva-provisioning-invariants.md).

``backfill_blank_profile`` repairs ONE such profile from the sourced_candidates
row PAIR still has for the person:

1. Read the profile and every résumé back from JobDiva. A profile with any
   résumé of real length is left alone as far as the résumé goes -- a
   recruiter may have uploaded the real one since.
2. Build the résumé PAIR can render for the person (build_profile_resume) and
   upload it as a .docx under that candidate (uploadResume with candidateid).
3. Re-read the profile (JobDiva may parse fields out of the new résumé) and
   fill only what is still blank or placeholder: name, email, alternate
   email, phones[], city / state / zip, the country when no address was ever
   entered, and the social links (LinkedIn profile, GitHub, website ...).
   Nothing anyone typed into JobDiva is overwritten.

It never touches the profile of a person sourced FROM JobDiva (their own,
pre-existing profile) or one PAIR recorded as pre-existing. Dry run (the
default) returns the plan and writes nothing.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from services.jobdiva import (
    created_profile_fill,
    jobdiva_profile_id,
    jobdiva_service,
    social_links_fill,
)
from services.profile_resume import (
    alternate_email_of,
    build_profile_resume,
    is_placeholder_name,
    jobdiva_address_fields,
    jobdiva_social_links,
    resume_to_docx,
    split_person_name,
)
from utils.email_utils import is_placeholder_email

logger = logging.getLogger(__name__)

# A résumé with less text than this is the blank one PAIR used to upload
# (JobDiva stores ~3 characters for it).
BLANK_RESUME_CHARS = 50

# The bounded SELECT a caller (script / admin endpoint) runs to find candidates.
# One row per JobDiva profile: the most recently updated local row. JobDiva-
# sourced rows (their own, pre-existing profile) and rows PAIR recorded as a
# pre-existing profile are excluded; legacy rows without the origin stamp are
# included -- the JobDiva read-back decides whether they are blank. Profiles
# already checked (BACKFILL_STAMP_SQL) are skipped unless include_checked, so
# repeated batches walk the whole list.
BACKFILL_CANDIDATES_SQL = """
    SELECT DISTINCT ON (data->>'jobdiva_candidate_id')
        candidate_id, jobdiva_id, name, email, phone, headline, location,
        profile_url, resume_text, source, data
    FROM sourced_candidates
    WHERE data->>'jobdiva_candidate_id' ~ '^[0-9]+$'
      AND COALESCE(source, '') NOT ILIKE 'JobDiva%%'
      AND COALESCE(data->>'jobdiva_profile_origin', 'pair') = 'pair'
      AND (%(include_checked)s OR data->>'jobdiva_backfill_checked_at' IS NULL)
      AND (%(jobdiva_ids)s::text[] IS NULL OR data->>'jobdiva_candidate_id' = ANY(%(jobdiva_ids)s::text[]))
      AND (%(job_ids)s::text[] IS NULL OR jobdiva_id = ANY(%(job_ids)s::text[]))
    ORDER BY data->>'jobdiva_candidate_id', updated_at DESC NULLS LAST
    LIMIT %(limit)s
"""

# Stamped on every local row of a profile the backfill has settled (repaired,
# already complete, or deliberately skipped) -- not on a failure, which the next
# batch retries.
BACKFILL_STAMP_SQL = """
    UPDATE sourced_candidates
    SET data = COALESCE(data, '{}'::jsonb) || %(delta)s::jsonb
    WHERE data->>'jobdiva_candidate_id' = %(jobdiva_id)s
"""


def settles(report: Dict[str, Any]) -> bool:
    """Whether a report closes the profile out (stamp it) or leaves it for a retry."""
    status = str(report.get("status") or "")
    return bool(report.get("apply")) and not status.startswith("failed") and status != "planned"


def _data_of(row: Dict[str, Any]) -> Dict[str, Any]:
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = {}
    return data if isinstance(data, dict) else {}


async def backfill_blank_profile(
    row: Dict[str, Any],
    *,
    apply: bool = False,
    service: Any = None,
) -> Dict[str, Any]:
    """Plan (and with ``apply``, perform) the repair of one PAIR-created profile.

    ``row`` is a sourced_candidates row (``BACKFILL_CANDIDATES_SQL`` columns).
    Returns a JSON-able report: ``status`` is one of ``planned``, ``repaired``,
    ``partially_repaired``, ``already_complete`` or ``skipped_*`` / ``failed_*``,
    plus what was (to be) uploaded and filled. Never raises for a JobDiva error.
    """
    service = service or jobdiva_service
    data = _data_of(row)
    jd_id = str(data.get("jobdiva_candidate_id") or "").strip()
    report: Dict[str, Any] = {
        "candidate_id": row.get("candidate_id"),
        "jobdiva_candidate_id": jd_id,
        "source": row.get("source"),
        "apply": bool(apply),
    }
    if not jd_id.isdigit():
        return {**report, "status": "skipped_no_jobdiva_id"}
    if jobdiva_profile_id(row.get("source"), row.get("candidate_id"), jd_id):
        return {**report, "status": "skipped_jobdiva_sourced"}
    if data.get("jobdiva_profile_origin") == "jobdiva":
        return {**report, "status": "skipped_preexisting_profile"}

    profiles = await service.fetch_candidate_profiles_batch([jd_id])
    current = (profiles or {}).get(jd_id) or {}
    if not current:
        return {**report, "status": "failed_profile_not_readable"}
    resumes = await service.get_candidate_resume_texts(jd_id)
    if resumes is None:
        return {**report, "status": "failed_resumes_not_readable"}
    try:
        resume_count = int(str(current.get("RESUMECOUNT") or "0").strip() or 0)
    except ValueError:
        resume_count = 0
    if resume_count > len(resumes):
        # JobDiva says it holds résumés we did not read: never judge "blank"
        # on a partial view.
        return {**report, "status": "failed_resumes_not_readable", "resume_count": resume_count}
    longest = max((len((r.get("text") or "").strip()) for r in resumes), default=0)
    resume_blank = longest < BLANK_RESUME_CHARS

    email = str(row.get("email") or "").strip()
    email = email if email and not is_placeholder_email(email) else ""
    phone = str(row.get("phone") or "").strip()
    resume = build_profile_resume(row, data, email=email, phone=phone)
    first_name, last_name = split_person_name(resume.name)
    if is_placeholder_name(resume.name) or not first_name:
        return {**report, "status": "skipped_no_usable_name"}
    address = jobdiva_address_fields(resume.location)
    alternate = alternate_email_of(row, data, email)
    social = jobdiva_social_links(row, data)

    def _plan_fill(profile: Dict[str, Any]):
        return (
            created_profile_fill(
                profile, first_name=first_name, last_name=last_name, email=email, phone=phone,
                address=address, fresh=False, alternate_email=alternate,
            ),
            social_links_fill(profile, social),
        )

    fill, links = _plan_fill(current)
    report.update({
        "existing_resumes": len(resumes),
        "longest_existing_resume_chars": longest,
        "upload_resume": resume_blank and not resume.is_blank,
        "resume_sections": list(resume.sections),
        "resume_chars": len(resume.text),
        "fill_fields": sorted(fill),
        "social_links": [link["name"] for link in links],
    })
    if resume_blank and resume.is_blank:
        report["note"] = "nothing to build a résumé from"
    if not apply:
        return {**report, "status": "planned"}
    if not report["upload_resume"] and not fill and not links:
        return {**report, "status": "already_complete"}

    upload_ok = True
    if report["upload_resume"]:
        document = resume_to_docx(resume)
        stem = "_".join(p for p in (first_name, last_name) if p) or "Candidate"
        upload = await service.upload_resume(
            jd_id, resume.text, resume_file=document,
            filename=f"{stem}_Resume.docx" if document else f"{stem}_Resume.txt",
            origin_source=str(row.get("source") or ""),
        )
        report["upload"] = upload
        upload_ok = bool(upload.get("ok"))
        if upload_ok:
            # JobDiva may have parsed fields out of the new résumé: fill only
            # what is STILL blank.
            reread = (await service.fetch_candidate_profiles_batch([jd_id])) or {}
            current = reread.get(jd_id) or current
            fill, links = _plan_fill(current)
            report["fill_fields"] = sorted(fill)
            report["social_links"] = [link["name"] for link in links]

    fill_ok = True
    if fill:
        written = await service.write_profile_fill(jd_id, fill, current)
        fill_ok = bool(written.get("ok"))
        if written.get("refused"):
            report["refused_fields"] = sorted(written["refused"])
        if written.get("email_held_elsewhere"):
            # Another JobDiva record holds this email: very likely a duplicate
            # of an existing person -- merge the two in JobDiva.
            report["email_held_by_another_profile"] = True
    if links:
        links_ok = await service.update_candidate_social_links(jd_id, links)
        report["social_links_ok"] = links_ok
        fill_ok = fill_ok and links_ok
    report["fill_ok"] = fill_ok
    if upload_ok and fill_ok:
        status = "repaired"
    elif upload_ok or fill_ok:
        status = "partially_repaired"
    else:
        status = "failed_writes"
    logger.info(
        "jobdiva backfill %s: %s upload=%s fill=%s",
        jd_id, status, report.get("upload", {}).get("status") if report["upload_resume"] else "-",
        report["fill_fields"] or "-",
    )
    return {**report, "status": status}
