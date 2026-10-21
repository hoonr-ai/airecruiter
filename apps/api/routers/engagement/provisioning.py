"""Engagement package: provisioning."""

import asyncio
import html
from typing import Optional, List, Any, Dict, Tuple, Sequence
import psycopg2.extras
import json
import re
from datetime import datetime, timezone
from routers._helpers import (
    get_db_connection,
)
from core.queue import enqueue
from core.queue_tasks import CREATE_JOBDIVA_PASS_NOTE, REFRESH_JOB_METRICS, UPDATE_JOBDIVA_QUALIFICATION
from core.pipeline_trace import span as _trace_span
from core.email import (
    notify_candidate_passed,
    _build_word_resume_document,
    candidate_report_link,
    resolve_app_base_url,
)
from services.jobdiva import (
    JOBDIVA_PROFILE_INVARIANT,
    jobdiva_service,
    jobdiva_profile_id,
    get_field as _jd_get_field,
    _get_candidate_email as _jd_candidate_email,
    _get_candidate_phone as _jd_candidate_phone,
)
from services.profile_resume import (
    ProfileResume,
    alternate_email_of,
    build_profile_resume,
    is_placeholder_name,
    jobdiva_address_fields,
    jobdiva_social_links,
    linkedin_public_identifier,
    normalize_linkedin_profile,
    resume_to_docx,
    split_person_name,
)
from utils.email_utils import is_placeholder_email
from services.auto_assign_service import auto_assign_service  # noqa: F401  (core.queue_tasks calls it through this module so tests can patch it)
from core import (
    JOBDIVA_PAIR_RECRUITER_ID,
    JOBDIVA_PAIR_QUALIFICATION_NAME,
    JOBDIVA_PAIR_QUALIFICATION_ID,
    JOBDIVA_PASS_ACTION_NAME,
    JOBDIVA_PASS_QUALIFICATION_VALUE,
)

from .common import HARD_FILTER_PASS_STATUS, JOBDIVA_BLANK_PROFILE_REFUSED, _PROVISION_CONCURRENCY, _parse_json_list, logger
from .pairbot import _job_key_list
from .payloads import _format_normalized_score_100
from . import common


# JobDiva's parser requires an email, so the provisioner mints one for
# phone-only candidates. It is deterministic per candidate and
# `_update_candidate_name` writes it through to JobDiva, which makes it a real
# lookup key on re-provision — see the applicant index in
# `_provision_batch_to_jobdiva`.
_PAIR_SYNTHETIC_EMAIL_DOMAIN = "@no-email.jobdiva.local"


def _persist_jobdiva_link_state(
    candidate_id_internal: str,
    job_keys: List[str],
    *,
    person_delta: Optional[Dict[str, Any]] = None,
    job_delta: Optional[Dict[str, Any]] = None,
) -> None:
    """Stamp JobDiva linkage onto sourced_candidates.data, at the right scope.

    Provenance is kept OUT of the `source` column (the origin channel, written
    once) and lives in `data` -- see services/jobdiva.py, "Provenance":

    * ``person_delta`` -- facts about the person's JobDiva profile, valid on
      every job's row for this candidate_id: ``jobdiva_candidate_id`` and
      ``jobdiva_profile_origin`` ("pair" when PAIR minted the profile, "jobdiva"
      when it pre-existed). Written without a job predicate, like the id always was.
    * ``job_delta`` -- facts about THIS job's application:
      ``jobdiva_application_origin`` ("pair" / "organic"), ``jobdiva_provisioned_at``,
      ``jobdiva_provisioned_from``. Scoped to the job's rows, because an
      application exists per job -- splatting it onto another job's row would
      claim PAIR applied the person there too.

    Both are merge deltas (``||``), never whole blobs. The person-level UPDATE
    has no ``jobdiva_id`` predicate -- correct for the profile facts, which hold
    on every job's row for that person -- so a whole-row blob here would splat
    one job's match_score / engage_status / enhanced_info onto every other job's
    row for the same candidate. That in turn trips ``/engage/launch`` idempotency
    (``engage_status NOT IN ('', 'failed')``) and permanently silences outreach
    on jobs the person was never contacted for. The pool slot is held only for
    the UPDATEs themselves, never across the JobDiva HTTP calls.
    """
    if not person_delta and not job_delta:
        return
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if person_delta:
                cur.execute(
                    "UPDATE sourced_candidates SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb "
                    "WHERE candidate_id = %s",
                    (json.dumps(person_delta, default=str), candidate_id_internal),
                )
            if job_delta and job_keys:
                cur.execute(
                    "UPDATE sourced_candidates SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb "
                    "WHERE candidate_id = %s AND jobdiva_id = ANY(%s)",
                    (json.dumps(job_delta, default=str), candidate_id_internal, [str(k) for k in job_keys]),
                )
        conn.commit()
    finally:
        conn.close()


def _report_jobdiva_profile_invariant(kind: str, **attrs: Any) -> None:
    """Loud, alertable record of a JobDiva profile-identity invariant breach.

    Always logs at ERROR with the grep-able ``JOBDIVA_PROFILE_INVARIANT`` marker
    (services/jobdiva.py); additionally emits a New Relic custom event
    ``JobDivaProfileInvariant`` plus an error-level message when NR is enabled,
    so an alert can be set on either. Never raises -- this sits inside the
    provisioning path.
    """
    payload = {"kind": kind}
    payload.update({k: ("" if v is None else str(v)) for k, v in attrs.items()})
    logger.error("%s %s", JOBDIVA_PROFILE_INVARIANT, json.dumps(payload, default=str))
    try:
        from core.newrelic import is_enabled, record_custom_event, record_message
        if not is_enabled():
            return
        record_custom_event("JobDivaProfileInvariant", payload)
        record_message(f"{JOBDIVA_PROFILE_INVARIANT}: {kind}", attributes=payload, level="error")
    except Exception:
        return


def _resolve_link_candidate_id(
    source: Optional[str],
    cand_data: Optional[Dict[str, Any]],
    candidate_id: Optional[str],
) -> Optional[str]:
    """Return the JobDiva profile id to attach the application to, or None.

    ``candidate_id`` is the row's own ``sourced_candidates.candidate_id``. Only an
    id we trust to be a real JobDiva profile id is returned, in this order:

    1. A JobDiva-sourced row's own id. Every JobDiva pool (TalentSearch / JobAgent /
       Applicants) sets ``candidate_id`` from JobDiva's own response
       (``get_field(c, ["candidateId", "CANDIDATEID", "id", "ID"])`` in
       services/jobdiva.py), so it IS the profile id -- and it beats a stored
       ``jobdiva_candidate_id`` that disagrees. Until the createJobApplication fix,
       ``CreateJobApplicationWithResume`` ignored the id we passed, minted a
       duplicate profile, and the provisioner persisted the duplicate's id here.
       Preferring the row's own id self-heals those rows on their next launch
       instead of attaching yet another job to the duplicate; the disagreement is
       logged so the pair can be merged in JobDiva.
    2. An explicitly stored ``jobdiva_candidate_id``: a profile PAIR created for a
       non-JobDiva row, or one learned from the job's applicant list.

    A non-JobDiva row's numeric-looking internal id is never returned -- linking
    it would attach the application to an unrelated JobDiva profile.
    """
    stored = str((cand_data or {}).get("jobdiva_candidate_id") or "").strip()
    # `stored == own id` is the emitter stamp carried through save; it proves
    # JobDiva provenance even if the `source` label was renamed.
    own_id = jobdiva_profile_id(source, candidate_id, stored)
    if own_id:
        if stored and stored != own_id:
            logger.warning(
                "jobdiva link: row %s (%s) stores jobdiva_candidate_id=%s but its own id "
                "is the JobDiva profile id — linking %s; %s is likely a duplicate profile "
                "to merge in JobDiva",
                candidate_id, source, stored, own_id, stored,
            )
        return own_id
    if stored.isdigit():
        return stored
    return None


def _select_pass_email_resume(
    jobdiva_resume: Optional[str],
    local_resume: Optional[str],
    blocked_markers: Tuple[str, ...],
) -> Tuple[str, bool]:
    """Choose the resume text for the Candidate Passed email.

    Prefers the full JobDiva resume; falls back to the locally stored resume when
    the JobDiva text is empty or a known placeholder. Returns
    ``(resume_text, used_local_fallback)``.
    """
    text = (jobdiva_resume or "").strip()
    if text and any(marker in text for marker in blocked_markers):
        text = ""
    if text:
        return text, False
    local = (local_resume or "").strip()
    if local and not any(marker in local for marker in blocked_markers):
        return local, True
    return "", False


async def _resolve_provisioning_job_ids(job_id_internal: str):
    """Resolve (numeric_job_id, ref_job_id) from either form of the job identifier.
    Returns (None, None) if the job is not found.
    """
    numeric_job_id = None
    ref_job_id = None
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if str(job_id_internal).isdigit():
            numeric_job_id = str(job_id_internal)
            cur.execute("SELECT jobdiva_id FROM monitored_jobs WHERE job_id = %s LIMIT 1", (numeric_job_id,))
            row_j = cur.fetchone()
            if row_j:
                ref_job_id = row_j["jobdiva_id"]
        else:
            ref_job_id = job_id_internal
            cur.execute("SELECT job_id FROM monitored_jobs WHERE jobdiva_id = %s LIMIT 1", (ref_job_id,))
            row_j = cur.fetchone()
            if row_j:
                numeric_job_id = row_j["job_id"]
        cur.close()
    finally:
        conn.close()
    return numeric_job_id, ref_job_id


_provisioning_unipile_service_instance: Optional[Any] = None


def _provisioning_unipile_service():
    global _provisioning_unipile_service_instance
    if _provisioning_unipile_service_instance is None:
        from services.unipile import UnipileService

        _provisioning_unipile_service_instance = UnipileService()
    return _provisioning_unipile_service_instance


async def _fetch_linkedin_profile_for_resume(row: Dict[str, Any], cand_data: Dict[str, Any]) -> Dict[str, Any]:
    """The person's full LinkedIn profile (``normalize_linkedin_profile`` shape),
    read through Unipile by member id (``unipile_<id>`` rows) or by the public
    ``/in/<slug>``. ``{}`` when the row has no LinkedIn identity or the read fails."""
    candidate_id = str(row.get("candidate_id") or "")
    identifier = candidate_id[len("unipile_"):] if candidate_id.startswith("unipile_") else ""
    if not identifier:
        urls = cand_data.get("urls") if isinstance(cand_data.get("urls"), dict) else {}
        identifier = (
            linkedin_public_identifier(row.get("profile_url"))
            or linkedin_public_identifier(urls.get("linkedin"))
            or linkedin_public_identifier(urls.get("linkedin_url"))
        )
    if not identifier:
        return {}
    try:
        raw = await asyncio.wait_for(
            _provisioning_unipile_service().get_candidate_profile(identifier), timeout=30
        )
    except Exception as exc:
        logger.warning("LinkedIn re-read for the JobDiva résumé failed for %s: %s", candidate_id, exc)
        return {}
    return normalize_linkedin_profile(raw) if isinstance(raw, dict) else {}


async def _provisioning_resume(
    row: Dict[str, Any],
    cand_data: Dict[str, Any],
    *,
    email: str,
    phone: str,
    refetch_budget: List[int],
) -> ProfileResume:
    """The résumé a JobDiva profile will be created from.

    Built from the row (services/profile_resume.py). A thin one -- no work
    history, summary or résumé text -- or one with no real name triggers one
    LinkedIn re-read while the batch's ``refetch_budget`` lasts
    (JOBDIVA_PROVISION_LINKEDIN_REFETCH / _CAP).
    """
    resume = build_profile_resume(row, cand_data, email=email, phone=phone)
    needs_reread = resume.is_thin or is_placeholder_name(resume.name)
    if not (needs_reread and common.JOBDIVA_PROVISION_LINKEDIN_REFETCH and refetch_budget[0] > 0):
        return resume
    refetch_budget[0] -= 1
    profile = await _fetch_linkedin_profile_for_resume(row, cand_data)
    if not profile:
        return resume
    enriched = dict(cand_data)
    stored = cand_data.get("linkedin_profile") if isinstance(cand_data.get("linkedin_profile"), dict) else {}
    enriched["linkedin_profile"] = {**stored, **profile}
    refreshed = build_profile_resume(row, enriched, email=email, phone=phone)
    logger.info(
        "LinkedIn re-read for %s: résumé sections %s -> %s",
        row.get("candidate_id"), list(resume.sections), list(refreshed.sections),
    )
    return refreshed


def _resume_file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._") or "Candidate"
    return f"{stem[:80]}_Resume"


async def _provision_batch_to_jobdiva(
    candidate_ids: List[str],
    job_id_internal: str,
    *,
    label: str = "Batch Provisioning",
) -> Dict[str, Any]:
    """
    Efficiently provisions all candidates as JobDiva applicants for a job.

    KEY IMPROVEMENT over the old per-candidate approach:
    - Resolves job IDs ONCE (not N times)
    - Fetches the applicants list from JobDiva ONCE (not N times)
    - Loads all candidate rows in a SINGLE DB query
    - Processes candidates concurrently, bounded by _PROVISION_CONCURRENCY

    Returns a dict with 'success', 'skipped', 'failed' counts.
    """
    results: Dict[str, int] = {
        "success": 0, "skipped": 0, "failed": 0, "duplicate_suspected": 0, "blank_profile_refused": 0,
    }
    if not candidate_ids:
        return results
    if not common.jobdiva_provisioning_enabled():
        logger.warning(
            "[%s] JOBDIVA_PROVISIONING_ENABLED=false: %d candidate(s) not provisioned to JobDiva",
            label, len(candidate_ids),
        )
        results["skipped"] = len(candidate_ids)
        return results
    # LinkedIn re-reads left for this batch (see _provisioning_resume).
    refetch_budget = [common.JOBDIVA_PROVISION_LINKEDIN_REFETCH_CAP]

    try:
        # ── Phase 1: Resolve job IDs (one DB round-trip)
        numeric_job_id, ref_job_id = await _resolve_provisioning_job_ids(job_id_internal)
        if not numeric_job_id and not ref_job_id:
            logger.warning(f"⚠️ [{label}] Cannot resolve job IDs for '{job_id_internal}'. Skipping all.")
            results["failed"] = len(candidate_ids)
            return results

        # ── Phase 2: Load ALL candidate rows in ONE query
        conn = get_db_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT candidate_id, name, email, phone, resume_text, data, jobdiva_id, source
                FROM sourced_candidates
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s OR jobdiva_id = %s OR jobdiva_id = 'unknown')
            """, (list(candidate_ids), job_id_internal, numeric_job_id, ref_job_id))
            candidate_rows: Dict[str, Any] = {str(r["candidate_id"]): r for r in cur.fetchall()}
            cur.close()
        finally:
            conn.close()

        missing = len(candidate_ids) - len(candidate_rows)
        if missing:
            logger.warning(f"⚠️ [{label}] {missing}/{len(candidate_ids)} candidate IDs not found in sourced_candidates for job {ref_job_id}")

        # Every jobdiva_id spelling the row load above accepts -- the job-level
        # provenance stamps below must land on exactly those rows.
        job_row_keys: List[str] = []
        for key in (job_id_internal, numeric_job_id, ref_job_id, "unknown"):
            if key and str(key) not in job_row_keys:
                job_row_keys.append(str(key))

        # ── Phase 3: Fetch existing applicants from JobDiva ONCE
        jd_job_id = numeric_job_id or ref_job_id
        existing_applicants: List[Dict[str, Any]] = []
        if jd_job_id:
            logger.info(f"🔍 [{label}] Fetching existing applicants for job {jd_job_id} (single call)...")
            existing_applicants = await jobdiva_service.get_job_applicants_detail(jd_job_id)
            logger.info(f"✅ [{label}] Found {len(existing_applicants)} existing applicants in JobDiva")

        # Build fast lookups for dedup, keyed on JD candidate id / normalised
        # email / normalised phone. These are MAPS, not sets: on an email- or
        # phone-only match we need the matching applicant's real JobDiva id so
        # we can persist it (see the match branch below), otherwise the row
        # stays without a `jobdiva_candidate_id` forever and /engage/re-provision
        # re-attempts it on every run.
        #
        # Field extraction goes through the shared jobdiva.py helpers rather
        # than a hand-rolled two-key read: JobDiva BI varies its casing and puts
        # numbers in CELLPHONE / PHONE1..4 slots, so `a.get("PHONE")` alone
        # yields an empty set and makes every candidate look new.
        existing_jd_ids: set = set()
        existing_emails: Dict[str, str] = {}
        existing_phones: Dict[str, str] = {}
        def _remember(index: Dict[str, str], key: str, jd_id: str) -> None:
            """First writer wins, but a real id always beats a blank one.

            JobDiva BI can emit a partial row for a person (no CANDIDATEID) ahead
            of the full one; a plain `setdefault` would pin the blank and the
            stamp-back below would learn nothing.
            """
            if key and not index.get(key):
                index[key] = jd_id

        for a in existing_applicants:
            a_jd_id = str(_jd_get_field(a, ["candidateId", "CANDIDATEID", "id", "ID"]) or "")
            if a_jd_id:
                existing_jd_ids.add(a_jd_id)

            a_email = str(_jd_candidate_email(a) or "").lower().strip()
            # `auto_*@jobdiva.com` is JobDiva's own placeholder and is worthless as
            # a key. Our `pair-…@no-email.jobdiva.local` is NOT: we mint it below
            # it is deterministic per candidate, and `_update_candidate_name` writes
            # it through to JobDiva — so it is the only handle we have on the
            # phone-only cohort and must stay indexed.
            if a_email and not a_email.startswith("auto_") and (
                _PAIR_SYNTHETIC_EMAIL_DOMAIN in a_email or not is_placeholder_email(a_email)
            ):
                _remember(existing_emails, a_email, a_jd_id)

            # Index the mobile-preferred number AND the plain PHONE field. Keying
            # only on `_get_candidate_phone` would return the cell when both exist,
            # losing the match for candidates whose stored number is the landline —
            # a narrowing versus the `a.get("PHONE")` this replaced. Deliberately
            # NOT every slot: a shared switchboard in PHONE3 would match an
            # unrelated person and make us skip provisioning someone genuinely
            # absent from JobDiva, which is worse than a duplicate.
            for raw_phone in (_jd_candidate_phone(a), a.get("PHONE"), a.get("phone")):
                a_phone = "".join(ch for ch in str(raw_phone or "") if ch.isdigit())
                if len(a_phone) >= 7:
                    _remember(existing_phones, a_phone, a_jd_id)

        # ── Phase 4: Provision each candidate (concurrent, semaphore-bounded)
        async def _provision_one(cand_id: str) -> str:
            async with _PROVISION_CONCURRENCY:
                row = candidate_rows.get(str(cand_id))
                if not row:
                    logger.warning(f"⚠️ [{label}] Candidate {cand_id} missing from DB — skipping.")
                    return "failed"

                cand_data = row.get("data") or {}
                if isinstance(cand_data, str):
                    try:
                        cand_data = json.loads(cand_data)
                    except Exception:
                        cand_data = {}

                email = (row.get("email") or "").strip()
                # Real contact only on the résumé; `email` may become the synthetic
                # lookup address below.
                resume_email = email if email and not is_placeholder_email(email) else ""
                phone = (row.get("phone") or "").strip()
                existing_jd_id = str(cand_data.get("jobdiva_candidate_id") or "").strip()
                # The profile to attach this job to, if we know it: a JobDiva-sourced
                # row's own id (which beats a stale stored id — see the resolver),
                # else an explicitly stored jobdiva_candidate_id. None for a
                # LinkedIn/Exa/Dice row never matched to JobDiva.
                link_candidate_id = _resolve_link_candidate_id(
                    row.get("source"), cand_data, str(cand_id)
                )
                # Non-empty iff this row is a JobDiva-sourced person (own id IS the
                # profile id). Drives the two tripwires below.
                own_jd_id = jobdiva_profile_id(row.get("source"), cand_id, existing_jd_id)

                phone_norm = "".join(ch for ch in phone if ch.isdigit())
                
                # JobDiva parser absolutely requires an email address.
                # If none is provided, generate a dummy one so the profile creates successfully.
                if not email:
                    email = f"pair-{phone_norm or cand_id}{_PAIR_SYNTHETIC_EMAIL_DOMAIN}"

                email_lower = email.lower()

                # Check against pre-fetched applicant sets (no extra API call).
                # Test both the resolved link id and the stored id: for a JobDiva
                # row whose stored id points at a duplicate profile, whichever of
                # the two JobDiva lists as the applicant counts.
                jcid_match = bool(
                    (link_candidate_id and link_candidate_id in existing_jd_ids)
                    or (existing_jd_id and existing_jd_id in existing_jd_ids)
                )
                email_match = bool(
                    email_lower
                    and not email_lower.startswith("auto_")
                    and email_lower in existing_emails
                )
                phone_match = bool(
                    phone_norm and len(phone_norm) >= 7 and phone_norm in existing_phones
                )

                if jcid_match or email_match or phone_match:
                    logger.info(
                        f"✅ [{label}] Candidate {cand_id} already in JobDiva "
                        f"(jcid={jcid_match} email={email_match} phone={phone_match})"
                    )
                    # Persist the profile id this row should carry: the resolved
                    # link id when we have one (for a JobDiva row that is its own
                    # id, which also re-stamps a stored duplicate's id), else the
                    # matched applicant's real id from the email/phone index —
                    # without this an email/phone match learned nothing and
                    # /engage/re-provision re-attempted the same people forever.
                    matched_jd_id = (
                        link_candidate_id
                        or existing_jd_id
                        or existing_emails.get(email_lower, "")
                        or existing_phones.get(phone_norm, "")
                    )
                    person_delta: Dict[str, Any] = {}
                    job_delta: Dict[str, Any] = {}
                    if matched_jd_id and existing_jd_id != str(matched_jd_id):
                        cand_data["jobdiva_candidate_id"] = matched_jd_id
                        person_delta["jobdiva_candidate_id"] = matched_jd_id
                    # Provenance we can assert here: a JobDiva-sourced person's
                    # profile pre-existed, and a JobDiva-Applicants row's
                    # application is organic by definition. Anything else (an
                    # Exa row already listed as an applicant) stays as stamped --
                    # PAIR may have filed that application on an earlier launch.
                    if own_jd_id and not cand_data.get("jobdiva_profile_origin"):
                        person_delta["jobdiva_profile_origin"] = "jobdiva"
                    if (
                        not cand_data.get("jobdiva_application_origin")
                        and str(row.get("source") or "").strip().lower() == "jobdiva-applicants"
                    ):
                        job_delta["jobdiva_application_origin"] = "organic"
                    if person_delta or job_delta:
                        cand_data.update(person_delta)
                        cand_data.update(job_delta)
                        _persist_jobdiva_link_state(
                            cand_id, job_row_keys,
                            person_delta=person_delta or None, job_delta=job_delta or None,
                        )
                    return "skipped"

                # ── Not found → create a new JobDiva application

                # Fail closed: a JobDiva-sourced row must never reach the call
                # that can mint a profile. Structurally link_candidate_id is
                # always set for such a row; this is the tripwire for a future
                # refactor of the resolver (PR #493 was exactly that regression).
                if own_jd_id and not link_candidate_id:
                    _report_jobdiva_profile_invariant(
                        "jobdiva_row_without_link_id",
                        candidate_id=cand_id, source=row.get("source"), job_id=jd_job_id,
                        stored_jobdiva_candidate_id=existing_jd_id,
                    )
                    return "failed"

                first_name, last_name = split_person_name(row.get("name"))
                # Linking never uploads a résumé (createJobApplication has no file).
                create_kwargs: Dict[str, Any] = {"resume_text": "", "filename": "candidate_resume.txt"}
                if link_candidate_id is None:
                    # Nobody PAIR knows in JobDiva, so this call may create the
                    # profile. Give it everything we hold: the full résumé (the
                    # row's own, or one rendered from its LinkedIn profile) as a
                    # real file, plus the address fields JobDiva's parse may miss.
                    # Sending only a name and "(Profile sourced via PAIR)" -- in
                    # `textfile`, which JobDiva does not store -- is what created
                    # blank profiles (services/profile_resume.py).
                    resume = await _provisioning_resume(
                        row, cand_data, email=resume_email, phone=phone, refetch_budget=refetch_budget,
                    )
                    first_name, last_name = split_person_name(resume.name)
                    refusal = ""
                    if is_placeholder_name(resume.name) or not first_name:
                        refusal = "no_usable_name"
                    elif resume.is_blank:
                        refusal = "no_profile_data"
                    if refusal:
                        logger.error("%s %s", JOBDIVA_BLANK_PROFILE_REFUSED, json.dumps({
                            "reason": refusal, "candidate_id": cand_id, "source": row.get("source"),
                            "job_id": jd_job_id, "name": resume.name,
                        }, default=str))
                        return "blank_profile_refused"
                    document = resume_to_docx(resume)
                    stem = _resume_file_stem(resume.name)
                    create_kwargs = {
                        "resume_text": resume.text,
                        "resume_file": document,
                        "filename": f"{stem}.docx" if document else f"{stem}.txt",
                        "profile_fields": jobdiva_address_fields(resume.location),
                        # Everything else the LinkedIn data can fill on the profile.
                        "alternate_email": alternate_email_of(row, cand_data, resume_email),
                        "social_links": jobdiva_social_links(row, cand_data),
                    }
                    logger.info(
                        f"📄 [{label}] {cand_id} résumé for JobDiva: sections={list(resume.sections)} "
                        f"chars={len(resume.text)} file={'docx' if document else 'txt'}"
                    )

                # With a known profile id, create_job_application_with_resume calls
                # JobDiva's createJobApplication (attach to the existing profile —
                # it cannot create one) and never falls back to minting a profile;
                # allow_profile_creation=False makes the service refuse the create
                # path outright even if that link-first logic ever regresses.
                # Only a row with no link id (LinkedIn/Exa/Dice, and not matched to
                # JobDiva by email/phone either) reaches CreateJobApplicationWithResume,
                # the call that parses the resume into a NEW profile.
                try:
                    outcome = await jobdiva_service.create_job_application_with_resume(
                        candidate_id=link_candidate_id,
                        job_id=jd_job_id,
                        first_name=first_name,
                        last_name=last_name,
                        email=email or "",
                        phone=phone or "",
                        allow_profile_creation=link_candidate_id is None,
                        # Origin channel -> JobDiva Resume Source id, so JobDiva
                        # itself records that PAIR filed this application.
                        origin_source=str(row.get("source") or ""),
                        **create_kwargs,
                    )
                    success, new_jd_id = outcome
                except Exception as exc:
                    logger.error(f"❌ [{label}] Exception for {cand_id}: {exc}", exc_info=True)
                    return "failed"

                # How the application was recorded (JobDivaApplicationOutcome);
                # a bare tuple (older fakes) is read by which endpoint must have run.
                outcome_path = getattr(outcome, "path", None) or ("linked" if link_candidate_id else "created")
                # A looked-up id, or a profile JobDiva already had and matched our
                # résumé to, is not one PAIR minted.
                outcome_found_via_search = bool(
                    getattr(outcome, "found_via_search", False) or getattr(outcome, "matched_existing", False)
                )
                # This job's application was recorded by PAIR, whichever endpoint did it.
                job_delta = {
                    "jobdiva_application_origin": "pair",
                    "jobdiva_provisioned_at": common._utc_now_iso(),
                    "jobdiva_provisioned_from": str(row.get("source") or ""),
                }

                if success and new_jd_id:
                    persisted_id = str(new_jd_id)
                    status = "success"
                    if own_jd_id and persisted_id != own_jd_id:
                        # JobDiva answered with a profile id that is not this
                        # JobDiva-sourced person's own id: a duplicate profile was
                        # minted, or the application went to the wrong profile.
                        # Keep the real id — persisting the stranger's id is what
                        # made the pre-fix duplicates self-perpetuating — and shout.
                        _report_jobdiva_profile_invariant(
                            "unexpected_profile_id",
                            candidate_id=cand_id, source=row.get("source"), job_id=jd_job_id,
                            expected_jobdiva_candidate_id=own_jd_id,
                            returned_jobdiva_candidate_id=persisted_id,
                        )
                        persisted_id = own_jd_id
                        status = "duplicate_suspected"
                    logger.info(f"🎉 [{label}] Candidate {cand_id} → JobDiva ID: {persisted_id}")
                    person_delta = {"jobdiva_candidate_id": persisted_id}
                    # Who minted the profile: a JobDiva-sourced person's profile
                    # pre-existed; the create endpoint answering with an id means
                    # PAIR minted it (a recovered/looked-up id is a pre-existing
                    # profile or unknown, so only an absent stamp is filled).
                    if own_jd_id:
                        person_delta["jobdiva_profile_origin"] = "jobdiva"
                    elif outcome_path == "created" and not outcome_found_via_search:
                        person_delta["jobdiva_profile_origin"] = "pair"
                    elif outcome_found_via_search and not cand_data.get("jobdiva_profile_origin"):
                        person_delta["jobdiva_profile_origin"] = "jobdiva"
                    cand_data.update(person_delta)
                    cand_data.update(job_delta)
                    _persist_jobdiva_link_state(
                        cand_id, job_row_keys, person_delta=person_delta, job_delta=job_delta
                    )
                    # Add to in-memory sets so concurrent siblings don't re-create the same person.
                    # This covers both newly created AND pre-existing profiles that were linked.
                    existing_jd_ids.add(persisted_id)
                    if email_lower and not email_lower.startswith("auto_") and not is_placeholder_email(email_lower):
                        existing_emails[email_lower] = persisted_id
                    if phone_norm and len(phone_norm) >= 7:
                        existing_phones[phone_norm] = persisted_id
                    return status
                elif success:
                    # Only the create path can get here (linking always returns the
                    # linked id): JobDiva made the application but sent no profile id
                    # and the service's lookup could not recover one. Record that
                    # PAIR filed this job's application anyway -- the id-less row is
                    # what the applicant-origin audit and re-provision look for.
                    logger.warning(f"⚠️ [{label}] Application created for {cand_id} but JobDiva returned no candidate id")
                    cand_data.update(job_delta)
                    _persist_jobdiva_link_state(cand_id, job_row_keys, job_delta=job_delta)
                    return "success"
                else:
                    logger.error(f"❌ [{label}] create_job_application_with_resume returned False for {cand_id}")
                    return "failed"

        tasks = [_provision_one(cid) for cid in candidate_ids]
        statuses = await asyncio.gather(*tasks, return_exceptions=True)
        for s in statuses:
            if isinstance(s, Exception):
                results["failed"] += 1
            elif s == "success":
                results["success"] += 1
            elif s == "duplicate_suspected":
                # The application exists, so it counts as provisioned — but the
                # id JobDiva returned was not the person's own; see the ERROR log.
                results["success"] += 1
                results["duplicate_suspected"] += 1
            elif s == "skipped":
                results["skipped"] += 1
            elif s == "blank_profile_refused":
                # Not provisioned (so re-provision retries it once the row has
                # data), and counted apart so a batch of them is visible.
                results["failed"] += 1
                results["blank_profile_refused"] += 1
            else:
                results["failed"] += 1

        logger.info(f"📊 [{label}] Done for job {ref_job_id}: {results}")
        return results

    except Exception as e:
        logger.error(f"❌ [{label}] Outer error: {e}", exc_info=True)
        results["failed"] = len(candidate_ids)
        return results


async def _provision_candidate_to_jobdiva(candidate_id_internal: str, job_id_internal: str):
    """Single-candidate shim kept for backward-compat. Delegates to the batch function."""
    result = await _provision_batch_to_jobdiva([candidate_id_internal], job_id_internal)
    return result.get("success", 0) > 0 or result.get("skipped", 0) > 0


# A provisioning claim older than this is presumed dead (worker killed mid-run)
# and may be taken again.
_PROVISION_CLAIM_STALE_MINUTES = 15
_PROVISION_CHUNK = 25


def _claim_rows_for_provisioning(job_keys: List[str], candidate_ids: List[str]) -> List[str]:
    """Stamp data.jobdiva_provisioning_claimed_at on launched rows that still
    have no JobDiva profile id and no live claim; return the ids stamped.

    Same population as /engage/re-provision's default (launched, missing
    jobdiva_candidate_id), so two overlapping provisioning runs can never both
    create the same person's profile (the duplicate-profile bug of PR #615)."""
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE sourced_candidates sc
            SET data = COALESCE(sc.data, '{}'::jsonb)
                       || jsonb_build_object('jobdiva_provisioning_claimed_at', now()::text),
                updated_at = CURRENT_TIMESTAMP
            WHERE sc.candidate_id = ANY(%(ids)s)
              AND sc.jobdiva_id = ANY(%(keys)s)
              AND COALESCE(sc.data->>'jobdiva_candidate_id', '') = ''
              AND (
                  COALESCE(sc.data->>'engage_interview_id', '') <> ''
                  OR LOWER(COALESCE(sc.data->>'engage_status', '')) = 'sent'
                  OR EXISTS (
                      SELECT 1 FROM engage_interview_audit a
                      WHERE a.candidate_id = sc.candidate_id
                        AND a.jobdiva_id = ANY(%(keys)s)
                        AND COALESCE(a.interview_id, '') <> ''
                  )
              )
              AND (
                  NULLIF(sc.data->>'jobdiva_provisioning_claimed_at', '') IS NULL
                  OR NULLIF(sc.data->>'jobdiva_provisioning_claimed_at', '')::timestamptz
                     < now() - make_interval(mins => %(stale)s)
              )
            RETURNING sc.candidate_id
            """,
            {"ids": candidate_ids, "keys": job_keys, "stale": _PROVISION_CLAIM_STALE_MINUTES},
        )
        claimed = {str(r[0]) for r in (cur.fetchall() or [])}
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return [c for c in candidate_ids if c in claimed]


async def provision_sent_candidates(job_id: str, candidate_ids: Sequence[str]) -> Dict[str, Any]:
    """Create/link the JobDiva applications for launched candidates, awaited.

    The launch runner calls this (stage S7b) instead of the fire-and-forget
    per-batch provisioning `_send_bulk_interview_core` spawns
    (``provision_inline=False``). Only rows that are launched for this job and
    still lack data.jobdiva_candidate_id are taken, each under a
    data.jobdiva_provisioning_claimed_at claim (stale after 15 min), then
    provisioned with `_provision_batch_to_jobdiva` in chunks of 25.

    Returns ``{"requested", "claimed", "not_claimed", "success", "skipped",
    "failed", "duplicate_suspected", "blank_profile_refused"}``. Never raises
    for a provisioning failure (counted in ``failed``); a claim DB error
    raises so the caller can retry later.
    """
    ids = [str(c) for c in dict.fromkeys(candidate_ids or []) if str(c).strip()]
    out: Dict[str, Any] = {
        "requested": len(ids), "claimed": 0, "not_claimed": 0, "success": 0, "skipped": 0,
        "failed": 0, "duplicate_suspected": 0, "blank_profile_refused": 0,
    }
    if not ids or not job_id:
        return out
    if not common.jobdiva_provisioning_enabled():
        # Kill switch: nothing claimed (a later run with it on still takes them).
        logger.warning(
            "launch provisioning disabled (JOBDIVA_PROVISIONING_ENABLED=false) job=%s n=%d", job_id, len(ids),
        )
        out["not_claimed"] = len(ids)
        out["skipped"] = len(ids)
        return out
    numeric_job_id, ref_job_id = await _resolve_provisioning_job_ids(str(job_id))
    keys = _job_key_list((str(job_id), numeric_job_id or "", ref_job_id or ""))
    claimed = await asyncio.to_thread(_claim_rows_for_provisioning, keys, ids)
    out["claimed"] = len(claimed)
    out["not_claimed"] = len(ids) - len(claimed)
    for i in range(0, len(claimed), _PROVISION_CHUNK):
        chunk = claimed[i:i + _PROVISION_CHUNK]
        try:
            async with _trace_span("jobdiva_provisioning", n=len(chunk)):
                res = await _provision_batch_to_jobdiva(chunk, str(job_id), label="Launch Provisioning")
        except Exception as exc:  # noqa: BLE001 — provisioning never fails a launch
            logger.error("launch provisioning chunk failed job=%s n=%d: %s", job_id, len(chunk), exc)
            res = {"failed": len(chunk)}
        for k in ("success", "skipped", "failed", "duplicate_suspected", "blank_profile_refused"):
            out[k] += int((res or {}).get(k, 0) or 0)
    return out

# ---------------------------------------------------------------------------
# Notification Helpers
# ---------------------------------------------------------------------------

async def _check_and_fire_candidate_passed_notification(
    interview_id: int,
    detail_payload: Dict[str, Any],
    job_id: str,
    candidate_id: str,
):
    """
    Checks if a candidate passed the phone screen criteria and fires Email #3.
    Criteria: PASS on all hard filters AND match score > 70%.
    """
    try:
        if not job_id or not candidate_id:
            return

        # 1. New Pass Criteria (Strictly from Webhook Payload)
        interview_block = detail_payload.get("interview", {})
        hf_status = str(interview_block.get("hard_filter_status") or "").lower()
        cand_score = interview_block.get("candidate_score")
        total_possible = interview_block.get("total_score")
        
        # Send pass email only when all hard filters are explicitly passed.
        meets_criteria = hf_status in (HARD_FILTER_PASS_STATUS, "pass")
        
        normalized_score_display = "Passed"
        if meets_criteria and cand_score is not None and total_possible:
            normalized_score_display = _format_normalized_score_100(cand_score, total_possible) or "Passed"
        
        if not meets_criteria:
            logger.info(f"⏭️ Candidate {candidate_id} did not meet strict pass criteria (HF: {hf_status}, Score: {cand_score}/{total_possible}).")
            return

        score_display = normalized_score_display if cand_score is not None else "Passed"

        # 5. Fetch Job & Candidate metadata for email
        conn = common._get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 4. Fetch Job & Candidate metadata for email
        cur.execute("""
            SELECT job_id, title, enhanced_title, city, state, pay_rate, recruiter_emails, jobdiva_id
            FROM monitored_jobs
            WHERE job_id = %s OR jobdiva_id = %s
            ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
            LIMIT 1
        """, (job_id, job_id))
        job_row = cur.fetchone()
        
        cur.execute("""
            SELECT name, email, phone, resume_text, data, source
            FROM sourced_candidates
            WHERE candidate_id = %s AND jobdiva_id = %s
            LIMIT 1
        """, (candidate_id, job_row["jobdiva_id"] if job_row else job_id))
        cand_row = cur.fetchone()

        if not job_row or not cand_row:
            cur.close()
            conn.close()
            return

        # Deduplication: Atomically set engage_passed_email_sent=True and check old value.
        # This prevents a race condition where two concurrent webhook calls both read the
        # flag as False before either has written True, resulting in duplicate emails.
        jobdiva_id_for_dedup = job_row["jobdiva_id"] if job_row else job_id

        def _rollback_passed_email_flag():
            try:
                r_conn = get_db_connection()
                r_cur = r_conn.cursor()
                r_cur.execute("""
                    UPDATE sourced_candidates
                    SET data = data - 'engage_passed_email_sent',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s AND jobdiva_id = %s
                """, (candidate_id, jobdiva_id_for_dedup))
                r_conn.commit()
                r_cur.close()
                r_conn.close()
                logger.info(f"↩️ Rolled back engage_passed_email_sent flag for candidate {candidate_id} / job {jobdiva_id_for_dedup}")
            except Exception as r_err:
                logger.error(f"Failed to rollback engage_passed_email_sent flag: {r_err}")

        cur.execute("""
            UPDATE sourced_candidates
            SET data = COALESCE(data, '{}'::jsonb) || '{"engage_passed_email_sent": true}'::jsonb,
                updated_at = CURRENT_TIMESTAMP
            WHERE candidate_id = %s
              AND jobdiva_id = %s
              AND (data IS NULL OR (data->>'engage_passed_email_sent')::boolean IS NOT TRUE)
            RETURNING id
        """, (candidate_id, jobdiva_id_for_dedup))
        updated = cur.fetchone()
        conn.commit()

        if not updated:
            # Another concurrent request already set the flag — skip to avoid duplicate
            logger.info(f"⏭️ Passed email already sent for candidate {candidate_id} / job {job_id}. Skipping duplicate.")
            cur.close()
            conn.close()
            return

        cand_data = cand_row.get("data") or {}

        # 5. Build screening summary (all items in evaluation/transcriptions)
        screening_summary = []
        transcriptions = detail_payload.get("transcriptions") or []
        
        if transcriptions:
            for item in transcriptions:
                q_text = item.get("question") or "Question"
                a_text = item.get("answer") or "—"
                score = item.get("candidate_score")
                total = item.get("total_score", 10.0)
                reason = item.get("reason")
                hf_status_item = item.get("hard_filter_status")
                q_order_raw = item.get("question_order", 0)
                reason_norm = str(reason or "").strip().lower()

                hf_norm = str(hf_status_item or "").strip().lower().replace(" ", "_")
                is_hard_filter = hf_norm not in ("", "not_hard_filter", "na", "n/a", "none")
                # Keep email behavior aligned with Pairbot UI:
                # - no score for hard-filter questions
                # - no score for info-only questions
                # - score only for truly scored evaluation questions
                score_value = None
                try:
                    score_value = float(score) if score is not None else None
                except (TypeError, ValueError):
                    score_value = None

                try:
                    q_order = int(q_order_raw)
                except (TypeError, ValueError):
                    q_order = 0

                # Pairbot contract:
                # Q1/Q4 hard filters (already excluded above), Q2,3,5-9 info-only, Q10+ scored.
                if q_order > 0:
                    is_info_only = (not is_hard_filter) and (q_order <= 9)
                else:
                    # Legacy fallback when question_order is missing
                    # Treat explicit informational analysis as info-only even if
                    # partner API sends a placeholder score like 0.0.
                    looks_informational = (
                        "informational answer captured" in reason_norm
                        or "info-only" in reason_norm
                        or "information only" in reason_norm
                    )
                    is_info_only = (not is_hard_filter) and (
                        looks_informational or score_value is None or score_value < 0
                    )
                is_scored_question = (not is_hard_filter) and (not is_info_only)

                # Exclude the bot's closing sentence which is always scored 0.0/0.
                # The ONLY reliable signal is total_score == 0 (explicitly sent as 0 by
                # pairbot) — no real evaluation question is ever scored out of 0 (they are
                # always out of 10.0).
                # We intentionally do NOT check the answer text because newer pairbot
                # versions populate the closing-sentence answer with the full thank-you
                # paragraph, so checking for empty/"—" would miss it.
                #
                # Assumption (info-only): info-only questions (Q2,3,5-9) always carry
                # a non-zero total_score in production. We have not observed total_score==0
                # for any real info-only item in payload logs. The logger.warning below
                # will surface any unexpected case if that assumption ever breaks.
                #
                # Intentional tradeoff (null total_score): we do NOT include
                # total_value is None in the condition. A null total_score on a real
                # scored question (e.g. a transient API omission) should surface in the
                # email rather than be silently dropped. If pairbot sends null for the
                # closing sentence, that item will pass through — that is the safer choice.
                try:
                    total_value = float(total) if total is not None else None
                except (TypeError, ValueError):
                    total_value = None
                is_closing_sentence = (
                    score_value == 0.0
                    and total_value == 0.0
                    and not is_hard_filter
                )
                if is_closing_sentence:
                    logger.warning(
                        "⏭️ Skipping item as bot closing sentence "
                        "(score=%.1f, total=%.1f, q_order=%d): %.60r",
                        score_value, total_value, q_order, q_text,
                    )
                    continue

                screening_summary.append({
                    "question": q_text,
                    "answer": a_text,
                    "score": score,
                    "total_score": total,
                    "reason": reason,
                    "question_order": q_order,
                    "hard_filter_status": hf_status_item,
                    "is_hard_filter": is_hard_filter,
                    "is_info_only": is_info_only,
                    "is_scored_question": is_scored_question,
                })
        elif hf_status != "":
            # Fallback for simple status-based payload
            screening_summary.append({"field": "Hard Filter Status", "value": str(hf_status).capitalize()})
            screening_summary.append({"field": "Phone Screen Score", "value": score_display})
        else:
            # Legacy fallback
            for ev in (detail_payload.get("evaluation") or []):
                screening_summary.append({
                    "field": ev.get("question", "Question"),
                    "value": ev.get("answer", ev.get("status", "—"))
                })

        # Resolve Numeric Candidate ID for JobDiva API calls
        jd_candidate_id = candidate_id
        if cand_data and (cand_data.get("jobdiva_candidate_id") or cand_data.get("candidate_id")):
            potential_id = cand_data.get("jobdiva_candidate_id") or cand_data.get("candidate_id")
            if str(potential_id).isdigit():
                jd_candidate_id = str(potential_id)

        # 6. Prepare attachment (resume text as Word-compatible .doc fallback)
        resume_bytes = None
        resume_filename = None

        # Reuse the same quality gate as the View Resume endpoint:
        # attach only real JobDiva resume text, skip placeholder/generated text.
        blocked_resume_markers = (
            "Professional experience details available upon request",
            "Experienced professional with a strong background",
            "Contact information and detailed work history available upon request",
            "Resume content unavailable",
        )

        # Prefer the full JobDiva resume text for the attachment; if JobDiva
        # returns nothing/placeholder text, we fall back to the locally stored
        # resume below so the pass email still carries an attachment.
        fetched_jobdiva_resume = ""
        if str(jd_candidate_id).isdigit():
            try:
                jd_resume = await jobdiva_service.get_candidate_resume(candidate_id=str(jd_candidate_id))
                if isinstance(jd_resume, dict):
                    fetched_jobdiva_resume = jd_resume.get("resume_text") or ""
            except Exception as resume_err:
                logger.warning(
                    "Failed to fetch JobDiva resume for candidate %s: %s",
                    jd_candidate_id,
                    resume_err,
                )

        resume_text, used_local_resume_fallback = _select_pass_email_resume(
            fetched_jobdiva_resume, cand_row.get("resume_text"), blocked_resume_markers
        )
        if used_local_resume_fallback:
            logger.info(
                "📎 Using locally stored resume fallback for pass email (candidate %s / job %s)",
                candidate_id,
                job_id,
            )

        if (resume_text or "").strip():
            resume_bytes = _build_word_resume_document(cand_row["name"] or "Candidate", resume_text)
            # Try to get name from candidate
            safe_name = "".join(c for c in (cand_row["name"] or "Candidate") if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
            resume_filename = f"Resume_{safe_name}_{job_id}.doc"
        
        app_job_id = str(job_row.get("job_id") or job_id)
        jd_job_id = str(job_row.get("jobdiva_id") or job_id)

        # 7. Fire the email & Update JobDiva Qualification
        recruiter_emails = _parse_json_list(job_row.get("recruiter_emails", []))
        
        # Determine the completion timestamp for the qualification update
        completion_ts = interview_block.get("completed_at") or interview_block.get("evaluation_completed_at")
        if not completion_ts:
            completion_ts = datetime.now(timezone.utc).isoformat()

        # Update JobDiva Qualification: PAIR Candidates = PASS
        # We fire these asynchronously so they don't block the email or main flow
        await enqueue(
            UPDATE_JOBDIVA_QUALIFICATION,
            str(jd_candidate_id), JOBDIVA_PAIR_QUALIFICATION_NAME, JOBDIVA_PASS_QUALIFICATION_VALUE,
            JOBDIVA_PAIR_RECRUITER_ID, str(completion_ts), JOBDIVA_PAIR_QUALIFICATION_ID,
        )

        # Create JobDiva Note: PAIR Pass Candidate Report
        # Note: We use the job title from job_row for the message
        base_url = resolve_app_base_url(request.app_base_url if 'request' in locals() else "")
        pair_job_title = job_row.get("enhanced_title") or job_row.get("title") or "the"
        report_link = candidate_report_link(base_url, jd_job_id, candidate_id)
        safe_report_link = html.escape(report_link, quote=True)
        safe_pair_job_title = html.escape(str(pair_job_title))
        note_text = f"Candidate completed Phone Screen for {safe_pair_job_title} position. <a href=\"{safe_report_link}\" target=\"_blank\">Click Here</a> to view the report."
        
        await enqueue(
            CREATE_JOBDIVA_PASS_NOTE,
            str(jd_candidate_id), str(jd_job_id), JOBDIVA_PASS_ACTION_NAME, note_text, JOBDIVA_PAIR_RECRUITER_ID,
        )

        # The JobDiva profile the email links to: the same id the Cross
        # Submissions email and panel open (a JobDiva row's own id, else the
        # profile Launch PAIR created/matched); "" for an unlinked LinkedIn row.
        from services.cross_submissions import jobdiva_profile_id_for
        jd_profile_id = jobdiva_profile_id_for(
            cand_row.get("source"), candidate_id, cand_data if isinstance(cand_data, dict) else {}
        )

        success = await asyncio.to_thread(
            notify_candidate_passed,
            candidate_name=cand_row["name"] or "Candidate",
            candidate_email=cand_row["email"],
            candidate_phone=cand_row["phone"],
            screen_score=score_display,
            summary=interview_block.get("summary") or "Passed screening criteria.",
            screening_summary=screening_summary,
            jobdiva_id=job_row["jobdiva_id"] or job_id,
            job_title=job_row.get("enhanced_title") or job_row.get("title") or "",
            location=f"{job_row['city']}, {job_row['state']}" if job_row['city'] else "—",
            salary_range=job_row["pay_rate"] or "—",
            recruiter_emails=recruiter_emails,
            resume_bytes=resume_bytes,
            resume_filename=resume_filename,
            candidate_id=candidate_id,
            job_id=app_job_id,
            jobdiva_candidate_id=jd_profile_id,
        )

        if success:
            # Flag was already set atomically before sending (race-condition-safe).
            # Refresh Performance Metrics for this job (e.g. Time to First Pass)
            await enqueue(REFRESH_JOB_METRICS, str(job_id), job_id=f"{REFRESH_JOB_METRICS}:{job_id}")
        else:
            logger.warning(f"⚠️ Email send failed for candidate {candidate_id} / job {job_id}. Rolling back dedup flag.")
            _rollback_passed_email_flag()

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"❌ Failed to process Candidate Passed notification: {e}", exc_info=True)
        if 'updated' in locals() and updated and '_rollback_passed_email_flag' in locals():
            _rollback_passed_email_flag()
