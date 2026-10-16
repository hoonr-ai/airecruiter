"""
Engagement Router - Engage & Assess Button Endpoints

Provides endpoints for:
1. Generating interview payloads for candidates (Engage)
2. Sending bulk interview requests to PAIR API (Engage)
3. Looking up latest interview for a candidate (Assess)
4. Proxying PAIR dashboard data for assessment display (Assess)

Auto-creates the engage_interview_audit table on startup.
"""

import asyncio
import contextlib
import hashlib
import html
import inspect
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional, List, Any, Dict, Tuple, AsyncIterator, Callable, Sequence
import psycopg2.extras
import json
import logging
import os
import httpx
import re
from datetime import datetime, timezone, timedelta
from routers._helpers import (
    get_db_connection,
    _verify_job_access_by_id,
    get_article_for_title,
    is_legacy_default_bot_intro,
    normalize_bot_intro_tokens,
)
from services.pair_auth import get_pair_auth_headers
from core.tasks import spawn as _spawn_task
from core.pipeline_trace import new_run as _trace_new_run, span as _trace_span

from core.email import (
    notify_pair_launched,
    notify_job_posting,
    notify_candidate_passed,
    _build_word_resume_document,
    candidate_report_link,
    resolve_app_base_url,
)
from services.gender_logic import normalize_gender_prediction, infer_gender_from_name_ai
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
from services.auto_assign_service import auto_assign_service
from services.notifications_service import create_pair_launched_notification
from core.auth import UserIdentity, get_current_user
from core import (
    JOBDIVA_PAIR_RECRUITER_ID,
    JOBDIVA_PAIR_QUALIFICATION_NAME,
    JOBDIVA_PAIR_QUALIFICATION_ID,
    JOBDIVA_PASS_ACTION_NAME,
    JOBDIVA_PASS_QUALIFICATION_VALUE,
    DEFAULT_SCREENING_LEVEL,
)
from utils.email_utils import is_placeholder_email, _EMAIL_RE

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Engagement"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EXTERNAL_INTERVIEW_API_URL = os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai")
PASS_SCORE_THRESHOLD = float(os.getenv("PASS_SCORE_THRESHOLD", "70"))
PASS_CANDIDATE_SCORE_RATIO = float(os.getenv("PASS_CANDIDATE_SCORE_RATIO", "0.7"))
HARD_FILTER_PASS_STATUS = os.getenv("HARD_FILTER_PASS_STATUS", "passed").lower()
ENGAGE_PASSED_STATUSES = os.getenv("ENGAGE_PASSED_STATUSES", "completed,passed").lower().split(",")

# Bound concurrent JobDiva provisioning calls. send_bulk_interview fan-outs one
# _provision_candidate_to_jobdiva per candidate; without this, a 50-candidate
# bulk-engage fires 50 simultaneous JobDiva HTTP calls AND grabs 50 DB pool
# slots (briefly, after Fix 1). 5 matches the scale jobdiva_ratelimit_probe.py
# has been probing — tunable via env if JobDiva's rate budget changes.
_PROVISION_CONCURRENCY = asyncio.Semaphore(int(os.getenv("PROVISION_CONCURRENCY", "5")))

# A person PAIR creates in JobDiva gets a résumé built from everything we hold
# (services/profile_resume.py). When that is thin -- no work history, summary or
# résumé text, typically an Exa deep-search row -- the LinkedIn profile is read
# once more through Unipile before the JobDiva profile is created. Bounded per
# provisioning batch: each read is a LinkedIn profile view on a recruiter seat.
JOBDIVA_PROVISION_LINKEDIN_REFETCH = os.getenv(
    "JOBDIVA_PROVISION_LINKEDIN_REFETCH", "true"
).strip().lower() in {"1", "true", "yes", "on"}
JOBDIVA_PROVISION_LINKEDIN_REFETCH_CAP = max(0, int(os.getenv("JOBDIVA_PROVISION_LINKEDIN_REFETCH_CAP", "40") or 0))
# Grep / alert marker for a create the provisioner refused because the person
# would have landed in JobDiva as a blank profile (no usable name, nothing else).
JOBDIVA_BLANK_PROFILE_REFUSED = "JOBDIVA_BLANK_PROFILE_REFUSED"


def jobdiva_provisioning_enabled() -> bool:
    """JOBDIVA_PROVISIONING_ENABLED kill switch (default true), read at call
    time. false = no launch path creates or links JobDiva candidates/
    applications (the inline per-batch provisioning, the launch-session
    stage, /engage/re-provision): a local end-to-end launch must never
    write JobDiva."""
    return os.getenv("JOBDIVA_PROVISIONING_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }
_NR_RESPONSE_BODY_LIMIT = 16000


def _log_generate_payload_response_to_newrelic(response_obj: Dict[str, Any], *, level: str = "info") -> None:
    """Best-effort New Relic logging for /engage/generate-payload responses.

    Sizes and counts only. The payload is every candidate's name, email, phone
    and résumé, and the error text can quote them, so neither goes to New
    Relic (the event used to carry the first 16 KB of the response)."""
    try:
        from core.newrelic import is_enabled, record_custom_event, record_message
        if not is_enabled():
            return
    except Exception:
        return

    try:
        response_text = json.dumps(response_obj, default=str)
    except Exception:
        response_text = str(response_obj)

    try:
        event_data: Dict[str, Any] = {
            "api_endpoint": "/engage/generate-payload",
            "api_operation": "generate_payload",
            "success": bool(response_obj.get("success")) if isinstance(response_obj, dict) else False,
            "response_size_bytes": len(response_text),
            "truncated": len(response_text) > _NR_RESPONSE_BODY_LIMIT,
        }
        if isinstance(response_obj, dict):
            for key in ("candidate_count", "dnc_blocked_count"):
                if isinstance(response_obj.get(key), int):
                    event_data[key] = response_obj[key]
            if isinstance(response_obj.get("candidate_ids"), list):
                event_data["requested_count"] = len(response_obj["candidate_ids"])
            if response_obj.get("job_id"):
                event_data["job_id"] = str(response_obj["job_id"])[:64]
            if response_obj.get("error_type"):
                event_data["error_type"] = str(response_obj["error_type"])[:80]
        record_custom_event("EngageGeneratePayload", event_data)
        record_message("Engage generate-payload response", attributes=event_data, level=level)
    except Exception:
        # New Relic logging must never affect the API path.
        return


def _nr_suppress_apdex() -> None:
    """Keep a minutes-long streaming endpoint out of Apdex (core.newrelic
    helper when present; fail-open no-op otherwise)."""
    try:
        from core import newrelic as _nr
        fn = getattr(_nr, "suppress_apdex", None)
        if callable(fn):
            fn()
    except Exception:  # noqa: BLE001 — telemetry never breaks a request
        pass


def _log_recordings_upstream_failure(interview_id: str, exc: BaseException) -> None:
    """Best-effort New Relic event for a PAIR-detail failure on the
    recordings endpoint. The endpoint itself returns 200 (unavailable: true)
    so the modal degrades gracefully, which means a status-code-based
    monitor would never see the outage — this event is what an alert can
    fire on instead.
    """
    try:
        from core.newrelic import is_enabled, record_custom_event, record_message
        if not is_enabled():
            return
    except Exception:
        return

    try:
        event_data = {
            "api_endpoint": "/interviews/{interview_id}/recordings",
            "interview_id": interview_id,
            "error": str(exc)[:500],
        }
        record_custom_event("RecordingsUpstreamFailure", event_data)
        record_message("Call recordings upstream (PAIR) lookup failed", attributes=event_data, level="warning")
    except Exception:
        return

def _parse_json_list(val) -> list:
    """Safely parse a value that may be a JSON string, list, or empty."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return [e.strip() for e in val.split(",") if e.strip()]
    return []


def _fetch_stored_employer_signals(
    conn, candidate_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Employer signals persisted OUTSIDE `sourced_candidates.data`.

    The applicant sync writes `data` BEFORE any resume extraction runs (auto-
    sync passes bypass_screening=True, which skips LLM enrichment entirely), so
    on the auto-launch path that blob carries no employer fields at all. The
    extracted employment history lands in `candidate_enhanced_info` instead —
    a separate table, UNIQUE on candidate_id — and is only ever joined back in
    memory at read time (`_attach_cached_enhanced_info`), never persisted.

    The result was that the UI rendered "Bank Of America … Present" from the
    hydrated blob while the launch gate, reading only `data`, matched against
    an empty candidate and let the outreach through. Everything the gate reads
    must come from the same view of the candidate the recruiter sees.
    """
    out: Dict[str, Dict[str, Any]] = {}
    # `c is not None` before str(): str(None) is the truthy literal "None",
    # which would otherwise be sent to the DB as a candidate id.
    ids = [str(c) for c in (candidate_ids or []) if c is not None and str(c).strip()]
    if not ids:
        return out
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT candidate_id, job_title, company_experience
                FROM candidate_enhanced_info
                WHERE candidate_id = ANY(%s)
                """,
                (ids,),
            )
            for row in cur.fetchall() or []:
                out[str(row["candidate_id"])] = {
                    "company_experience": row.get("company_experience") or [],
                    "title": row.get("job_title") or "",
                }
    except Exception as exc:  # noqa: BLE001 — hydration must never block a launch
        logger.warning(
            "employer-signal hydration failed (exclusion gate may run blind): %s", exc
        )
    return out


def _merge_employer_signals(
    candidate: Dict[str, Any],
    headline: str = "",
    signals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """`candidate` plus any employer signal it was missing, for the gate.

    Copy-on-merge so the caller's stored blob is never mutated, and strictly
    fill-if-absent — a value already on the row wins over the cached one.
    `headline` comes from the sourced_candidates column (which the gate's
    queries must select): `collect_current_companies` has an "… at X" parse
    built for rows that carry the employer only in headline text, and it could
    never fire at launch while that column went unread.
    """
    merged = dict(candidate) if isinstance(candidate, dict) else {}
    if headline and not merged.get("headline"):
        merged["headline"] = headline
    signals = signals or {}
    if signals.get("company_experience") and not merged.get("company_experience"):
        merged["company_experience"] = signals["company_experience"]
    if signals.get("title") and not merged.get("title"):
        merged["title"] = signals["title"]
    # JobDiva's own parsed work history (services/employer_resolution.py):
    # free-text lines the gate matches one-directionally, attached by the
    # launch-time resolution pass alongside any fresh resume extraction.
    if signals.get("jobdiva_profile_experience") and not merged.get("jobdiva_profile_experience"):
        merged["jobdiva_profile_experience"] = signals["jobdiva_profile_experience"]
    return merged


def _exa_resume_texts(candidate: Dict[str, Any]) -> Tuple[List[str], List[str], bool]:
    """Extract Exa highlights as unstructured text for the launch gate.

    Exa candidates often lack structured company data (vague headline), but
    their `resume_text` (LinkedIn highlights) almost always has it. This helper
    splits highlights into experience fragments and routes only fragments with
    an explicit current marker or an open-ended date range to the
    current-employer ladder. Other fragments go to the last-employer ladder.
    This keeps a former client role from being mistaken for a current one when
    Exa returns multiple roles in the same highlight line.
    """
    cand_data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    source = str(
        candidate.get("source") or cand_data.get("source") or candidate.get("source_type") or ""
    ).strip().lower()

    if source not in ("linkedin-exa", "exa"):
        return [], [], False

    text = str(candidate.get("resume_text") or cand_data.get("resume_text") or "").strip()
    if not text:
        return [], [], True

    current: List[str] = []
    last: List[str] = []
    current_marker = re.compile(
        r"\b(?:19|20)\d{2}\s*(?:[-–—]|to)\s*(?:present|current|now)\b"
        r"|[\(\[]\s*(?:present|current|now)\s*[\)\]]"
        r"|\bcurrent\s+(?:role|position|employer|employment)\b"
        r"|\bcurrently\s+(?:at|with|employed|working)\b",
        re.IGNORECASE,
    )
    open_ended_date = re.compile(
        r"\b(?:19|20)\d{2}\s*(?:[-–—]|to)\s*"
        r"(?:present|current|now)?\s*[\])}.,;:]*\s*$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        # Exa may place two roles on one line. Split at semicolons so a
        # current role's date does not make a former role on that same line
        # look current too.
        for fragment in re.split(r";\s*", line):
            fragment = fragment.strip()
            if not fragment:
                continue
            if current_marker.search(fragment) or open_ended_date.search(fragment):
                current.append(fragment)
            else:
                last.append(fragment)

    return current, last, True


def is_candidate_excluded_from_pair(candidate: Dict[str, Any], client_name: str = "") -> Tuple[bool, str]:
    """Check if a candidate should be excluded from PAIR outreach.

    Excludes:
      1. Current Employee / Working through Pyramid Consulting
      2. Offer Extended
      3. Offer Accepted
      4. Current Employee of the hiring client
      5. Current or LAST employer on the no-contact company list

    Callers MUST pass a candidate hydrated via `_merge_employer_signals` —
    a bare `sourced_candidates.data` blob can be missing every field checked
    below, in which case this returns "not excluded" because it had nothing to
    judge, not because the candidate is clean.
    """
    if not candidate or not isinstance(candidate, dict):
        return False, ""

    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    enhanced = data.get("enhanced_info") if isinstance(data.get("enhanced_info"), dict) else {}

    for avail_field in [data.get("available"), candidate.get("available")]:
        if avail_field is False or str(avail_field).strip().lower() == "false":
            return True, "Current Employee (Pyramid / Unavailable)"

    quals = data.get("qualifications") or candidate.get("qualifications") or enhanced.get("qualifications") or []
    if isinstance(quals, list):
        for q in quals:
            if not isinstance(q, dict):
                continue
            q_val = str(q.get("qualificationValue") or q.get("value") or "").strip()
            q_val_lower = q_val.lower()
            if "current employee" in q_val_lower:
                return True, "Current Employee (Pyramid)"
            if "offer extended" in q_val_lower or "offer - extended" in q_val_lower:
                return True, "Offer Extended"
            if "offer accepted" in q_val_lower or "offer - accepted" in q_val_lower or q_val_lower in {"placed", "hired"}:
                return True, "Offer Accepted"

    status_strs = [
        str(data.get("employee_status") or "").strip(),
        str(data.get("available") or "").strip(),
        str(data.get("availability_status") or "").strip(),
        str(data.get("status") or "").strip(),
        str(candidate.get("status") or "").strip(),
    ]

    for st in status_strs:
        if not st:
            continue
        st_lower = st.lower()
        if "offer extended" in st_lower or "offer - extended" in st_lower:
            return True, "Offer Extended"
        if "offer accepted" in st_lower or "offer - accepted" in st_lower or st_lower in {"placed", "hired"}:
            return True, "Offer Accepted"
        if "current employee" in st_lower:
            return True, "Current Employee (Pyramid)"

    # Shared matcher (services/company_match.py) so search-time filtering
    # (Exa/Unipile) and this launch gate can't disagree. Covers flat
    # company fields, current company_experience entries, Exa deep-search
    # `exa_recent_companies`, and an "… at X" headline parse — Unipile/Exa
    # rows often carry the employer only in headline text.
    from services.company_match import (
        collect_current_companies,
        employed_by_client,
        is_placeholder_client,
        normalize_company_name,
    )

    current_companies = collect_current_companies(candidate)
    for comp in current_companies:
        if "pyramid" in normalize_company_name(comp).split():
            return True, "Current Employee (Pyramid)"

    # The candidate's own interview answer to "which company do you currently
    # work for?" (persisted by the voice-agent webhook as
    # stated_current_employer). Their own words beat every extracted signal,
    # so this runs even when a structured current employer is on file. Free
    # text — matched one-directionally only (client ⊂ answer). The no-contact
    # side of the same answer is handled inside check_no_contact below.
    stated_text = str(
        data.get("stated_current_employer")
        or candidate.get("stated_current_employer")
        or ""
    ).strip()
    if stated_text and client_name and not is_placeholder_client(client_name):
        from services.company_match import client_appears_in_text
        if client_appears_in_text(stated_text, client_name):
            return True, "Employed by Hiring Client (stated in interview)"

    # Works at the client TODAY. The last employer is consulted only when no
    # current employer is on file at all (many source rows carry no "Present"
    # entry) — a fallback, not a union, so someone who left the client for a
    # named employer stays contactable. Matching is on whole-word tokens, not
    # raw substrings: client "Meta" matches "Meta Platforms", never "Metadata
    # Solutions".
    client_hit = employed_by_client(candidate, client_name)
    if client_hit:
        if client_hit["relation"] == "current":
            return True, "Employed by Hiring Client"
        if client_hit["relation"] == "placement":
            return True, "Placed at Hiring Client (current engagement)"
        return True, "Employed by Hiring Client (last known employer)"

    # No-contact companies (services/no_contact.py): current or LAST employer
    # on the code-managed list. Step 5 greys these rows out and /candidates/
    # save refuses to persist them, so reaching here means a stale session or
    # legacy stored row — block outreach regardless.
    try:
        from services.no_contact import check_no_contact
        nc_hit = check_no_contact(candidate)
    except Exception:  # noqa: BLE001 — a matcher error must not block launches
        nc_hit = None
    if nc_hit:
        return True, f"No-Contact Company ({nc_hit['keyword']})"

    # JobDiva profile work history (jobdiva_profile_experience, attached by
    # the launch-time employer resolution pass): free-text resume lines with
    # structured dates. Too noisy for the structured matchers, so:
    #   - no-contact keywords scan the current + last lines one-directionally
    #     (keyword ⊂ text — the same safe direction the keyword matcher
    #     already uses; over-matching skips a contactable candidate, which is
    #     the side these thresholds deliberately err toward);
    #   - the hiring client matches only via client_appears_in_text
    #     (client ⊂ text, never the reverse) and only as a FALLBACK below the
    #     structured signals, mirroring employed_by_client's current-then-last
    #     ladder — a named structured employer that isn't the client still
    #     means the candidate stays contactable.
    profile_current_texts: List[str] = []
    profile_last_texts: List[str] = []
    try:
        from services.employer_resolution import candidate_profile_texts
        profile_current_texts, profile_last_texts = candidate_profile_texts(candidate)
    except Exception:  # noqa: BLE001 — profile parsing must not block launches
        pass

    # For Exa candidates, structured company data is often absent (vague headline),
    # but their current employer is almost always present in the raw `resume_text`.
    exa_current, exa_last, is_exa_source = _exa_resume_texts(candidate)
    profile_current_texts.extend(exa_current)
    profile_last_texts.extend(exa_last)

    # Reuse the source classification from the parser rather than normalizing
    # nested source fields a second time.
    conflict_suffix = "Exa profile text" if is_exa_source else "JobDiva profile"

    if profile_current_texts or profile_last_texts:
        try:
            from services.no_contact import matches_no_contact_company
            for text in profile_current_texts + profile_last_texts:
                kw = matches_no_contact_company(text)
                if kw:
                    return True, f"No-Contact Company ({kw})"
        except Exception:  # noqa: BLE001
            pass
        if client_name and not is_placeholder_client(client_name):
            from services.company_match import (
                client_appears_in_text,
                collect_last_companies as _collect_last,
            )
            if not current_companies:
                for text in profile_current_texts:
                    if client_appears_in_text(text, client_name):
                        return True, f"Employed by Hiring Client ({conflict_suffix})"
                # Last-resort ladder rung: consulted only when NO structured
                # signal of any kind exists — a structured last employer that
                # already cleared employed_by_client must not be second-guessed
                # by a noisier source.
                if not profile_current_texts and not _collect_last(candidate):
                    for text in profile_last_texts:
                        if client_appears_in_text(text, client_name):
                            return True, (
                                "Employed by Hiring Client "
                                f"(last known employer, {conflict_suffix})"
                            )

    # Visibility for the failure mode that put a Bank of America employee into
    # outreach for a Bank of America req: a real hiring client, but not one
    # current-employer signal on the row, so the check above had nothing to
    # match. Passing here means UNKNOWN, not clean. We still let the launch
    # proceed (blocking would hold back every applicant synced before resume
    # extraction runs), but it must not be silent — this was invisible until a
    # recruiter happened to read a phone-screen notification.
    if current_companies or not client_name or is_placeholder_client(client_name):
        return False, ""
    from services.company_match import collect_last_companies

    if (
        collect_last_companies(candidate)
        or profile_current_texts
        or profile_last_texts
        or stated_text
    ):
        return False, ""
    logger.warning(
        "client_conflict_check_blind candidate=%s client=%r — no current- or "
        "last-employer signal on the stored row; hiring-client exclusion could "
        "not be evaluated",
        candidate.get("candidate_id") or candidate.get("id") or "?",
        client_name,
    )
    return False, ""


def _format_normalized_score_100(score: Any, total: Any) -> Optional[str]:
    """Format raw score/total as the same normalized 100-point score used in the report."""
    if score is None or total in (None, 0, 0.0, "0", ""):
        return None

    try:
        normalized_score = (float(score) / float(total)) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    if normalized_score.is_integer():
        return f"{int(normalized_score)}/100"
    return f"{round(normalized_score, 1):.1f}/100"





def _sanitize_pair_candidate_email(raw: str) -> str:
    """Return a usable email for PAIR outreach, or "" when missing/placeholder.

    Email is OPTIONAL for launch — PAIR reaches candidates by phone — so a dead
    or absent address (incl. JobDiva `Auto_<id>@jobdiva.com` placeholders) is
    blanked rather than blocking the launch. A real, well-formed address is
    kept so email outreach can still go out when available.
    """
    cleaned = (raw or "").strip().lower()
    if not cleaned or not _EMAIL_RE.match(cleaned) or is_placeholder_email(cleaned):
        return ""
    return cleaned


def _pair_phone_digits(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _validate_pair_payload_contacts(payload_obj: Dict[str, Any]) -> None:
    """PAIR launch gate: require at least ONE usable contact method per resume —
    a usable phone (PAIR calls the candidate) OR a real email (PAIR emails them).
    Either alone is enough. Placeholder/dead emails are blanked, not rejected,
    and only block launch when there is also no usable phone.
    """
    resumes = payload_obj.get("resumes")
    if not isinstance(resumes, list):
        raise HTTPException(status_code=400, detail="Payload is missing resumes")

    for idx, resume in enumerate(resumes, start=1):
        if not isinstance(resume, dict):
            raise HTTPException(status_code=400, detail=f"Resume {idx} payload is invalid")

        # Sanitize the email (blank dead/placeholder addresses). A real address
        # that survives sanitizing counts as a usable contact method.
        clean_email = _sanitize_pair_candidate_email(str(resume.get("email") or ""))
        resume["email"] = clean_email

        # Need at least one way to reach the candidate: a usable phone (≥7
        # digits, matching the frontend launch gate) OR a real email. Either
        # one alone is sufficient to launch.
        has_phone = len(_pair_phone_digits(resume.get("phone"))) >= 7
        has_email = bool(clean_email)
        if not has_phone and not has_email:
            who = resume.get("name") or resume.get("candidate_name") or f"Resume {idx}"
            raise HTTPException(
                status_code=400,
                detail=f"A usable phone number or email is required before launching PAIR ({who}).",
            )


# Patterns that identify the Compensation (Q5) and Work-Arrangement / Job-Type
# (Q6) front-matter questions. When a recruiter sets a pass criterion on either
# of these — e.g. "Must be open to W2 with Pyramid" — they are knockout gates
# and must be treated as hard filters regardless of screening level.
#
# DESIGN NOTES:
# - Patterns are intentionally anchored / context-rich to avoid false positives
#   on role-specific questions that happen to mention related terms in passing.
#   e.g. "Have you managed subcontractors?" must NOT match.
# - Bare "c2c", "1099", "subcontract" are NOT in the top-level alternation;
#   they only match when preceded by employment-context phrases.
_COMP_ARRANGEMENT_PATTERNS = re.compile(
    r"(expected\s+(compensation|pay|salary|rate|package)"
    r"|what\s+is\s+your\s+(expected|current)\s+(comp|salary|pay|rate)"
    r"|compensation\s+expectation"
    r"|your\s+(expected|desired|target)\s+(comp|salary|pay|rate|package)"
    r"|(salary|pay|hourly\s+rate|rate)\s+expectation"    # "salary expectations", "pay expectation"
    r"|(salary|pay|hourly)\s+rate"                        # "pay rate", "hourly rate"
    r"|expecting\s+.{0,20}(compensation|pay|salary|rate)" # "expecting for this role"
    r"|hourly\s+rate\s+.{0,15}(target|expect|look)"       # "hourly rate are you targeting"
    r"|work(ing)?\s+arrangement|job\s+type"
    r"|employment\s+(type|arrangement)"                    # "employment arrangement" + "employment type"
    r"|open\s+to\s+work(ing)?\s+(?:on|under|with|as|for)\s+(?:[\w,-]+\s+){0,4}(?:on|under|with|as|for)?\s*(?:a\s+)?(w-?2|c2c|corp|1099|subcontract)" # e.g. "open to work with the client as a w2"
    r"|w-?2\s+(agreement|employee|employment|position|role|basis|arrangement)"
    r"|\bc2c\b"
    r"|corp(-|\s+)to(-|\s+)corp\s+(arrangement|basis|role|position|employee|contractor)"
    r"|eligible\s+(for|to\s+work).{0,15}(w-?2|c2c)"       # "eligible for W-2 employment"
    r"|which\s+types?\s+of\s+working\s+arrangement"
    r"|select\s+all\s+that\s+apply.*(w-?2|c2c|subcontract|independent\s+contractor)"
    r"|as\s+a\s+subcontract(or)?\s+(through|via|with))",   # "as a subcontractor through your employer"
    flags=re.IGNORECASE,
)


def _is_compensation_or_work_arrangement_question(text: str) -> bool:
    """True if the question text is identifiable as a compensation or
    work-arrangement / job-type front-matter question (Q5 or Q6).

    Used to auto-promote these to hard filters when the recruiter has
    configured an explicit pass criterion. Intentionally anchored to
    employment-context phrases to avoid false-positive matches on
    role-specific questions (e.g. 'Have you managed subcontractors?').
    """
    return bool(_COMP_ARRANGEMENT_PATTERNS.search(text or ""))


def _sanitize_pre_screen_questions_for_pair(
    questions: List[Dict[str, Any]],
    *,
    fallback_job_title: str = "the role",
    boolean_mode: bool = False,
) -> List[Dict[str, Any]]:
    """Normalize pre-screen questions to PAIR schema constraints."""
    sanitized: List[Dict[str, Any]] = []
    for q in questions:
        text = " ".join(str(q.get("question_text") or "").split()).strip()
        if not text:
            continue

        if len(text) < 10:
            text = f"Please explain: {text}"
        if len(text) > 1000:
            text = text[:1000].rstrip()

        pass_criteria = str(q.get("pass_criteria") or "").strip()
        if len(pass_criteria) > 500:
            pass_criteria = pass_criteria[:500].rstrip()

        category = str(q.get("category") or "default").strip() or "default"
        if len(category) > 50:
            category = category[:50].rstrip()

        is_hard_filter_in_db = bool(q.get("is_hard_filter", False))
        question_type = str(q.get("question_type") or "").strip().lower()
        has_pass_criteria = bool(pass_criteria)
        is_front_matter = bool(q.get("is_default")) or category.lower() in ("default", "logistics", "work-arrangement")

        q_lower = text.lower()
        is_new_opps = _PHRASE_NEW_OPPS in q_lower
        is_onsite_hybrid = any(p in q_lower for p in _PHRASES_ONSITE_HYBRID)

        q_order = int(q.get("order_index", 0) or 0)
        is_role_specific = q_order > 9 or category.lower() not in ("default", "logistics", "work-arrangement")

        # Dynamic Hard Filter Logic
        # - Always-on questions (new-opps, onsite/hybrid) → unconditionally True
        # - Other front-matter questions → True iff recruiter provided pass_criteria
        # - Non-front-matter (role-specific) in boolean mode → honour the DB flag
        # - Non-front-matter in non-boolean mode → these are scored interview questions
        # These safety-critical front-matter questions are always gates; an
        # explicit question type must never downgrade them to informational.
        if is_new_opps or is_onsite_hybrid:
            is_hard_filter = True
            is_info_only = False
        elif question_type == "hard_filter":
            is_hard_filter = True
            is_info_only = False
        elif question_type == "info_only":
            is_hard_filter = False
            is_info_only = True
        elif question_type == "scored":
            is_hard_filter = False
            is_info_only = False
        elif is_front_matter:
            is_hard_filter = has_pass_criteria
            is_info_only = not is_hard_filter
        elif boolean_mode:
            # Role-specific boolean questions keep their pre-calculated hard filter flag
            is_hard_filter = is_hard_filter_in_db
            is_info_only = not is_hard_filter
        else:
            is_hard_filter = False
            is_info_only = False

        sanitized.append({
            "question_text": text,
            "pass_criteria": pass_criteria,
            "is_default": bool(q.get("is_default", True)),
            "category": category,
            "is_hard_filter": is_hard_filter,
            "is_info_only": is_info_only,
            "question_type": question_type or None,
        })

    return sanitized


def _is_yes_no_question(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized.startswith(("are ", "do ", "does ", "did ", "have ", "has ", "is ", "can ", "will ", "would "))


# ---------------------------------------------------------------------------
# Always-on hard-filter detection phrases
# These phrases identify questions that are unconditionally knockout criteria
# regardless of whether the recruiter supplies a pass_criteria value.
# Centralised here so that any wording change to the question templates only
# needs to be updated in one place (backend + shared with frontend via docs).
# ---------------------------------------------------------------------------
_PHRASE_NEW_OPPS = "exploring new job opportunities"
_PHRASES_ONSITE_HYBRID = (
    "follows an onsite",
    "hybrid work arrangement",
    "onsite work arrangement",
)

_ROLE_RESPONSIBILITIES_MATCH_FRAGMENT = "current or most recent role"


def _is_role_responsibilities_question(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return _ROLE_RESPONSIBILITIES_MATCH_FRAGMENT in normalized


def _to_boolean_question_text(text: str) -> str:
    raw = " ".join(str(text or "").split()).strip()
    if not raw:
        return "Do you have hands-on experience relevant to this role?"

    lower = raw.lower()
    # Keep already-boolean wording intact.
    if _is_yes_no_question(lower):
        return raw if raw.endswith("?") else f"{raw}?"

    # Deterministic rewrite for known front-matter regular prompts.
    if "current location" in lower:
        return "Are you currently based in the required job region for this role?"
    if "earliest availability" in lower or "availability to start" in lower:
        return "Are you available to start within the required timeline for this role?"
    if "expected compensation" in lower or "salary" in lower or "pay" in lower:
        return "Are your compensation expectations aligned with this role?"
    if "working arrangements" in lower and "w2" in lower:
        return "Are you eligible to work in at least one of these arrangements: W2, Subcontractor, or Independent Contractor?"
    if "authorized to work" in lower:
        return "Are you authorized to work indefinitely for any employer in the United States?"
    if "visa sponsorship" in lower:
        return "Will you now or in the future require visa sponsorship to continue working in the United States?"

    # Role-specific fallback: map open-ended prompts to a yes/no skill check.
    skill_match = re.search(r"\bwith\s+([^?.]+)", raw, flags=re.IGNORECASE)
    if skill_match:
        skill = skill_match.group(1).strip(" .,:;")
        if skill:
            return f"Do you have hands-on experience with {skill}?"

    return "Do you have hands-on experience relevant to this role?"


def _enforce_boolean_pre_screen_questions(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rewritten: List[Dict[str, Any]] = []
    for q in questions or []:
        if not isinstance(q, dict):
            continue

        q_text_raw = str(q.get("question_text") or "")
        if _is_role_responsibilities_question(q_text_raw):
            # L0.5 boolean interviews must not ask this open-ended prompt.
            continue
            
        category = str(q.get("category") or "").strip().lower()
        # work-arrangement is already a yes/no hard filter — always preserve.
        if category in ("default", "logistics", "work-arrangement"):
            rewritten.append(q)
            continue
            
        qt = _to_boolean_question_text(q_text_raw)
        pc = str(q.get("pass_criteria") or "").strip()
        if not pc:
            pc = "Pass when candidate confirms relevant experience or eligibility."
        rewritten.append({
            **q,
            "question_text": qt,
            "pass_criteria": pc,
            "is_hard_filter": True,
        })
    return rewritten


def _extract_pair_error_message(response_data: Dict[str, Any], status_code: int) -> str:
    """Return the most useful PAIR error text for UI surfacing."""
    if not isinstance(response_data, dict):
        return f"PAIR API returned status {status_code}"

    message = str(response_data.get("message") or "").strip()
    if message:
        return message

    detail = response_data.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()

    if isinstance(detail, list) and detail:
        parts: List[str] = []
        for item in detail[:3]:
            if isinstance(item, dict):
                loc = item.get("loc")
                where = ".".join(str(p) for p in loc if p != "body") if isinstance(loc, list) else ""
                msg = str(item.get("msg") or "validation error").strip()
                parts.append(f"{where}: {msg}" if where else msg)
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        if parts:
            return "; ".join(parts)

    return f"PAIR API returned status {status_code}"

# ---------------------------------------------------------------------------
# Auto-Migration: Ensure audit table exists
# ---------------------------------------------------------------------------
def _ensure_audit_table():
    """Create engage_interview_audit table if it doesn't exist, and patch any missing columns."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Create table (no-op if already exists)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS engage_interview_audit (
                        id SERIAL PRIMARY KEY,
                        candidate_id VARCHAR(255) NOT NULL,
                        jobdiva_id VARCHAR(255),
                        interview_id VARCHAR(255),
                        candidate_name VARCHAR(255),
                        candidate_email VARCHAR(255),
                        payload JSONB,
                        response JSONB,
                        status VARCHAR(50) DEFAULT 'sent',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Idempotent column adds (ALTER TABLE) removed to prevent
                # lock contention. These should be handled via manual migrations.
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_engage_audit_candidate
                    ON engage_interview_audit(candidate_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_engage_audit_interview
                    ON engage_interview_audit(interview_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_engage_audit_job_candidate_id_desc
                    ON engage_interview_audit(jobdiva_id, candidate_id, id DESC);
                """)
                conn.commit()
        logger.info("✅ engage_interview_audit table ready")
    except Exception as e:
        logger.error(f"❌ Failed to create engage_interview_audit table: {e}")

# NOTE: _ensure_audit_table used to run at module import. That meant any
# DB slowness or lock blocked `from routers import engagement, ai_generation, …`
# in main.py — which in turn prevented every other router in that import
# statement from registering, producing 404s across the API. The call has
# been moved to `init_engagement_tables` which main.py awaits from lifespan
# with a timeout.

async def init_engagement_tables() -> None:
    """Async wrapper for the sync migration. Called from main.py lifespan."""
    await asyncio.to_thread(_ensure_audit_table)
    from services import call_recordings
    call_recordings.warn_if_misconfigured()

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _get_db_connection():
    return get_db_connection()

# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------
class GeneratePayloadRequest(BaseModel):
    candidate_ids: List[str]
    job_id: str

class SendBulkInterviewRequest(BaseModel):
    payload: str  # JSON string (editable by user in modal)
    real_candidate_ids: List[str]
    is_initial_launch: bool = False
    dry_run: bool = False
    # Wizard "Launch PAIR" sends this to trigger the recruiter launch email.
    # Manual rankings Engage clicks leave it false so we don't spam emails.
    # Previously, dry_run did double duty (skip pairbot + fire email), which
    # silently caused bulk launches to never reach pairbot.
    notify_recruiters: bool = False
    # When provided by the caller, controls Email #2 (job posting request)
    # explicitly so batched launches can fire it once on the final successful
    # batch only.
    send_job_posting_email: Optional[bool] = None
    app_base_url: str = ""
    # Recruiter warm handoff (Pair-bot docs/recruiter-handoff-pair-contract.md):
    # after its last question the bot holds the call for the launch's
    # recruiter (the server-resolved recruiter_email) instead of signing off.
    recruiter_handoff: bool = False
    handoff_timeout_sec: Optional[int] = Field(default=None, ge=60, le=1800)


# ---------------------------------------------------------------------------
# 1. POST /engage/generate-payload
# ---------------------------------------------------------------------------
@router.post("/engage/generate-payload")
async def generate_engage_payload(request: GeneratePayloadRequest):
    """Thin HTTP wrapper. The QA/edit modal fetches an editable payload here;
    the batched launch orchestrator (`/engage/launch`) calls
    `_generate_payload_for` directly, with no browser round-trip."""
    return await _generate_payload_for(request)


_LIVE_SOURCED_ROWS_SQL = """
    SELECT DISTINCT ON (candidate_id)
        candidate_id, name, email, phone, headline, location, data{extra}
    FROM sourced_candidates
    WHERE candidate_id = ANY(%s)
      AND dnc_stopped_at IS NULL
    ORDER BY candidate_id, updated_at DESC
"""


def _fetch_live_sourced_rows(cur, candidate_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Newest non-DNC sourced_candidates row per id, in one round-trip.

    Keeps the schema-drift fallback: some environments still lack
    `resume_match_percentage`, so retry without it after a rollback.
    """
    ids = [c for c in dict.fromkeys(candidate_ids) if c]
    if not ids:
        return {}
    try:
        cur.execute(_LIVE_SOURCED_ROWS_SQL.format(extra=", resume_match_percentage"), (ids,))
        rows = cur.fetchall()
    except Exception:
        cur.connection.rollback()
        cur.execute(_LIVE_SOURCED_ROWS_SQL.format(extra=""), (ids,))
        rows = cur.fetchall()
    return {r["candidate_id"]: r for r in rows}


async def _generate_payload_for(request: GeneratePayloadRequest):
    """
    Generate an interview payload for a candidate.
    Fetches candidate data from sourced_candidates and job data from monitored_jobs,
    then assembles it into the samplepayload.json structure.
    """
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # ----- Fetch candidate data -----
        # DNC gate: rows with dnc_stopped_at set are excluded so a candidate
        # added to the Do-Not-Contact list after launch stops receiving
        # interview invites without inactivating the rest of the PAIR.
        resumes = []
        candidate_phone = ""
        dnc_blocked_ids: List[str] = []
        # Fix 6: one ANY() query instead of one query per candidate (N+1).
        # DISTINCT ON keeps the newest live row per candidate; output order is
        # re-imposed from request.candidate_ids below because positional
        # matching downstream (launch retry set) depends on it.
        rows_by_cid = _fetch_live_sourced_rows(cur, list(request.candidate_ids))
        missing_ids = [cid for cid in dict.fromkeys(request.candidate_ids) if cid not in rows_by_cid]
        dnc_set: set = set()
        if missing_ids:
            # Either the candidate isn't sourced for any job, or every row is
            # dnc_stopped. One probe tells them apart for all misses at once.
            cur.execute("""
                SELECT DISTINCT candidate_id FROM sourced_candidates
                WHERE candidate_id = ANY(%s) AND dnc_stopped_at IS NOT NULL
            """, (missing_ids,))
            dnc_set = {r["candidate_id"] for r in cur.fetchall()}
        for cid in request.candidate_ids:
            row = rows_by_cid.get(cid)
            if not row and cid in dnc_set:
                dnc_blocked_ids.append(cid)
                logger.info(
                    "engagement_dnc_skip candidate_id=%s reason=dnc_stopped_at_set",
                    cid,
                )
                continue

            if row:
                name = row.get("name", "Unknown")
                parts = name.split(" ", 1)
                first_name = parts[0] if parts else name
                last_name = parts[1] if len(parts) > 1 else ""
                # Sanitize phone: keep only digits + leading '+'. Pairbot enforces
                # max_length=20 on phone; values like 'Available upon request'
                # stored in sourced_candidates cause 422 validation errors.
                raw_phone = row.get("phone", "") or ""
                plus = "+" if raw_phone.strip().startswith("+") else ""
                digits = "".join(ch for ch in raw_phone if ch.isdigit())
                phone = f"{plus}{digits}" if digits else ""
                email = row.get("email", "") or ""

                if not candidate_phone and phone:
                    candidate_phone = phone

                # Extract headline/summary from data blob if available
                data_blob = row.get("data") or {}
                if isinstance(data_blob, str):
                    try:
                        data_blob = json.loads(data_blob)
                    except Exception:
                        data_blob = {}
                headline = row.get("headline") or data_blob.get("headline", "")
                match_score = row.get("resume_match_percentage")
                if match_score is None:
                    match_score = data_blob.get("match_score")
                if match_score is not None:
                    try:
                        match_score = float(match_score)
                    except Exception:
                        pass

                resumes.append({
                    "source_candidate_id": row.get("candidate_id") or cid,
                    "name": name,
                    "email": email,
                    "phone": phone,  # already sanitized to digits only above
                    "gender_label": (
                        row.get("gender_label")
                        or data_blob.get("gender_label")
                        or "default"
                    ),
                    "gender_confidence": (
                        row.get("gender_confidence")
                        if row.get("gender_confidence") is not None
                        else data_blob.get("gender_confidence", 0.0)
                    ),
                    "gender_source": (
                        row.get("gender_source")
                        or data_blob.get("gender_source")
                        or "unknown"
                    ),
                    "gender_updated_at": (
                        row.get("gender_updated_at")
                        or data_blob.get("gender_updated_at")
                    ),
                    # NOTE: résumé/experience/summary/skills/education are intentionally
                    # NOT built here — the emitted payload uses `final_resumes` below,
                    # which carries only unique per-candidate identity + scores. Pairbot
                    # does not receive résumé content, so fetching it was dead work.
                    "match_score": match_score,
                    "resume_screening_score": match_score,
                })
            else:
                # Fallback for candidates not found in DB
                resumes.append({
                    "source_candidate_id": cid,
                    "name": "Unknown Candidate",
                    "email": "",
                    "phone": "",
                })

        # ----- Fetch job data -----
        # Some jobs have duplicate rows where job_id matches jobdiva_id.
        # We prioritize the row with a numeric job_id and the latest creation date.
        cur.execute("""
            SELECT * FROM monitored_jobs 
            WHERE job_id = %s OR jobdiva_id = %s
            ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC 
            LIMIT 1
        """, (request.job_id, request.job_id))
        job_row = cur.fetchone()

        # ----- Fetch pre-screen questions from job_screen_questions table -----
        # Match by job_id first, then fall back to jobdiva_id if needed
        pre_screen_questions = []
        if job_row:
            jobdiva_id_for_lookup = job_row.get("jobdiva_id") or ""
            job_id_for_lookup = job_row.get("job_id") or request.job_id
            cur.execute("""
                SELECT question_text, pass_criteria, is_default, category, order_index, is_hard_filter, question_type
                FROM job_screen_questions
                WHERE jobdiva_id = %s OR jobdiva_id = %s
                ORDER BY order_index
            """, (jobdiva_id_for_lookup, job_id_for_lookup))
            rows = cur.fetchall()
            from routers.voice_agent import _humanize_question_text
            pre_screen_questions_raw = [
                {
                    "question_text": _humanize_question_text(r["question_text"]),
                    "pass_criteria": r["pass_criteria"],
                    "is_default": r["is_default"],
                    "category": r["category"],
                    "order_index": r["order_index"],
                    "is_hard_filter": r.get("is_hard_filter", False),
                    "question_type": r.get("question_type"),
                }
                for r in rows
            ]
            is_l05 = str(job_row.get("screening_level") or "").strip().lower() == "l0.5"
            if is_l05:
                pre_screen_questions_raw = _enforce_boolean_pre_screen_questions(pre_screen_questions_raw)
            pre_screen_questions = _sanitize_pre_screen_questions_for_pair(
                pre_screen_questions_raw,
                fallback_job_title=(job_row.get("enhanced_title") or job_row.get("title") or request.job_id),
                boolean_mode=is_l05,
            )
            # Employer backstop (2026-09-02): every interview asks where the
            # candidate works NOW. The answer returns on the PairBot webhook
            # (routers/voice_agent.py), persists as stated_current_employer,
            # and re-runs the no-contact + hiring-client checks — the last
            # line of defense for candidates launched employer-unverified.
            # Appended post-sanitize in the PAIR schema shape; category
            # "logistics" so L0.5 boolean rewriting preserves it verbatim.
            try:
                from services.stated_employer import append_employer_question
                pre_screen_questions = append_employer_question(pre_screen_questions)
            except Exception as _q_err:  # noqa: BLE001
                logger.warning(f"employer question injection skipped: {_q_err}")

        cur.close()
        conn.close()

        # Build JD section — must match pairbotqa expected structure:
        # ----- Fetch full rubric for JD enrichment -----
        from services.job_rubric_db import JobRubricDB
        rubric_db = JobRubricDB()
        jobdiva_id_for_rubric = job_row.get("jobdiva_id") if job_row else request.job_id
        rubric = rubric_db.get_full_rubric(jobdiva_id_for_rubric)
        if rubric:
            rubric.pop("screen_questions", None)
            rubric.pop("soft_skills", None)
            rubric.pop("bot_introduction", None)

        # Build JD block with structured context and rubric
        if job_row:
            # De-dup: title/customer_name/city/state/location_type live ONLY in
            # `context` (matches samplepayload.json). They were previously also
            # copied to the jd top level — redundant payload. Pairbot reads them
            # from context.
            jd = {
                "job_id": job_row.get("job_id") or request.job_id,
                "jobdiva_id": job_row.get("jobdiva_id") or "",
                "campaign_id": job_row.get("campaign_id") or "0",
                "context": {
                    "title": job_row.get("enhanced_title") or job_row.get("title", ""),
                    "customer_name": job_row.get("customer_name") or "Unknown",
                    "city": job_row.get("city") or "TBD",
                    "state": job_row.get("state") or "",
                    "location_type": job_row.get("location_type") or "Onsite",
                    "jobdiva_description": job_row.get("jobdiva_description") or "",
                    "ai_description": job_row.get("ai_description") or "",
                    "recruiter_notes": job_row.get("recruiter_notes") or "",
                    "is_l05": is_l05,
                    "screening_level": job_row.get("screening_level") or DEFAULT_SCREENING_LEVEL,
                },
                "rubric": rubric if rubric else {},
                "pre_screen_questions": pre_screen_questions,
            }
        else:
            jd = {
                "job_id": request.job_id,
                "jobdiva_id": "",
                "context": {},
                "rubric": {},
                "pre_screen_questions": _sanitize_pre_screen_questions_for_pair(
                    [],
                    fallback_job_title=request.job_id,
                )
            }

        # Resolve gender labels for the whole batch concurrently. Each
        # infer_gender_from_name_ai call is cached and globally
        # semaphore-bounded, so gathering avoids serializing the launch batch
        # on N sequential OpenAI round-trips (the old per-resume `await` here).
        async def _resolve_gender(r):
            gender = normalize_gender_prediction(
                predicted_label=r.get("gender_label", "default"),
                confidence=r.get("gender_confidence", 0.0),
                source=r.get("gender_source", "unknown"),
                threshold=0.0,
                updated_at=r.get("gender_updated_at"),
            )
            candidate_name = r.get("name") or "Unknown"
            if gender.gender_label == "default" and candidate_name:
                ai_gender = await infer_gender_from_name_ai(candidate_name)
                if ai_gender.gender_label in {"male", "female"}:
                    return ai_gender
            return gender

        resolved_genders = await asyncio.gather(*[_resolve_gender(r) for r in resumes])

        # Build resumes list using raw_resume_text
        final_resumes = []
        for r, gender in zip(resumes, resolved_genders):
            candidate_name = r.get("name") or "Unknown"
            candidate_email = str(r.get("email") or "").strip().lower()
            final_resumes.append({
                "source_candidate_id": r.get("source_candidate_id"),
                "name": candidate_name,
                "email": candidate_email,
                "phone": r.get("phone"),
                "gender_label": gender.gender_label,
                "match_score": r.get("match_score"),
                "resume_screening_score": r.get("resume_screening_score"),
            })

        raw_company_intro = (job_row.get("bot_introduction") or "") if job_row else ""
        if job_row:
            def _clean_job_title_for_intro(title: str) -> str:
                if not title:
                    return "role"
                import re
                cleaned = re.sub(r'^(?:US|USA|CAN|CANADA|UK|INDIA|MEX|APAC|EMEA|LATAM|[A-Z]{2,3}(?:\/[A-Z]{2,3})?)\s*[-:/|]\s*', '', str(title), flags=re.IGNORECASE).strip()
                cleaned = re.sub(r'\b(?:remote|onsite|on-site|hybrid|wfh)\b', '', cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(" -:/|")
                return cleaned or "role"

            def _clean_location_for_intro(loc: str) -> str:
                if not loc:
                    return "your area"
                import re
                cleaned = re.sub(r'\b(?:remote|onsite|on-site|hybrid|wfh)\b', '', str(loc), flags=re.IGNORECASE)
                cleaned = re.sub(r'\(\s*\)', '', cleaned)
                cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(" ,/|")
                return cleaned or "your area"

            raw_title_str = (job_row.get("enhanced_title") or job_row.get("title") or "role").strip()
            job_t = _clean_job_title_for_intro(raw_title_str)
            raw_loc_str = f"{job_row.get('city') or ''}, {job_row.get('state') or ''}".strip(", ") or "your area"
            job_l = _clean_location_for_intro(raw_loc_str)
            article = get_article_for_title(job_t)
            if not raw_company_intro.strip() or is_legacy_default_bot_intro(raw_company_intro):
                raw_company_intro = (
                    f"Hi {{{{candidate name}}}}, I'm Alex, a virtual recruiter with Pyramid Consulting. "
                    f"We are helping our client recruit for {article} {job_t} in {job_l}, and you have been shortlisted for this role. "
                    f"Please note that this conversation may be recorded for verification and quality purposes. "
                    f"Are you available for a quick 3-5 mins conversation to increase the possibilities of getting hired?"
                )
            else:
                raw_company_intro = normalize_bot_intro_tokens(raw_company_intro, job_t, job_l)

        # Assemble final payload matching pairbotqa /api/bulk-interviews schema
        payload = {
            "resumes": final_resumes,
            "jd": jd,
            "company_intro": raw_company_intro,
            "interview_duration": "20-25",
            "source": "pair"
        }

        payload_str = json.dumps(payload, indent=2)

        response_body = {
            "success": True,
            "payload": payload_str,
            "candidate_count": len(resumes),
            "dnc_blocked_count": len(dnc_blocked_ids),
            "dnc_blocked_ids": dnc_blocked_ids,
        }
        _log_generate_payload_response_to_newrelic(response_body, level="info")
        return response_body

    except Exception as e:
        _log_generate_payload_response_to_newrelic(
            {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
                "candidate_ids": request.candidate_ids,
                "job_id": request.job_id,
            },
            level="error",
        )
        logger.error(f"❌ generate-payload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------------------------
# Helper: fire PAIR launch notifications (background task)
# Fires Email #1 (launch confirmation) + Email #2 (job posting request)
# from a single DB query so we don't hit monitored_jobs twice.
# ---------------------------------------------------------------------------
async def _send_pair_launch_email(
    *, job_id: str, candidate_count: int, send_job_posting: bool = True, app_base_url: str = "",
    relaunch: bool = False,
) -> None:
    """
    Fetches job metadata from monitored_jobs and fires launch emails.

    Email #1 (PAIR launch confirmation to recruiters) always fires.
    Email #2 (job posting team request) is gated on `send_job_posting` —
    callers pass False on re-launches so we don't re-spam the posting team
    every time a recruiter sources another batch of candidates.
    ``relaunch`` (the job's first interview predates this launch) words Email
    #1 as "more candidates launched" instead of a new campaign.

    Runs inside asyncio.create_task() so failures are fully isolated.
    """
    if not job_id:
        return
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT job_id, jobdiva_id, title, enhanced_title, customer_name,
                   city, state, location_type, bot_introduction,
                   jobdiva_description, ai_description, recruiter_notes,
                   selected_job_boards, recruiter_emails
            FROM monitored_jobs
            WHERE job_id = %s OR jobdiva_id = %s
            ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
            LIMIT 1
        """, (job_id, job_id))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            logger.warning("📧 _send_pair_launch_email: job '%s' not found in monitored_jobs", job_id)
            return

        recruiter_emails: list = _parse_json_list(row.get("recruiter_emails", []))
        job_boards: list       = _parse_json_list(row.get("selected_job_boards", []))

        jobdiva_id    = str(row.get("jobdiva_id") or "")
        job_title      = row.get("enhanced_title") or row.get("title", "")
        customer_name  = row.get("customer_name", "Unknown")
        location       = f"{row.get('city', 'TBD')}, {row.get('state', '')}"
        db_job_id     = str(row.get("job_id") or job_id)
        clean_emails  = [str(e) for e in recruiter_emails if e]
        ai_desc       = row.get("ai_description") or ""

        # ── Email #1: PAIR Launch Confirmation ───────────────────────────────
        # Isolated in its own try: a failure here must not also skip the
        # in-app notification below — they're independent channels, and the
        # in-app one no longer needs the email to have succeeded first (dedup
        # is now a DB-level partial index, not call-order).
        try:
            await asyncio.to_thread(
                notify_pair_launched,
                jobdiva_id=jobdiva_id,
                job_title=job_title,
                customer_name=customer_name,
                candidate_count=candidate_count,
                recruiter_emails=clean_emails,
                job_id=db_job_id,
                app_base_url=app_base_url,
                relaunch=relaunch,
            )
        except Exception as email_err:
            logger.warning(
                "📧 PAIR launch email failed for job %s (in-app notification still proceeds): %s",
                db_job_id, email_err, exc_info=True,
            )

        # In-app companion to Email #1 — same recipients, same trigger point,
        # job-level (see services/notifications_service.py).
        await create_pair_launched_notification(
            job_id=db_job_id,
            jobdiva_id=jobdiva_id,
            job_title=job_title,
            candidate_count=candidate_count,
            recruiter_emails=clean_emails,
        )

        # ── Email #2: Job Posting Request (skipped on re-launch or no boards) ──
        if send_job_posting and job_boards:
            await asyncio.to_thread(
                notify_job_posting,
                jobdiva_id=jobdiva_id,
                job_title=job_title,
                recruiter_emails=clean_emails,
                job_boards=job_boards,
                ai_description=ai_desc,
                job_id=db_job_id,
                app_base_url=app_base_url,
            )
        else:
            logger.info(
                "📧 Skipping job-posting email for job %s (not requested or no job boards selected)",
                jobdiva_id or job_id,
            )

    except Exception as exc:
        logger.warning("📧 _send_pair_launch_email failed silently: %s", exc, exc_info=True)


async def _post_to_pairbot(
    url: str,
    payload_obj: Dict[str, Any],
    *,
    max_attempts: int = 3,
    base_backoff_seconds: float = 2.0,
    timeout_seconds: float = 60.0,
) -> httpx.Response:
    """POST to pairbot with retries on network error / 5xx.

    PAI-155: Pairbot bulk-create now returns 202 quickly; do NOT retry on
    TimeoutException (a timed-out create may still be running server-side,
    and retrying would duplicate work / orphan bulk_jds).
    """
    last_exc: Optional[BaseException] = None
    last_response: Optional[httpx.Response] = None
    headers = get_pair_auth_headers(json_content_type=True)
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    url,
                    json=payload_obj,
                    headers=headers,
                )
            if response.status_code < 500:
                return response
            last_response = response
            logger.warning(
                "pairbot_5xx attempt=%d/%d status=%d",
                attempt + 1, max_attempts, response.status_code,
            )
        except httpx.TimeoutException as exc:
            # Do not retry timeouts — Pairbot may still be creating.
            logger.warning(
                "pairbot_timeout attempt=%d/%d msg=%s (no retry)",
                attempt + 1, max_attempts, str(exc),
            )
            raise
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            logger.warning(
                "pairbot_transport_error attempt=%d/%d type=%s msg=%s",
                attempt + 1, max_attempts, type(exc).__name__, str(exc),
            )
        if attempt < max_attempts - 1:
            await asyncio.sleep(base_backoff_seconds * (2 ** attempt))

    if last_response is not None:
        return last_response
    raise last_exc if last_exc is not None else RuntimeError("pairbot call failed")


def _stamp_engage_status_batch(
    candidate_ids: List[str],
    status_value: str,
    *,
    job_id_value: str = "",
    job_id_alt: str = "",
    bulk_id: Optional[str] = None,
) -> None:
    """Batch-write engage_status for many candidates in one connection.

    With ``bulk_id`` the Pairbot bulk the rows were posted in is written as
    data.engage_bulk_id in the SAME statement, so a row can never read
    'processing' without saying which bulk to reconcile it against."""
    if not candidate_ids:
        return
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        if bulk_id:
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = jsonb_set(
                        jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{engage_status}',
                            to_jsonb(%s::text),
                            true
                        ),
                        '{engage_bulk_id}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                """,
                (
                    status_value,
                    str(bulk_id),
                    list(candidate_ids),
                    str(job_id_value or ""),
                    str(job_id_alt or ""),
                ),
            )
        else:
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = jsonb_set(
                        COALESCE(data, '{}'::jsonb),
                        '{engage_status}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                """,
                (
                    status_value,
                    list(candidate_ids),
                    str(job_id_value or ""),
                    str(job_id_alt or ""),
                ),
            )
        conn.commit()
        cur.close()
    finally:
        conn.close()


async def _wait_for_pairbot_creation(
    bulk_id: str,
    *,
    timeout_seconds: float = 600.0,
) -> Dict[str, Any]:
    """PAI-155: wait on Pairbot SSE until interviews are created.

    Pairbot returns 202 + bulk_id immediately; interview rows arrive later via
    status=creation_completed (with data[]). Outreach progress uses the same
    stream but Curate only needs creation_completed here.

    Uses a connect timeout plus a background pump into a queue. Waiting on
    the queue (not aiter.__anext__) keeps the SSE generator alive across idle
    gaps so a quiet-but-alive stream is not closed before timeout_seconds.
    """
    stream_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews/{bulk_id}/stream"
    headers: Dict[str, str] = get_pair_auth_headers()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    # read=None: Pairbot may emit nothing until creation_completed; the overall
    # deadline is enforced by wait_for on the queue, not the httpx read timeout.
    client_timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        async with client.stream("GET", stream_url, headers=headers) as response:
            if response.status_code >= 400:
                body = (await response.aread())[:300]
                raise RuntimeError(
                    f"Pairbot stream failed status={response.status_code} body={body!r}"
                )
            chunks: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
            pump_error: List[Exception] = []

            async def _pump_chunks() -> None:
                try:
                    async for chunk in response.aiter_text():
                        await chunks.put(chunk)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    pump_error.append(exc)
                finally:
                    await chunks.put(None)

            pump = asyncio.create_task(_pump_chunks())
            buffer = ""
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out waiting for Pairbot creation_completed bulk_id={bulk_id}"
                        )
                    try:
                        chunk = await asyncio.wait_for(
                            chunks.get(),
                            timeout=min(60.0, remaining),
                        )
                    except asyncio.TimeoutError:
                        # No bytes yet; generator is still alive on the pump.
                        continue
                    if chunk is None:
                        break
                    # SSE allows LF or CRLF frame terminators.
                    buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n\n" in buffer:
                        raw_event, buffer = buffer.split("\n\n", 1)
                        data_lines = [
                            line[5:].strip()
                            for line in raw_event.split("\n")
                            if line.startswith("data:")
                        ]
                        if not data_lines:
                            continue
                        try:
                            event = json.loads("".join(data_lines))
                        except Exception:
                            logger.warning(
                                "pairbot_stream_parse_error bulk_id=%s raw=%r",
                                bulk_id,
                                data_lines,
                            )
                            continue
                        status = str(event.get("status") or "")
                        if status == "creation_completed":
                            return event if isinstance(event, dict) else {"status": status}
                        if status == "creation_failed":
                            raise RuntimeError(
                                event.get("message")
                                or f"Pairbot creation_failed for bulk_id={bulk_id}"
                            )
                        # Ignore accepted/creating/processing/completed (outreach).
            finally:
                if not pump.done():
                    pump.cancel()
                    try:
                        await pump
                    except (asyncio.CancelledError, Exception):
                        pass
            if pump_error:
                raise pump_error[0]
    raise TimeoutError(f"Pairbot stream ended without creation_completed bulk_id={bulk_id}")


async def _fetch_pairbot_job_interviews(job_key: str) -> Dict[str, Any]:
    """Pairbot's job-scoped record: ``GET /api/interviews/by-job/{job_key}``.

    Returns its ``data`` block — ``{bulk_in_progress, in_progress_details,
    interviews: [{interview_id, source_candidate_id, jobdiva_id, status, …}]}``
    (newest interview first). Pairbot matches the key against both the
    jobdiva ref and the job id of the bulk / interview. Raises on a non-200
    or a transport error: a failed check must never read as "nothing exists".
    """
    headers = get_pair_auth_headers()
    url = f"{EXTERNAL_INTERVIEW_API_URL}/api/interviews/by-job/{job_key}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Pairbot by-job check failed status={resp.status_code}")
    body = resp.json()
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def _pairbot_interview_index(
    payload: Dict[str, Any],
) -> Tuple[Dict[str, str], set, Dict[str, str]]:
    """(source_candidate_id -> newest interview id, every listed
    source_candidate_id, every listed interview id -> the source_candidate_id
    it is listed under ('' when none)) from a by-job payload."""
    by_source: Dict[str, str] = {}
    listed_sources: set = set()
    listed_interviews: Dict[str, str] = {}
    interviews = payload.get("interviews") if isinstance(payload, dict) else None
    for iv in interviews if isinstance(interviews, list) else []:
        if not isinstance(iv, dict):
            continue
        src = str(iv.get("source_candidate_id") or "").strip()
        iid = str(iv.get("interview_id") or "").strip()
        if iid:
            listed_interviews.setdefault(iid, src)
        if not src:
            continue
        listed_sources.add(src)
        # Pairbot lists newest first; the first id seen per candidate wins.
        if iid and src not in by_source:
            by_source[src] = iid
    return by_source, listed_sources, listed_interviews


def _job_key_list(job_keys: Any) -> List[str]:
    """Distinct non-empty job keys, order kept. A bare string is one key."""
    if isinstance(job_keys, str):
        job_keys = (job_keys,)
    out: List[str] = []
    for key in job_keys or ():
        k = str(key or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def _stamp_first_launch(job_key: str) -> None:
    """T2 (first interview created) on monitored_jobs via
    services.launch_timing.stamp_first_launch — COALESCE there, so calling it
    for every batch that created an interview is idempotent. Fail-open, and a
    no-op until that module exists."""
    if not job_key:
        return
    try:
        from services.launch_timing import stamp_first_launch
    except ImportError:
        return
    try:
        stamp_first_launch(str(job_key))
    except Exception as exc:  # noqa: BLE001 — timing must never fail a launch
        logger.warning("launch_timing.stamp_first_launch failed job=%s: %s", job_key, exc)


def _record_reconciled_interviews(job_keys: Sequence[str], found: Dict[str, str]) -> List[str]:
    """Write interviews Pairbot's by-job record proves exist for THIS job.

    ``job_keys`` are every spelling sourced_candidates.jobdiva_id may hold for
    the job — ``(job_id, jobdiva_ref)``; the audit row goes under the LAST
    non-empty key (the ref, as the launch core writes it). Per candidate:
    engage_status 'sent' + engage_interview_id (+ engage_updated_at, and any
    engage_block_reason dropped) on rows that have no interview id yet and are
    still in a launch state (''/failed/queued/processing/sending) — a row the
    voice webhook already moved on (in_progress, passed, an interview Fail) is
    never pulled back to 'sent'. The audit row is inserted only when none
    exists for (candidate_id, job, interview_id); a transaction-scoped
    advisory lock on (candidate_id, interview_id) makes that check-then-insert
    exclusive, so two concurrent reconciles cannot both insert. Candidates are
    written in id order (no lock-order deadlock between two reconciles). One
    transaction: it raises and commits nothing on a DB error. Returns the
    candidate ids for which anything was written.
    """
    keys = _job_key_list(job_keys)
    if not keys or not found:
        return []
    audit_job = keys[-1]
    now_iso = datetime.now(timezone.utc).isoformat()
    written: List[str] = []
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        for cid, interview_id in sorted(found.items(), key=lambda kv: str(kv[0])):
            cid, interview_id = str(cid), str(interview_id or "").strip()
            if not cid or not interview_id:
                continue
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = (COALESCE(data, '{}'::jsonb) - 'engage_block_reason')
                           || jsonb_build_object(
                                'engage_status', 'sent',
                                'engage_interview_id', %s::text,
                                'engage_updated_at', %s::text
                           ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = %s
                  AND jobdiva_id = ANY(%s)
                  AND COALESCE(data->>'engage_interview_id', '') = ''
                  AND LOWER(COALESCE(data->>'engage_status', ''))
                      IN ('', 'failed', 'queued', 'processing', 'sending')
                RETURNING candidate_id
                """,
                (interview_id, now_iso, cid, keys),
            )
            updated = bool(cur.fetchall())
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"engage_audit:{cid}:{interview_id}",),
            )
            cur.execute(
                """
                INSERT INTO engage_interview_audit
                    (candidate_id, jobdiva_id, interview_id, response, status)
                SELECT %s, %s, %s, %s::jsonb, 'Initiated'
                WHERE NOT EXISTS (
                    SELECT 1 FROM engage_interview_audit
                    WHERE candidate_id = %s
                      AND jobdiva_id = ANY(%s)
                      AND interview_id = %s
                )
                RETURNING id
                """,
                (
                    cid, audit_job, interview_id,
                    json.dumps({"interview_id": interview_id, "reconciled_from": "pairbot_by_job"}),
                    cid, keys, interview_id,
                ),
            )
            inserted = bool(cur.fetchall())
            if updated or inserted:
                written.append(cid)
        conn.commit()
        cur.close()
    finally:
        conn.close()
    if written:
        logger.info(
            "pairbot_reconcile_recorded job=%s candidates=%d", audit_job, len(written),
        )
        _stamp_first_launch(audit_job)
    return written


async def _confirm_failed_candidates_with_pairbot(
    bulk_id: str,
    job_id: str,
    candidate_ids: List[str],
    *,
    job_keys: Optional[Sequence[str]] = None,
) -> List[str]:
    """After `_wait_for_pairbot_creation` errors out (timeout, dropped SSE
    stream, or any other wait failure), check PairBot's own record before
    concluding a candidate's interview was never created.

    PairBot already accepted the bulk with a 202; our stream dying or our
    600s wait expiring does NOT mean creation failed — PairBot may still be
    working, or may have finished right after we stopped listening. This is
    the only check before stamping `failed`.

    Candidates PairBot DOES list with an interview id are written through as
    `sent` + interview id + audit row (`_record_reconciled_interviews`,
    fail-open) — they used to be discarded and stayed 'processing' forever.
    ``job_keys`` are the job spellings to write under (default ``(job_id,)``).

    Returns only the candidate_ids PairBot itself confirms have no interview,
    and only once PairBot reports it is done processing the bulk. Everything
    else — including a failed check itself — comes back empty, so a real
    success already in progress is never overwritten to `failed`.
    """
    if not job_id or not candidate_ids:
        return []
    try:
        payload = await _fetch_pairbot_job_interviews(job_id)
    except Exception as exc:
        logger.error(
            "pairbot_status_check_after_wait_error_failed bulk_id=%s err=%s", bulk_id, exc
        )
        return []
    by_source, found_ids, _ = _pairbot_interview_index(payload)
    found = {str(c): by_source[str(c)] for c in candidate_ids if by_source.get(str(c))}
    if found:
        try:
            await asyncio.to_thread(_record_reconciled_interviews, job_keys or (job_id,), found)
        except Exception as exc:  # noqa: BLE001 — rows stay 'processing'; reconcile later
            logger.error(
                "pairbot_confirm_found_write_failed bulk_id=%s candidates=%d err=%s",
                bulk_id, len(found), exc,
            )
    if payload.get("bulk_in_progress"):
        # Still creating — nothing is a confirmed failure yet.
        return []
    return [cid for cid in candidate_ids if str(cid) not in found_ids]


async def _fetch_pairbot_bulk_status(bulk_id: str, *, timeout_seconds: float = 15.0) -> str:
    """The durable create status of one Pairbot bulk: the ``status`` of the
    FIRST event of ``GET /api/bulk-interviews/{bulk_id}/stream`` — 'creating'
    while its interviews are still being created, 'creation_completed' /
    'creation_failed' once done (Pairbot keeps that 24 h). A bulk Pairbot has
    no create record for (never accepted, or expired) answers 'completed'
    (the legacy outreach snapshot). Raises on a non-2xx, a transport error or
    no event within the timeout."""
    stream_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews/{bulk_id}/stream"
    headers: Dict[str, str] = get_pair_auth_headers()

    async def _first_event() -> str:
        client_timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            async with client.stream("GET", stream_url, headers=headers) as response:
                if response.status_code >= 400:
                    raise RuntimeError(f"Pairbot bulk stream failed status={response.status_code}")
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n\n" in buffer:
                        raw_event, buffer = buffer.split("\n\n", 1)
                        data_lines = [
                            line[5:].strip() for line in raw_event.split("\n") if line.startswith("data:")
                        ]
                        if not data_lines:
                            continue
                        event = json.loads("".join(data_lines))
                        if isinstance(event, dict):
                            return str(event.get("status") or "")
        raise RuntimeError(f"Pairbot bulk stream ended without an event bulk_id={bulk_id}")

    return await asyncio.wait_for(_first_event(), timeout=timeout_seconds + 5.0)


async def reconcile_with_pairbot(
    job_keys: Sequence[str],
    candidate_ids: Sequence[str],
    *,
    session_id: Optional[str] = None,
    existing_interview_ids: Optional[Dict[str, str]] = None,
    own_bulk_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Settle candidates against Pairbot's job-scoped record (by-job).

    One by-job read per distinct job key (``(job_id, jobdiva_ref)``; Pairbot
    matches either). A candidate counts as launched for THIS job only when
    by-job lists an interview under its OWN source_candidate_id; that is
    written through (`_record_reconciled_interviews`: 'sent' + interview id +
    an audit row via INSERT … WHERE NOT EXISTS).

    ``existing_interview_ids`` ({cid: interview id} from a DuplicateInterview
    answer): when by-job lists that interview for this job under ANOTHER
    source id, the candidate is a second PAIR id (a twin) for a person this
    job already interviews. It goes to ``duplicate_of`` and is never written
    as sent — one interview must not count as two launched candidates.

    Returns::

        {"sent": {cid: interview_id},        # proven AND recorded (committed)
         "unrecorded": {cid: interview_id},  # proven; the DB write failed
         "duplicate_of": {cid: {"interview_id", "source_candidate_id"}},
         "missing": [cid],                   # safe to treat as never created
         "bulk_in_progress": bool,           # Pairbot is creating SOME bulk —
         "in_progress_bulk_id": str|None,    #   its check is not job-scoped
         "own_bulks_creating": [bulk_id],    # of own_bulk_ids; a failed read counts
         "error": str|None}

    ``missing`` (by-job answered and lists no interview for them) is filled
    only when re-posting them is safe: every by-job read succeeded AND no
    bulk is being created — or, given ``own_bulk_ids`` (the caller's bulks
    for these candidates), none of those is still creating (each one's
    durable status) and by-job's in-progress bulk is not one of them.
    Pairbot dedupes only within one bulk, so a re-POST while our own bulk is
    still creating calls the person twice. A bulk Pairbot has not registered
    yet (its POST still in flight) does not read as creating: never pass a
    bulk posted moments ago.

    Fail-safe: when any by-job read fails, ``error`` is set and ``missing`` is
    empty — the caller must not re-POST anything on an error. A failed DB
    write sets ``error`` and reports the proven ids under ``unrecorded``.
    """
    result: Dict[str, Any] = {
        "sent": {}, "unrecorded": {}, "duplicate_of": {}, "missing": [],
        "bulk_in_progress": False, "in_progress_bulk_id": None,
        "own_bulks_creating": [], "error": None,
    }
    keys = _job_key_list(job_keys)
    ids = [str(c) for c in dict.fromkeys(candidate_ids or []) if str(c).strip()]
    if not ids:
        return result
    if not keys:
        result["error"] = "no job key to reconcile against"
        return result
    own = [str(b).strip() for b in dict.fromkeys(own_bulk_ids or []) if str(b or "").strip()]

    by_source: Dict[str, str] = {}
    listed_sources: set = set()
    listed_interviews: Dict[str, str] = {}
    fetch_errors: List[str] = []
    async with _trace_span("pairbot_reconcile", n=len(ids)):
        for key in keys:
            try:
                payload = await _fetch_pairbot_job_interviews(key)
            except Exception as exc:  # noqa: BLE001
                fetch_errors.append(f"by-job {type(exc).__name__}: {str(exc)[:160]}")
                continue
            b, srcs, ivs = _pairbot_interview_index(payload)
            for src, iid in b.items():
                by_source.setdefault(src, iid)
            listed_sources |= srcs
            for iid, src in ivs.items():
                listed_interviews.setdefault(iid, src)
            if payload.get("bulk_in_progress"):
                result["bulk_in_progress"] = True
                details = payload.get("in_progress_details")
                if isinstance(details, dict) and details.get("bulk_id") and not result["in_progress_bulk_id"]:
                    result["in_progress_bulk_id"] = str(details["bulk_id"])
        if result["bulk_in_progress"] and own and not fetch_errors:
            for bulk_id in own:
                try:
                    status = await _fetch_pairbot_bulk_status(bulk_id)
                except Exception as exc:  # noqa: BLE001 — unknown reads as still creating
                    logger.warning("pairbot_bulk_status_failed bulk_id=%s err=%s", bulk_id, exc)
                    status = "creating"
                if status == "creating":
                    result["own_bulks_creating"].append(bulk_id)

    sent: Dict[str, str] = {cid: by_source[cid] for cid in ids if by_source.get(cid)}
    duplicate_of: Dict[str, Dict[str, str]] = {}
    for cid, existing in (existing_interview_ids or {}).items():
        cid, existing = str(cid), str(existing or "").strip()
        if cid in ids and cid not in sent and existing and existing in listed_interviews:
            # Listed for this job, and not under cid (cid would be in `sent`).
            duplicate_of[cid] = {
                "interview_id": existing,
                "source_candidate_id": listed_interviews[existing],
            }
    result["duplicate_of"] = duplicate_of

    errors = list(fetch_errors)
    if sent:
        try:
            await asyncio.to_thread(_record_reconciled_interviews, keys, sent)
            result["sent"] = sent
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "pairbot_reconcile_write_failed session=%s candidates=%d err=%s",
                session_id, len(sent), exc,
            )
            errors.append(f"write {type(exc).__name__}: {str(exc)[:160]}")
            result["unrecorded"] = sent
    missing_is_safe = not fetch_errors and (
        not result["bulk_in_progress"]
        or (
            bool(own)
            and not result["own_bulks_creating"]
            and result["in_progress_bulk_id"] not in own
        )
    )
    if missing_is_safe:
        result["missing"] = [
            c for c in ids if c not in sent and c not in duplicate_of and c not in listed_sources
        ]
    if errors:
        result["error"] = "; ".join(errors)
    logger.info(
        "pairbot_reconcile session=%s job=%s asked=%d sent=%d unrecorded=%d twins=%d missing=%d "
        "in_progress=%s own_creating=%d error=%s",
        session_id, keys[-1], len(ids), len(result["sent"]), len(result["unrecorded"]),
        len(duplicate_of), len(result["missing"]), result["bulk_in_progress"],
        len(result["own_bulks_creating"]), bool(result["error"]),
    )
    return result


# --- Settling parked legacy rows -----------------------------------------------
# A launch outside a session can leave a row 'processing' with no recorded
# outcome: Pairbot's 2xx was silent about it (the core parks it instead of
# calling it failed), or the creation wait ended unconfirmed. 'processing' is
# in flight for every launch path — never re-posted — so nothing would ever
# settle it. This sweep (launch-session housekeeping, one worker per tick)
# asks Pairbot's by-job record about rows parked > _LEGACY_SETTLE_AFTER_S:
#   listed under the row's own id   → 'sent' + audit row (reconcile writes it),
#                                     then provisioned to JobDiva
#   listed under another PAIR id    → failed + blocked duplicate_person_this_job
#   missing (and nothing creating)  → still parked; after _LEGACY_SETTLE_GIVE_UP_S
#                                     → 'failed' (retryable): its echo may be
#                                     broken, so one 'missing' is not proof
#   by-job unreadable               → still parked, checked again later
# A row 'sending' > 30 min with no session (the process died mid-POST) is
# settled the same way. Each row is checked at most every
# _LEGACY_SETTLE_EVERY_S across the fleet (conditional claim).
_LEGACY_SETTLE_AFTER_S = 600
_LEGACY_SENDING_AFTER_S = 1800
_LEGACY_SETTLE_EVERY_S = 600
_LEGACY_SETTLE_GIVE_UP_S = 24 * 3600

_LEGACY_PARKED_CLAIM_SQL = """
    WITH due AS (
        SELECT id FROM sourced_candidates
        WHERE COALESCE(data->>'engage_session_id', '') = ''
          AND COALESCE(data->>'engage_interview_id', '') = ''
          AND (
                (LOWER(COALESCE(data->>'engage_status', '')) = 'processing'
                 AND updated_at < now() - make_interval(secs => %(after)s))
             OR (LOWER(COALESCE(data->>'engage_status', '')) = 'sending'
                 AND updated_at < now() - make_interval(secs => %(sending_after)s))
          )
          AND COALESCE(data->>'engage_settle_checked_at', '') < %(checked_before)s
        ORDER BY id
        LIMIT %(limit)s
        FOR UPDATE SKIP LOCKED
    )
    UPDATE sourced_candidates sc
    SET data = COALESCE(sc.data, '{}'::jsonb) || jsonb_build_object('engage_settle_checked_at', %(now)s::text)
    FROM due
    WHERE sc.id = due.id
    RETURNING sc.jobdiva_id, sc.candidate_id, COALESCE(sc.data->>'engage_updated_at', ''), sc.updated_at
"""

_LEGACY_SETTLE_FAIL_SQL = """
    UPDATE sourced_candidates
    SET data = COALESCE(data, '{}'::jsonb) || jsonb_build_object(
            'engage_status', 'failed',
            'engage_updated_at', %(now)s::text,
            'engage_last_response', jsonb_build_object('settled', %(why)s::text))
        || CASE WHEN %(block)s <> '' THEN jsonb_build_object('engage_block_reason', %(block)s::text)
                ELSE '{}'::jsonb END,
        updated_at = CURRENT_TIMESTAMP
    WHERE jobdiva_id = ANY(%(keys)s)
      AND candidate_id = ANY(%(ids)s)
      AND LOWER(COALESCE(data->>'engage_status', '')) IN ('processing', 'sending')
      AND COALESCE(data->>'engage_session_id', '') = ''
      AND COALESCE(data->>'engage_interview_id', '') = ''
    RETURNING candidate_id
"""


def _claim_parked_legacy_rows(limit: int) -> List[Tuple[str, str, str, Any]]:
    now = datetime.now(timezone.utc)
    params = {
        "after": _LEGACY_SETTLE_AFTER_S,
        "sending_after": _LEGACY_SENDING_AFTER_S,
        "checked_before": (now - timedelta(seconds=_LEGACY_SETTLE_EVERY_S)).isoformat(),
        "limit": int(limit),
        "now": now.isoformat(),
    }
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(_LEGACY_PARKED_CLAIM_SQL, params)
        rows = [(str(r[0] or ""), str(r[1] or ""), str(r[2] or ""), r[3]) for r in cur.fetchall() or []]
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return rows


def _legacy_job_keys(job_key: str) -> List[str]:
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT job_id::text, jobdiva_id FROM monitored_jobs WHERE job_id::text = %s OR jobdiva_id = %s LIMIT 1",
            (job_key, job_key),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    return _job_key_list((job_key,) + (tuple(row) if row else ()))


def _fail_parked_legacy_rows(keys: Sequence[str], ids: Sequence[str], why: str, block: str = "") -> List[str]:
    if not ids:
        return []
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(_LEGACY_SETTLE_FAIL_SQL, {
            "keys": list(keys), "ids": list(ids), "why": why, "block": block,
            "now": datetime.now(timezone.utc).isoformat(),
        })
        out = [str(r[0]) for r in cur.fetchall() or []]
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return out


def _parked_age_s(engage_updated_at: str, updated_at: Any) -> Optional[float]:
    ts: Optional[datetime] = None
    try:
        ts = datetime.fromisoformat(engage_updated_at) if engage_updated_at else None
    except ValueError:
        ts = None
    if ts is None and isinstance(updated_at, datetime):
        ts = updated_at
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


async def settle_parked_legacy_rows(limit: int = 200) -> Dict[str, int]:
    """One sweep (see above). Never raises: a failure leaves rows parked."""
    out = {"checked": 0, "sent": 0, "blocked": 0, "failed": 0, "still_parked": 0}
    try:
        rows = await asyncio.to_thread(_claim_parked_legacy_rows, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy_settle claim failed: %s", exc)
        return out
    by_job: Dict[str, List[Tuple[str, str, Any]]] = {}
    for job_key, cid, eua, ua in rows:
        if job_key and cid:
            by_job.setdefault(job_key, []).append((cid, eua, ua))
    for job_key, items in by_job.items():
        ids = list(dict.fromkeys(c for c, _, _ in items))
        out["checked"] += len(ids)
        try:
            keys = await asyncio.to_thread(_legacy_job_keys, job_key)
            res = await reconcile_with_pairbot(keys, ids)
            sent = [str(c) for c in (res.get("sent") or {})]
            out["sent"] += len(sent)
            twins = [str(c) for c in (res.get("duplicate_of") or {})]
            if twins:
                out["blocked"] += len(await asyncio.to_thread(
                    _fail_parked_legacy_rows, keys, twins, "listed_under_another_id",
                    LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB,
                ))
            age = {c: _parked_age_s(eua, ua) for c, eua, ua in items}
            give_up = [
                str(c) for c in res.get("missing") or []
                if (age.get(str(c)) or 0) >= _LEGACY_SETTLE_GIVE_UP_S
            ]
            if give_up:
                out["failed"] += len(await asyncio.to_thread(
                    _fail_parked_legacy_rows, keys, give_up, "not_created_at_pairbot",
                ))
            settled = set(sent) | set(twins) | set(give_up) | {str(c) for c in (res.get("unrecorded") or {})}
            out["still_parked"] += len([c for c in ids if c not in settled])
            if sent and jobdiva_provisioning_enabled():
                try:
                    await provision_sent_candidates(keys[-1], sent)
                except Exception as exc:  # noqa: BLE001 — the rows stay launched
                    logger.warning("legacy_settle provisioning failed job=%s: %s", job_key, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy_settle failed job=%s candidates=%d: %s", job_key, len(ids), exc)
    if out["checked"]:
        logger.info("legacy_settle %s", " ".join(f"{k}={v}" for k, v in out.items()))
    return out


# --- Pairbot DuplicateInterview ----------------------------------------------
# Pairbot answers a duplicate with failed_interviews[] {resume_index,
# error_type: "DuplicateInterview", existing_interview_id}. Its dedupe
# (get_interview_by_candidate_and_role) is scoped to the ONE bulk being created
# (bulk_jd_id; Pairbot origin/main d03be9b): every POST stores a new bulk_jds
# row, and the candidate+role unique index is gone (migration 0007). So Pairbot
# does NOT stop a second bulk from calling the same person again — PAIR's own
# send claim (_SEND_CLAIM_SQL) is what prevents that. Within one bulk a
# duplicate means two PAIR ids for one person (a twin). Older Pairbot deduped
# per person + LOWER(role title) across all jobs, where two jobs titled "Java
# Developer" collided. Either way a duplicate is recorded as launched for this
# job only when by-job lists an interview under the candidate's OWN id;
# otherwise the row is failed + blocked (never re-posted: Pairbot would only
# answer the same duplicate again):
LAUNCH_BLOCK_DUPLICATE_OTHER_JOB = "duplicate_role_other_job"
# by-job lists the existing interview for THIS job under another PAIR id: the
# person is already being interviewed for this job under their other profile.
LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB = "duplicate_person_this_job"
# by-job could not be read (or could not yet rule out a bulk still creating),
# so the duplicate could not be placed. Blocked the same way (fail-safe); a
# later reconcile_with_pairbot heals it to 'sent' if by-job does list it under
# this candidate for this job.
LAUNCH_BLOCK_DUPLICATE_UNVERIFIED = "duplicate_unverified"
_LAUNCH_BLOCK_REASON_TEXT = {
    LAUNCH_BLOCK_DUPLICATE_OTHER_JOB: (
        "already has a PAIR interview for another job with the same title"
    ),
    LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB: (
        "already has a PAIR interview for this job under another profile"
    ),
    LAUNCH_BLOCK_DUPLICATE_UNVERIFIED: (
        "PAIR already has an interview for this person and role that could not be matched to this job"
    ),
}


def _launch_block_reason_text(reason: str) -> str:
    return _LAUNCH_BLOCK_REASON_TEXT.get(reason, f"launch blocked ({reason})")


def _pairbot_rejected_ids(
    failed_interviews: Any,
    payload_obj: Dict[str, Any],
    candidate_ids: Sequence[str],
) -> set:
    """Candidate ids Pairbot's ``failed_interviews`` explicitly names (any
    error type): placed by ``resume_index`` into the posted payload, or by an
    echoed ``source_candidate_id`` / ``candidate_id``. A candidate Pairbot
    answered for is a definite outcome; one it is silent about is not."""
    if not isinstance(failed_interviews, list):
        return set()
    resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
    resumes = resumes if isinstance(resumes, list) else []
    wanted = {str(c) for c in candidate_ids}
    out: set = set()
    for item in failed_interviews:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("source_candidate_id") or item.get("candidate_id") or "").strip()
        raw_idx = item.get("resume_index")
        if not cid and not isinstance(raw_idx, bool):
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(resumes) and isinstance(resumes[idx], dict):
                cid = str(resumes[idx].get("source_candidate_id") or "").strip()
        if cid and cid in wanted:
            out.add(cid)
    return out


def _map_duplicate_interviews(
    failed_interviews: Any,
    payload_obj: Dict[str, Any],
    candidate_ids: Sequence[str],
    created_ids: Optional[set] = None,
) -> Dict[str, Dict[str, Any]]:
    """{candidate_id: Pairbot's failed_interviews item} for DuplicateInterview
    answers (the item carries ``existing_interview_id``). ``resume_index``
    indexes the EXACT payload that was posted, so the candidate is
    ``payload_obj["resumes"][resume_index].source_candidate_id`` — never a
    position in ``candidate_ids`` (the resumes are only re-sliced when the
    lengths line up). Ids outside ``candidate_ids`` or already created in this
    bulk (``created_ids``) are ignored; the first item per candidate wins."""
    resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
    if not isinstance(resumes, list) or not isinstance(failed_interviews, list):
        return {}
    wanted = {str(c) for c in candidate_ids}
    created = created_ids or set()
    out: Dict[str, Dict[str, Any]] = {}
    for item in failed_interviews:
        if not isinstance(item, dict) or item.get("error_type") != "DuplicateInterview":
            continue
        raw_idx = item.get("resume_index")
        if isinstance(raw_idx, bool):
            continue
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(resumes) or not isinstance(resumes[idx], dict):
            continue
        cid = str(resumes[idx].get("source_candidate_id") or "").strip()
        if not cid or cid not in wanted or cid in created:
            continue
        out.setdefault(cid, item)
    return out


# --- Per-candidate launch claims ------------------------------------------------
# Two conditional claims guard every Pairbot POST, so no two launch paths can
# post the same person (Pairbot itself does not stop a second bulk — see
# "Pairbot DuplicateInterview" above):
#
#   session claim  a server launch session claims its rows BEFORE it starts
#                  posting them: engage_status 'queued' + data.engage_session_id
#                  (claim_candidates_for_session). Legacy paths read 'queued' as
#                  already sent and their send claim cannot take it.
#   send claim     EVERY path (/engage/launch, /engage/send-bulk-interview,
#                  Rankings Engage, auto-launch, sessions) moves the rows it is
#                  about to post to 'sending' in the core, immediately before
#                  the POST (_claim_rows_for_send), and posts only what it won.
#                  The idempotency read before it is advisory: the core can
#                  spend minutes on employer resolution between that read and
#                  the POST.
#
# Both lock the candidate's rows in id order (SELECT … ORDER BY id FOR UPDATE:
# no lock-order deadlock; a waiter re-reads the committed row) and take a
# candidate only when EVERY row it has under the job's keys qualifies and no
# audit row holds an interview id for it. A row qualifies while it has no
# interview id and no engage_block_reason and engage_status is '' or 'failed'
# — or 'queued' for the claiming session (an idempotent re-claim on resume).
# The session claim records the status it replaced in
# data.engage_status_before_claim; releasing the claim restores it.
_LAUNCH_CLAIM_LOCK_CTE = """
    locked AS (
        SELECT sc.id, sc.candidate_id, sc.data
        FROM sourced_candidates sc
        WHERE sc.jobdiva_id = ANY(%(keys)s)
          AND sc.candidate_id = ANY(%(ids)s)
        ORDER BY sc.id
        FOR UPDATE
    ),
    eligible AS (
        SELECT l.candidate_id
        FROM locked l
        GROUP BY l.candidate_id
        HAVING bool_and(
            COALESCE(l.data->>'engage_interview_id', '') = ''
            AND COALESCE(l.data->>'engage_block_reason', '') = ''
            AND (
                COALESCE(l.data->>'engage_status', '') IN ('', 'failed')
                OR (%(sid)s <> ''
                    AND l.data->>'engage_status' = 'queued'
                    AND COALESCE(l.data->>'engage_session_id', '') = %(sid)s)
            )
        )
        AND NOT EXISTS (
            SELECT 1 FROM engage_interview_audit a
            WHERE a.candidate_id = l.candidate_id
              AND a.jobdiva_id = ANY(%(keys)s)
              AND COALESCE(a.interview_id, '') <> ''
        )
    )
"""

_CLAIM_SESSION_SQL = (
    "WITH" + _LAUNCH_CLAIM_LOCK_CTE + """
    UPDATE sourced_candidates t
    SET data = COALESCE(t.data, '{}'::jsonb)
               || jsonb_build_object('engage_status', 'queued', 'engage_session_id', %(sid)s::text)
               || CASE WHEN t.data->>'engage_status' = 'queued' THEN '{}'::jsonb
                       ELSE jsonb_build_object(
                           'engage_status_before_claim', COALESCE(t.data->>'engage_status', ''))
                  END,
        updated_at = CURRENT_TIMESTAMP
    FROM locked l
    JOIN eligible e ON e.candidate_id = l.candidate_id
    WHERE t.id = l.id
    RETURNING t.candidate_id
"""
)

# The send claim. Returns (candidate_id, claimed) for every candidate that has
# a row under the job's keys; a candidate with no row is absent.
_SEND_CLAIM_SQL = (
    "WITH" + _LAUNCH_CLAIM_LOCK_CTE + """,
    claimed AS (
        UPDATE sourced_candidates t
        SET data = COALESCE(t.data, '{}'::jsonb) || jsonb_build_object('engage_status', 'sending'),
            updated_at = CURRENT_TIMESTAMP
        FROM locked l
        JOIN eligible e ON e.candidate_id = l.candidate_id
        WHERE t.id = l.id
        RETURNING t.candidate_id
    )
    SELECT DISTINCT l.candidate_id,
           EXISTS (SELECT 1 FROM claimed c WHERE c.candidate_id = l.candidate_id) AS claimed
    FROM locked l
"""
)

# Undo a send claim when nothing (or nothing provable) reached Pairbot:
# 'sending' → 'queued' for the claiming session's own rows (the session keeps
# them and reconciles before any re-post), else → 'failed' (retryable, the
# state a failed POST has always left).
_RELEASE_SEND_CLAIM_SQL = """
    UPDATE sourced_candidates
    SET data = COALESCE(data, '{}'::jsonb) || jsonb_build_object(
            'engage_status',
            CASE WHEN %(sid)s <> '' AND COALESCE(data->>'engage_session_id', '') = %(sid)s
                 THEN 'queued' ELSE 'failed' END),
        updated_at = CURRENT_TIMESTAMP
    WHERE jobdiva_id = ANY(%(keys)s)
      AND candidate_id = ANY(%(ids)s)
      AND data->>'engage_status' = 'sending'
    RETURNING candidate_id
"""


# --- Person gate (services/launch_person_keys.py) ------------------------------
# The claims above are per candidate_id, but one human can be several ids
# (JobDiva numeric, exa_<url>, a Unipile id, a manual upload …). Before either
# claim runs, the same transaction takes a per-job advisory lock (so two
# claims for different ids of one person serialize) and drops every requested
# id whose email / phone / LinkedIn / JobDiva id is already HELD for the job by
# another candidate_id — an interview id, or queued/sending/processing/sent —
# or by an earlier id of the same request. Nothing is written for a dropped
# id: if the holder's launch later fails, the person is launchable again.
_LAUNCH_PERSON_LOCK_KEY = 728199  # 728193-728198 are taken (see launch_sessions/schema.py)

_PERSON_GATE_ROWS_SQL = """
    SELECT sc.candidate_id, sc.source, sc.email, sc.phone, sc.profile_url,
           sc.data->>'email', sc.data->>'phone', sc.data->>'profile_url',
           sc.data->>'linkedin_url', sc.data->>'public_profile_url', sc.data->'urls'->>'linkedin',
           sc.data->>'jobdiva_candidate_id',
           COALESCE(sc.data->>'engage_status', ''), COALESCE(sc.data->>'engage_interview_id', ''),
           COALESCE(sc.data->>'engage_session_id', '')
    FROM sourced_candidates sc
    WHERE sc.jobdiva_id = ANY(%(keys)s)
      AND (sc.candidate_id = ANY(%(ids)s)
           OR COALESCE(sc.data->>'engage_interview_id', '') <> ''
           OR LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('queued', 'sending', 'processing', 'sent'))
    ORDER BY sc.id
"""

_PERSON_GATE_AUDIT_SQL = """
    SELECT DISTINCT candidate_id, candidate_email
    FROM engage_interview_audit
    WHERE jobdiva_id = ANY(%(keys)s)
      AND COALESCE(interview_id, '') <> ''
      AND COALESCE(candidate_email, '') <> ''
"""


def _person_gate_row(r: Sequence[Any]) -> Dict[str, Any]:
    return {
        "candidate_id": r[0], "source": r[1], "email": r[2], "phone": r[3], "profile_url": r[4],
        "data": {
            "email": r[5], "phone": r[6], "profile_url": r[7], "linkedin_url": r[8],
            "public_profile_url": r[9], "urls": {"linkedin": r[10]}, "jobdiva_candidate_id": r[11],
        },
        "engage_status": r[12], "engage_interview_id": r[13], "engage_session_id": r[14],
    }


def _person_gate(
    cur,
    keys: List[str],
    ids: List[str],
    session_id: str,
    fallback_rows: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Tuple[str, str]]:
    """Run inside a claim transaction (before the claim statement). Returns
    ``{candidate_id: (holder candidate_id, key kind)}`` for the requested ids
    to drop. ``fallback_rows`` gives contacts for requested ids with no saved
    row (legacy posts those unclaimed — they must still not double a person)."""
    from services import launch_person_keys as lpk

    for k in sorted(set(keys)):
        cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_LAUNCH_PERSON_LOCK_KEY, k))
    cur.execute(_PERSON_GATE_ROWS_SQL, {"keys": keys, "ids": ids})
    wanted = set(ids)
    order = {cid: i for i, cid in enumerate(ids)}
    requested: List[Tuple[str, List[str]]] = []
    holders: List[Tuple[str, List[str]]] = []
    with_rows: set = set()
    for raw in cur.fetchall() or []:
        row = _person_gate_row(raw)
        cid = str(row["candidate_id"] or "")
        pkeys = lpk.person_keys(row)
        if cid in wanted:
            requested.append((cid, pkeys))
            with_rows.add(cid)
        if lpk.holds_person(row, session_id=session_id):
            holders.append((cid, pkeys))
    for cid, row in (fallback_rows or {}).items():
        if cid in wanted and cid not in with_rows:
            requested.append((cid, lpk.person_keys(row)))
    cur.execute(_PERSON_GATE_AUDIT_SQL, {"keys": keys})
    for cid, email in cur.fetchall() or []:
        ek = lpk.email_key(email)
        if ek:
            holders.append((str(cid or ""), [f"{lpk.KEY_EMAIL}:{ek}"]))
    requested.sort(key=lambda e: order.get(e[0], len(order)))
    dups = lpk.find_person_duplicates(requested, holders)
    for cid, (other, kind) in dups.items():
        logger.warning(
            "duplicate_person job=%s candidate=%s holder=%s key=%s session=%s",
            keys[0], cid, other, kind, bool(session_id),
        )
    return dups


def _person_duplicate_reason(other: str, kind: str) -> str:
    return f"Same person already launched for this job (matched by {kind}; candidate {other})"


def _claim_rows_for_send(
    job_keys: Sequence[str],
    candidate_ids: Sequence[str],
    session_id: Optional[str],
    fallback_rows: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[str], set, Dict[str, Tuple[str, str]]]:
    """Run the person gate + the send claim (sync). Returns (claimed ids in
    input order, every id that has a row under the job's keys, the ids the
    person gate dropped as ``{id: (holder id, key kind)}``). Raises on a DB
    error — the caller then posts nothing."""
    keys = _job_key_list(job_keys)
    ids = [str(c) for c in dict.fromkeys(candidate_ids or []) if str(c).strip()]
    if not keys or not ids:
        return [], set(), {}
    sid = str(session_id or "").strip()
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        dups = _person_gate(cur, keys, ids, sid, fallback_rows)
        gated = [c for c in ids if c not in dups]
        rows = []
        if gated:
            cur.execute(_SEND_CLAIM_SQL, {"keys": keys, "ids": gated, "sid": sid})
            rows = cur.fetchall() or []
        conn.commit()
        cur.close()
    finally:
        conn.close()
    with_rows = {str(r[0]) for r in rows}
    claimed = {str(r[0]) for r in rows if r[1]}
    return [c for c in ids if c in claimed], with_rows, dups


def _release_send_claim(
    job_keys: Sequence[str], candidate_ids: Sequence[str], session_id: Optional[str]
) -> int:
    """Undo a send claim (sync, fail-open: logs and returns 0 on error)."""
    keys = _job_key_list(job_keys)
    ids = [str(c) for c in candidate_ids or [] if str(c).strip()]
    if not keys or not ids:
        return 0
    try:
        conn = _get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                _RELEASE_SEND_CLAIM_SQL,
                {"keys": keys, "ids": ids, "sid": str(session_id or "").strip()},
            )
            n = len(cur.fetchall() or [])
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — rows stay 'sending' (never re-posted)
        logger.error(
            "engage_send_claim_release_failed candidates=%d err=%s — rows left 'sending'",
            len(ids), exc,
        )
        return 0
    return n


def claim_candidates_for_session(
    job_keys: Tuple[str, ...],
    candidate_ids: Sequence[str],
    session_id: str,
    duplicates: Optional[Dict[str, Tuple[str, str]]] = None,
) -> List[str]:
    """Atomically claim rows for a launch session (sync; call via to_thread).

    Returns the claimed candidate ids in input order; only these may be sent
    (with ``claimed_session_id=session_id``). Ids not returned are launched,
    in flight, blocked, owned by another session, or (person gate) another id
    of a person already held for the job — never post them. ``duplicates``,
    when given, is filled with the person gate's ``{id: (holder id, kind)}``.
    """
    keys = _job_key_list(job_keys)
    ids = [str(c) for c in dict.fromkeys(candidate_ids or []) if str(c).strip()]
    sid = str(session_id or "").strip()
    if not keys or not ids or not sid:
        return []
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        dups = _person_gate(cur, keys, ids, sid)
        gated = [c for c in ids if c not in dups]
        claimed: set = set()
        if gated:
            cur.execute(_CLAIM_SESSION_SQL, {"keys": keys, "ids": gated, "sid": sid})
            claimed = {str(r[0]) for r in (cur.fetchall() or [])}
        conn.commit()
        cur.close()
    finally:
        conn.close()
    if duplicates is not None:
        duplicates.update(dups)
    return [c for c in ids if c in claimed]


def release_session_claims(
    session_id: str,
    job_keys: Tuple[str, ...],
    candidate_ids: Optional[Sequence[str]] = None,
) -> int:
    """Hand a session's leftover 'queued' rows back: engage_status returns to
    what it was before the claim (data.engage_status_before_claim — '' or
    'failed'; '' for a claim older than that key), and engage_session_id +
    engage_status_before_claim are dropped. A previously failed row stays
    'failed', so auto-launch (which only takes '' rows) does not pick it up.
    Optionally only ``candidate_ids``. Rows another path moved on
    (sending/processing/sent/failed) are untouched. Sync; returns rows released."""
    keys = _job_key_list(job_keys)
    sid = str(session_id or "").strip()
    if not keys or not sid:
        return 0
    params: Dict[str, Any] = {"keys": keys, "sid": sid}
    id_filter = ""
    if candidate_ids is not None:
        params["ids"] = [str(c) for c in candidate_ids if str(c).strip()]
        if not params["ids"]:
            return 0
        id_filter = "AND candidate_id = ANY(%(ids)s)"
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE sourced_candidates
            SET data = (COALESCE(data, '{{}}'::jsonb) - 'engage_session_id' - 'engage_status_before_claim')
                       || jsonb_build_object(
                            'engage_status', COALESCE(data->>'engage_status_before_claim', '')),
                updated_at = CURRENT_TIMESTAMP
            WHERE jobdiva_id = ANY(%(keys)s)
              AND data->>'engage_status' = 'queued'
              AND data->>'engage_session_id' = %(sid)s
              {id_filter}
            RETURNING candidate_id
            """,
            params,
        )
        n = len(cur.fetchall() or [])
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return n


# JobDiva's parser requires an email, so the provisioner mints one for
# phone-only candidates. It is deterministic per candidate and
# `_update_candidate_name` writes it through to JobDiva, which makes it a real
# lookup key on re-provision — see the applicant index in
# `_provision_batch_to_jobdiva`.
_PAIR_SYNTHETIC_EMAIL_DOMAIN = "@no-email.jobdiva.local"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    if not (needs_reread and JOBDIVA_PROVISION_LINKEDIN_REFETCH and refetch_budget[0] > 0):
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
    if not jobdiva_provisioning_enabled():
        logger.warning(
            "[%s] JOBDIVA_PROVISIONING_ENABLED=false: %d candidate(s) not provisioned to JobDiva",
            label, len(candidate_ids),
        )
        results["skipped"] = len(candidate_ids)
        return results
    # LinkedIn re-reads left for this batch (see _provisioning_resume).
    refetch_budget = [JOBDIVA_PROVISION_LINKEDIN_REFETCH_CAP]

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
                    "jobdiva_provisioned_at": _utc_now_iso(),
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
    conn = _get_db_connection()
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
    if not jobdiva_provisioning_enabled():
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


# Per candidate (any row under either job key): already sent, or blocked.
# Params: claimed_session_id twice ('' = none), candidate ids, job key, alt key.
_ENGAGE_IDEMPOTENCY_SQL = """
    SELECT candidate_id,
           bool_or(
               (
                   COALESCE(data ->> 'engage_status', '') NOT IN ('', 'failed')
                   AND NOT (
                       data ->> 'engage_status' = 'queued'
                       AND %s <> ''
                       AND COALESCE(data ->> 'engage_session_id', '') = %s
                   )
               )
               OR COALESCE(data ->> 'engage_interview_id', '') <> ''
           ) AS already_sent,
           MAX(NULLIF(data ->> 'engage_block_reason', '')) AS block_reason
    FROM sourced_candidates
    WHERE candidate_id = ANY(%s)
      AND (jobdiva_id = %s OR jobdiva_id = %s)
    GROUP BY candidate_id
"""


@router.post("/engage/send-bulk-interview")
async def send_bulk_interview(request: SendBulkInterviewRequest):
    """Thin HTTP wrapper — the QA/edit modal and rankings Engage clicks call
    this. The batched launch orchestrator calls `_send_bulk_interview_core`
    directly, once per internal batch."""
    return await _send_bulk_interview_core(request)


async def _send_bulk_interview_core(
    request: SendBulkInterviewRequest,
    precomputed_signals: Optional[Dict[str, Dict[str, Any]]] = None,
    launch_id: Optional[str] = None,
    batch_idx: Optional[int] = None,
    *,
    claimed_session_id: Optional[str] = None,
    provision_inline: bool = True,
    bulk_id_for: Optional[Callable[..., Any]] = None,
    recruiter_email: Optional[str] = None,
):
    """
    Send the (potentially edited) interview payload to the PAIR bulk-interviews API.
    Saves the request and response to engage_interview_audit for traceability.

    Every path takes the send claim right before the POST (rows → 'sending';
    only the ids it wins are posted, the rest come back in
    skipped_already_sent; a DB error posts nothing). If no answer comes back
    from Pairbot, the held rows are released ('failed', or 'queued' for the
    claiming session's own rows).

    Launch-session hooks (all default to the legacy behaviour):
      claimed_session_id  rows 'queued' with this data.engage_session_id are
                          sendable (every other non-empty engage_status still
                          reads as already sent), the idempotency read fails
                          CLOSED — nothing is posted if it errors — and only
                          ids with a saved row are ever posted (a row-less id
                          is excluded; legacy calls still post it unclaimed).
      provision_inline    False = skip the fire-and-forget JobDiva
                          provisioning; the caller runs provision_sent_candidates.
      bulk_id_for         ``(batch_idx, posted_candidate_ids) -> bulk_id`` (or an
                          awaitable of it), called right before the POST with the
                          exact source_candidate_ids of the payload in resume
                          order; its id is the client bulk_id. If it raises,
                          nothing is posted.
    """
    # The fire-once ledger's "this launch" boundary (launch_effects_ledger).
    _core_started_at = datetime.now(timezone.utc)
    # Pool-leak safety net for the audit-write block below: if any exception
    # fires between `conn = _get_db_connection()` and the explicit close, the
    # outer try's finally returns the slot. The explicit close stays on the
    # happy path so the slot frees before the post-success email tasks run;
    # _PooledConnection.close() is idempotent (re-close is a no-op).
    conn = None
    cur = None
    try:
        # Parse the payload
        try:
            payload_obj = json.loads(request.payload)
            jd_block = payload_obj.get("jd", {})
            job_id_from_payload = jd_block.get("job_id") or jd_block.get("jobdiva_id") or "unknown"
            print(f"DEBUG: send_bulk_interview called for job {job_id_from_payload}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format in payload")

        _validate_pair_payload_contacts(payload_obj)

        # Fetch job metadata (recruiter emails) and defense-in-depth outreach check
        if job_id_from_payload and job_id_from_payload != "unknown":
            try:
                _stop_conn = _get_db_connection()
                try:
                    _stop_cur = _stop_conn.cursor()
                    _stop_cur.execute("""
                        SELECT outreach_stopped_at, recruiter_emails FROM monitored_jobs
                        WHERE job_id = %s OR jobdiva_id = %s
                        LIMIT 1
                    """, (str(job_id_from_payload), str(job_id_from_payload)))
                    _stop_row = _stop_cur.fetchone()
                    _stop_cur.close()
                    if _stop_row:
                        if _stop_row[0] is not None:
                            raise HTTPException(
                                status_code=409,
                                detail="Job activity has been stopped. Cannot launch new candidates.",
                            )
                        # Inject recruiter emails into PAIR payload so PAIR can send notifications (e.g. 20-hour report)
                        if _stop_row[1]:
                            recruiter_emails = [
                                str(email).strip()
                                for email in _parse_json_list(_stop_row[1])
                                if str(email).strip()
                            ]
                            payload_obj.setdefault("jd", {})["recruiter_emails"] = recruiter_emails
                finally:
                    _stop_conn.close()
            except HTTPException:
                raise
            except Exception as _stop_check_err:
                logger.warning(f"Could not check job metadata for job {job_id_from_payload}: {_stop_check_err}")

        # Idempotency: drop candidates that already have a *successful* engage_status
        # so retries or staged-launch races don't create duplicate interviews on the
        # PAIR side. Crucially, 'failed' is NOT treated as already-sent — a candidate
        # PAIR accepted but returned no interview_id for (or that erred) must remain
        # retryable, otherwise a transient miss would silently skip them forever.
        # We keep the skipped candidate_ids in skipped_already_sent so the caller
        # can show them as already-launched in the UI.
        #
        # Also already sent: a row holding an interview id (the voice webhook
        # can leave engage_status 'failed' = an interview Fail), and a row
        # 'queued' by a launch session other than ``claimed_session_id``.
        # Rows with data.engage_block_reason (a Pairbot duplicate) are never
        # re-posted; they come back as exclusions. This read is an early,
        # advisory filter: the send claim right before the POST
        # (_claim_rows_for_send) is what guarantees one post per person.
        skipped_already_sent: List[str] = []
        blocked_records: List[Dict[str, str]] = []
        if request.real_candidate_ids:
            try:
                _idem_conn = _get_db_connection()
                try:
                    _idem_cur = _idem_conn.cursor()
                    _idem_cur.execute(
                        _ENGAGE_IDEMPOTENCY_SQL,
                        (
                            str(claimed_session_id or ""),
                            str(claimed_session_id or ""),
                            list(request.real_candidate_ids),
                            str(job_id_from_payload or ""),
                            str(payload_obj.get("jd", {}).get("jobdiva_id") or ""),
                        ),
                    )
                    for _idem_row in _idem_cur.fetchall() or []:
                        if _idem_row[1]:
                            skipped_already_sent.append(_idem_row[0])
                        elif _idem_row[2]:
                            blocked_records.append({
                                "candidate_id": str(_idem_row[0]),
                                "name": "",
                                "reason": _launch_block_reason_text(str(_idem_row[2])),
                            })
                    _idem_cur.close()
                finally:
                    _idem_conn.close()
            except Exception as _idem_err:
                logger.warning(f"engage idempotency check failed for job {job_id_from_payload}: {_idem_err}")
                if claimed_session_id:
                    # A launch session never posts blind: fail CLOSED. Its
                    # rows stay 'queued' for the session to retry.
                    return {
                        "success": False,
                        "message": "Launch idempotency check failed; nothing was sent.",
                        "data": [],
                        "skipped_already_sent": [],
                        "excluded_candidates": [],
                        "employer_unverified": [],
                        "raw_response": {},
                    }

        if skipped_already_sent or blocked_records:
            skip_set = set(skipped_already_sent) | {b["candidate_id"] for b in blocked_records}
            kept_indices = [
                idx for idx, cid in enumerate(request.real_candidate_ids) if cid not in skip_set
            ]
            request.real_candidate_ids = [request.real_candidate_ids[i] for i in kept_indices]
            existing_resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
            if isinstance(existing_resumes, list) and len(existing_resumes) >= max(kept_indices, default=-1) + 1:
                payload_obj["resumes"] = [existing_resumes[i] for i in kept_indices]
            logger.info(
                f"engage idempotency: skipped {len(skipped_already_sent)} already-sent candidates for job {job_id_from_payload}"
                + (f" (+{len(blocked_records)} blocked)" if blocked_records else "")
            )

        excluded_records: List[Dict[str, str]] = list(blocked_records)
        unverified_employer_records: List[Dict[str, str]] = []
        if request.real_candidate_ids:
            excluded_candidate_ids = set()
            try:
                # (candidate_id, name, hydrated candidate) — rows are collected
                # first and gated after the DB connection is released, because
                # the employer-resolution pass between the two can await
                # resume fetches + LLM parses for a while.
                _gate_rows: List[Tuple[str, str, Dict[str, Any]]] = []
                _excl_conn = _get_db_connection()
                try:
                    _excl_cur = _excl_conn.cursor()
                    _excl_cur.execute(
                        """
                        SELECT customer_name
                        FROM monitored_jobs
                        WHERE job_id = %s OR jobdiva_id = %s
                        LIMIT 1
                        """,
                        (str(job_id_from_payload or ""), str(job_id_from_payload or "")),
                    )
                    _excl_client_row = _excl_cur.fetchone()
                    _excl_client = (
                        str(_excl_client_row[0])
                        if _excl_client_row and _excl_client_row[0]
                        else ""
                    )

                    _excl_cur.execute(
                        """
                        SELECT candidate_id, data, name, headline, resume_text, source
                        FROM sourced_candidates
                        WHERE candidate_id = ANY(%s)
                          AND (jobdiva_id = %s OR jobdiva_id = %s)
                        """,
                        (
                            list(request.real_candidate_ids),
                            str(job_id_from_payload or ""),
                            str(payload_obj.get("jd", {}).get("jobdiva_id") or ""),
                        ),
                    )
                    _excl_rows = _excl_cur.fetchall() or []
                    # Employment history lives in candidate_enhanced_info, not
                    # in `data` — without this the gate matches an empty blob.
                    _excl_signals = _fetch_stored_employer_signals(
                        _excl_conn, [str(r[0]) for r in _excl_rows]
                    )
                    for _row in _excl_rows:
                        _cdata = _row[1] or {}
                        if isinstance(_cdata, str):
                            try:
                                _cdata = json.loads(_cdata)
                            except Exception:
                                _cdata = {}
                        _cdata = _merge_employer_signals(
                            _cdata,
                            headline=str(_row[3] or ""),
                            signals=_excl_signals.get(str(_row[0])),
                        )
                        _cdata.setdefault("candidate_id", str(_row[0]))
                        # Stored resume + source ride along so the resolution
                        # pass can parse without re-fetching from JobDiva.
                        if _row[4] and not _cdata.get("resume_text"):
                            _cdata["resume_text"] = str(_row[4])
                        if _row[5] and not _cdata.get("source"):
                            _cdata["source"] = str(_row[5])
                        _gate_rows.append((str(_row[0]), str(_row[2] or ""), _cdata))
                    _excl_cur.close()
                finally:
                    _excl_conn.close()

                # Employer resolution (policy 2026-09-02): any candidate whose
                # employer data is missing or not confident gets their resume
                # fetched + parsed NOW, before outreach, with JobDiva's own
                # profile work history attached as corroboration. Fails open —
                # a resolution error gates on stored signals, as before.
                try:
                    from services.employer_resolution import employer_verification_state
                except Exception:  # noqa: BLE001
                    employer_verification_state = None  # type: ignore[assignment]
                _resolved_signals: Dict[str, Dict[str, Any]] = {}
                # Fix 1: /engage/launch resolves once for the whole pool and
                # passes the result in. Every id the launch pass covered is a
                # key (empty dict = nothing resolved), so only rows it missed
                # are resolved here. Legacy /engage/send-bulk passes None.
                if precomputed_signals is not None:
                    _resolved_signals = {
                        _cid: precomputed_signals[_cid]
                        for _cid, _, _ in _gate_rows
                        if _cid in precomputed_signals
                    }
                    _to_resolve = [
                        c for _cid, _, c in _gate_rows if _cid not in precomputed_signals
                    ]
                else:
                    _to_resolve = [c for _, _, c in _gate_rows]
                if _to_resolve:
                    try:
                        from services.employer_resolution import resolve_employer_signals
                        async with _trace_span("employer_gate", scope="batch", n=len(_to_resolve)):
                            _resolved_signals.update(await asyncio.wait_for(
                                resolve_employer_signals(_to_resolve),
                                timeout=240.0
                            ) or {})
                    except Exception as _res_err:  # noqa: BLE001
                        logger.warning(
                            "employer resolution failed for job %s (gating on stored signals): %s",
                            job_id_from_payload, _res_err,
                        )

                _gate_rows = [
                    (
                        _cid,
                        _cname,
                        _merge_employer_signals(_cdata, signals=_resolved_signals[_cid])
                        if _resolved_signals.get(_cid) else _cdata,
                    )
                    for _cid, _cname, _cdata in _gate_rows
                ]
                # Launch-level freshness stamps ride on the precomputed entry.
                if precomputed_signals is not None:
                    for _cid, _, _cdata in _gate_rows:
                        _ts = (precomputed_signals.get(_cid) or {}).get("resume_updated_at")
                        if _ts and not _cdata.get("resume_updated_at"):
                            _cdata["resume_updated_at"] = _ts
                # Resume freshness (phase 3): stamp JobDiva's resume timestamp
                # onto resume-verified candidates so a years-old "Present"
                # classifies as verified_stale, not verified. Advisory only —
                # never blocks; fails open.
                try:
                    from services.employer_resolution import stamp_resume_freshness
                    # Hard 20s cap: freshness is advisory, and this fetch sits
                    # on the launch path — a slow JobDiva must cost seconds,
                    # not a batch-long stall (TimeoutError lands in the
                    # except below and the launch proceeds unstamped).
                    _fresh_rows = [
                        c for _cid, _, c in _gate_rows
                        if precomputed_signals is None or _cid not in precomputed_signals
                    ]
                    if _fresh_rows:
                        await asyncio.wait_for(
                            stamp_resume_freshness(_fresh_rows),
                            timeout=20.0,
                        )
                except Exception as _fresh_err:  # noqa: BLE001
                    logger.warning(
                        "resume-freshness check failed for job %s: %s",
                        job_id_from_payload, _fresh_err,
                    )

                for _cid, _cname, _cdata in _gate_rows:
                    _is_excl, _excl_reason = is_candidate_excluded_from_pair(
                        _cdata, _excl_client
                    )
                    if _is_excl:
                        logger.info(
                            "send_bulk_interview skipping excluded candidate %s reason: %s",
                            _cid,
                            _excl_reason,
                        )
                        excluded_candidate_ids.add(_cid)
                        excluded_records.append({
                            "candidate_id": _cid,
                            "name": _cname,
                            "reason": _excl_reason,
                        })
                        continue
                    # Not excluded — but say HOW WELL we know that. An
                    # unverified employer means the client/no-contact checks
                    # had nothing to judge, not that the candidate is clean.
                    if employer_verification_state is not None:
                        _emp_state = employer_verification_state(_cdata)
                        if _emp_state != "verified":
                            _unv_record = {
                                "candidate_id": _cid,
                                "name": _cname,
                                "employer_verification": _emp_state,
                            }
                            if _cdata.get("resume_updated_at"):
                                _unv_record["resume_updated_at"] = str(
                                    _cdata["resume_updated_at"]
                                )
                            unverified_employer_records.append(_unv_record)
                            logger.info(
                                "launch_employer_unverified candidate=%s job=%s state=%s client=%r",
                                _cid, job_id_from_payload, _emp_state, _excl_client,
                            )
            except Exception as _excl_err:
                logger.warning(
                    f"candidate exclusion check failed for job {job_id_from_payload}: {_excl_err}"
                )

            if excluded_candidate_ids:
                kept_indices = [
                    idx
                    for idx, cid in enumerate(request.real_candidate_ids)
                    if cid not in excluded_candidate_ids
                ]
                request.real_candidate_ids = [
                    request.real_candidate_ids[i] for i in kept_indices
                ]
                existing_resumes = (
                    payload_obj.get("resumes")
                    if isinstance(payload_obj, dict)
                    else None
                )
                if (
                    isinstance(existing_resumes, list)
                    and len(existing_resumes) >= max(kept_indices, default=-1) + 1
                ):
                    payload_obj["resumes"] = [existing_resumes[i] for i in kept_indices]

        if not request.real_candidate_ids:
            return {
                "success": True,
                "message": "All requested candidates were already launched or excluded; nothing to send.",
                "data": [],
                "skipped_already_sent": skipped_already_sent,
                "excluded_candidates": excluded_records,
                "employer_unverified": unverified_employer_records,
                "raw_response": {},
            }

        is_success = False
        response_data = {}

        if request.dry_run:
            logger.info("🧪 Dry run enabled: Skipping external PAIR API call.")
            is_success = True
            response_data = {
                "status": "success",
                "message": "DRY RUN: External call skipped, but local processing continued.",
                "data": [
                    {"interview_id": f"dry_run_{cid}", "candidate_id": cid}
                    for cid in request.real_candidate_ids
                ]
            }
        else:
            # Send claim (every launch path — see "Per-candidate launch
            # claims"): atomically move the rows about to be posted to
            # 'sending' and post ONLY what this call won. The idempotency read
            # above is advisory (minutes of employer resolution can pass since
            # it ran); an id that lost the claim was sent, claimed or blocked
            # by another path meanwhile and comes back as already sent. A DB
            # error here posts nothing.
            _send_keys = (
                str(job_id_from_payload or ""),
                str(payload_obj.get("jd", {}).get("jobdiva_id") or ""),
            )
            # Contacts of the payload's résumés, for the person gate's view of
            # ids that have no saved row (legacy posts those unclaimed).
            _gate_fallback: Dict[str, Dict[str, Any]] = {}
            for _r in (payload_obj.get("resumes") if isinstance(payload_obj, dict) else None) or []:
                if isinstance(_r, dict) and str(_r.get("source_candidate_id") or "").strip():
                    _gate_fallback[str(_r["source_candidate_id"]).strip()] = {
                        "email": _r.get("email"), "phone": _r.get("phone"),
                        "profile_url": _r.get("linkedin_url") or _r.get("linkedin") or _r.get("profile_url"),
                    }
            try:
                _send_claimed, _send_with_rows, _send_dups = _claim_rows_for_send(
                    _send_keys, list(request.real_candidate_ids), claimed_session_id, _gate_fallback,
                )
            except Exception as _claim_err:  # noqa: BLE001
                logger.error(
                    "engage_send_claim_failed job=%s candidates=%d err=%s — nothing sent",
                    job_id_from_payload, len(request.real_candidate_ids), _claim_err,
                )
                return {
                    "success": False,
                    "message": "Launch claim failed; nothing was sent.",
                    "data": [],
                    "skipped_already_sent": skipped_already_sent,
                    "excluded_candidates": excluded_records,
                    "employer_unverified": unverified_employer_records,
                    "raw_response": {},
                }
            _send_keep = set(_send_claimed)
            # Person gate: another id of a person already held for this job.
            # Never posted, never "row-less", reported as an exclusion.
            for _c, (_other, _kind) in _send_dups.items():
                excluded_records.append({
                    "candidate_id": _c, "name": "", "reason": _person_duplicate_reason(_other, _kind),
                })
            _rowless = [
                str(c) for c in request.real_candidate_ids
                if str(c) not in _send_with_rows and str(c) not in _send_dups
            ]
            if _rowless:
                if claimed_session_id:
                    # A session posts only rows it holds — never a row-less id.
                    for _c in _rowless:
                        excluded_records.append({
                            "candidate_id": _c, "name": "", "reason": "not saved for this job",
                        })
                else:
                    # Legacy: no saved row to claim (nor for a session to
                    # race on) — posted as before.
                    _send_keep.update(_rowless)
                logger.warning(
                    "engage_send_claim_rowless job=%s candidates=%d session=%s",
                    job_id_from_payload, len(_rowless), bool(claimed_session_id),
                )
            _send_lost = [
                str(c) for c in request.real_candidate_ids
                if str(c) not in _send_keep and str(c) in _send_with_rows
            ]
            if _send_lost:
                skipped_already_sent.extend(_send_lost)
                logger.warning(
                    "engage_send_claim_lost job=%s candidates=%d (sent or claimed by another launch meanwhile)",
                    job_id_from_payload, len(_send_lost),
                )
            request.real_candidate_ids = [
                c for c in request.real_candidate_ids if str(c) in _send_keep
            ]
            _send_resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
            if isinstance(_send_resumes, list):
                if claimed_session_id:
                    payload_obj["resumes"] = [
                        r for r in _send_resumes
                        if isinstance(r, dict)
                        and str(r.get("source_candidate_id") or "").strip() in _send_keep
                    ]
                elif _send_lost or _send_dups:
                    _send_drop = set(_send_lost) | set(_send_dups)
                    payload_obj["resumes"] = [
                        r for r in _send_resumes
                        if not (
                            isinstance(r, dict)
                            and str(r.get("source_candidate_id") or "").strip() in _send_drop
                        )
                    ]
            if not request.real_candidate_ids:
                return {
                    "success": True,
                    "message": "All requested candidates were already launched or excluded; nothing to send.",
                    "data": [],
                    "skipped_already_sent": skipped_already_sent,
                    "excluded_candidates": excluded_records,
                    "employer_unverified": unverified_employer_records,
                    "raw_response": {},
                }
            # The rows this call now holds in 'sending'. Until Pairbot answers,
            # any exit gives them back (_release_send_claim): a session's own
            # rows to 'queued', others to 'failed'.
            _send_held = [c for c in request.real_candidate_ids if str(c) in set(_send_claimed)]

            # Send to external PAIR API
            external_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews"
            try:
                # Client-generate bulk_id so requests are idempotent and lookups
                # succeed even if the connection drops after PairBot accepts but
                # before the 202 response arrives — without this, a client-side
                # retry has no id to look up and would create a second bulk.
                client_bulk_id = str(uuid.uuid4())
                _launch_idem_key: Optional[str] = None
                if launch_id is not None and batch_idx is not None:
                    # Fix 3: deterministic per (launch, batch, ids) so a re-run of
                    # the same async batch reuses Pairbot's bulk instead of
                    # creating a duplicate.
                    _launch_idem_key = _launch_batch_idempotency_key(
                        launch_id, batch_idx, request.real_candidate_ids
                    )
                    client_bulk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _launch_idem_key))
                if bulk_id_for is not None:
                    # Launch session: the runner owns the bulk id (deterministic
                    # per session/attempt/batch; Pairbot honours a client bulk_id)
                    # and records the exact posted ids before anything leaves this
                    # process. If that fails, nothing is sent.
                    _posted_ids = [
                        str(r.get("source_candidate_id") or "")
                        for r in (payload_obj.get("resumes") or [])
                        if isinstance(r, dict)
                    ]
                    try:
                        _forced_bulk_id = bulk_id_for(batch_idx, _posted_ids)
                        if inspect.isawaitable(_forced_bulk_id):
                            _forced_bulk_id = await _forced_bulk_id
                    except Exception as _bid_err:  # noqa: BLE001
                        logger.error(
                            "launch_bulk_id_for_failed batch_idx=%s candidates=%d err=%s — not sending",
                            batch_idx, len(_posted_ids), _bid_err,
                        )
                        _release_send_claim(_send_keys, _send_held, claimed_session_id)
                        _send_held = []
                        return {
                            "success": False,
                            "message": f"Could not record the batch before sending ({type(_bid_err).__name__}); nothing was sent.",
                            "data": [],
                            "skipped_already_sent": skipped_already_sent,
                            "excluded_candidates": excluded_records,
                            "employer_unverified": unverified_employer_records,
                            "raw_response": {},
                        }
                    if _forced_bulk_id:
                        client_bulk_id = str(_forced_bulk_id)
                if isinstance(payload_obj, dict):
                    payload_obj["bulk_id"] = client_bulk_id
                    # Server-resolved recruiter only (never a request field,
                    # so /engage/send-bulk-interview callers cannot set it).
                    if recruiter_email:
                        payload_obj["recruiter_email"] = recruiter_email
                    # Only the launch's recruiter can join, so a handoff
                    # without one is refused by Pair-bot (422); send the flag
                    # only when there is someone to hand off to.
                    if request.recruiter_handoff and recruiter_email:
                        payload_obj["recruiter_handoff"] = True
                        if request.handoff_timeout_sec:
                            payload_obj["handoff_timeout_sec"] = request.handoff_timeout_sec
                    elif request.recruiter_handoff:
                        logger.warning("recruiter_handoff requested without a recruiter_email; launching without handoff")
                logger.info(f"📤 Sending bulk interview to {external_url} with bulk_id={client_bulk_id}")

                async with _trace_span("pairbot_post", batch_idx=batch_idx):
                    response = await _post_to_pairbot(external_url, payload_obj)
            except BaseException:
                # No answer from Pairbot (transport error, timeout, cancel).
                # Pre-claim behaviour for legacy rows ('failed' = retryable,
                # as a failed POST always left them); a session keeps its rows
                # 'queued' and reconciles by-job before any re-post.
                if _send_held:
                    _release_send_claim(_send_keys, _send_held, claimed_session_id)
                raise

            response_data = {}
            try:
                if response.content:
                    response_data = response.json()
                else:
                    response_data = {"message": "Success (Empty response)"}
            except Exception:
                response_data = {"message": f"Raw: {response.text[:100]}"}

            # PAI-155: 202 Accepted means interviews are creating in background.
            # Wait on SSE for creation_completed before writing audit/engage_status.
            is_success = 200 <= response.status_code < 300
            logger.info(f"📥 PAIR API response status: {response.status_code}")
            if is_success and response.status_code == 202:
                bulk_id = str(response_data.get("bulk_id") or client_bulk_id).strip()
                if not bulk_id:
                    bulk_id = client_bulk_id
                job_id_alt = str(payload_obj.get("jd", {}).get("jobdiva_id") or "")
                # Mark processing so rank-list shows work in progress if the
                # stream is slow; overwritten to sent/failed after creation.
                # The bulk id rides in the same UPDATE (data.engage_bulk_id) so
                # a resume knows which bulk to reconcile a 'processing' row on.
                try:
                    _stamp_engage_status_batch(
                        list(request.real_candidate_ids),
                        "processing",
                        job_id_value=str(job_id_from_payload or ""),
                        job_id_alt=job_id_alt,
                        bulk_id=bulk_id,
                    )
                except Exception as _proc_err:
                    logger.warning(
                        "engage_processing_stamp_failed bulk_id=%s err=%s",
                        bulk_id,
                        _proc_err,
                    )
                logger.info(
                    "pairbot_async_accepted bulk_id=%s waiting_for_creation candidates=%d",
                    bulk_id,
                    len(request.real_candidate_ids),
                )
                try:
                    if _launch_idem_key is not None:
                        _launch_batch_set_bulk(
                            _launch_idem_key, bulk_id, launch_id=launch_id,
                            idx=batch_idx, candidate_ids=list(request.real_candidate_ids),
                        )
                    async with _trace_span("pairbot_wait_creation", batch_idx=batch_idx):
                        if _launch_idem_key is not None:
                            # Webhook (POST /webhooks/pairbot/creation) first,
                            # SSE wait as the fallback — whichever lands first.
                            creation_event = await _wait_for_creation_webhook_or_sse(bulk_id)
                        else:
                            creation_event = await _wait_for_pairbot_creation(bulk_id)
                except Exception as wait_err:
                    # A dead stream or a wait timeout is NOT proof creation
                    # failed — PairBot already accepted the bulk and may
                    # still be working. Check PairBot's own record before
                    # stamping anyone `failed`; a retry on a false failure
                    # would create a duplicate bulk for candidates PairBot
                    # already has interviews for.
                    logger.error(
                        "engage_wait_for_creation_error bulk_id=%s candidates=%d err=%s "
                        "— confirming with PairBot before stamping failed",
                        bulk_id,
                        len(request.real_candidate_ids),
                        wait_err,
                    )
                    confirmed_failed = await _confirm_failed_candidates_with_pairbot(
                        bulk_id, job_id_alt, list(request.real_candidate_ids),
                        job_keys=(str(job_id_from_payload or ""), job_id_alt),
                    )
                    if confirmed_failed:
                        try:
                            _stamp_engage_status_batch(
                                confirmed_failed,
                                "failed",
                                job_id_value=str(job_id_from_payload or ""),
                                job_id_alt=job_id_alt,
                            )
                        except Exception as _fail_stamp_err:
                            logger.error(
                                "engage_failed_stamp_after_wait_error bulk_id=%s err=%s",
                                bulk_id,
                                _fail_stamp_err,
                            )
                    else:
                        # PairBot confirms it's still working, or the check
                        # itself couldn't tell — leave as 'processing'. There
                        # is no automatic reconciler any more, so this needs
                        # manual follow-up (dashboard/logs on bulk_id) if it
                        # never resolves.
                        logger.error(
                            "engage_wait_for_creation_error_unconfirmed bulk_id=%s "
                            "candidates=%d left as 'processing' — PairBot did not "
                            "confirm these failed; needs manual follow-up if it "
                            "never resolves",
                            bulk_id,
                            len(request.real_candidate_ids),
                        )
                    raise wait_err
                response_data = {
                    **response_data,
                    "bulk_id": bulk_id,
                    "data": creation_event.get("data") or [],
                    "total_created": creation_event.get("total_created"),
                    "failed_interviews": creation_event.get("failed_interviews"),
                    "message": creation_event.get("message")
                    or response_data.get("message")
                    or "Interviews created",
                }
                logger.info(
                    "pairbot_creation_completed bulk_id=%s created=%s",
                    bulk_id,
                    response_data.get("total_created"),
                )

        # Pairbot DuplicateInterview answers: resolved against by-job BEFORE
        # the audit connection is taken (a network call must not hold a pool
        # slot). Proven ones (by-job lists an interview under the candidate's
        # own id) are written 'sent' by reconcile_with_pairbot and reported as
        # already launched; proven-but-unwritten ones (its DB write failed) are
        # written by the loop below, in this call's own transaction. The rest
        # — twins of a person this job already interviews, other-job
        # duplicates, unverifiable ones — are failed + blocked below.
        dup_proven: Dict[str, str] = {}
        dup_unrecorded: Dict[str, str] = {}
        dup_blocked: Dict[str, str] = {}
        dup_items: Dict[str, Dict[str, Any]] = {}
        if is_success and isinstance(response_data, dict) and not request.dry_run:
            try:
                _created_ids = {
                    str(item.get("source_candidate_id") or "").strip()
                    for item in (response_data.get("data") or [])
                    if isinstance(item, dict) and item.get("interview_id")
                }
                _dups = _map_duplicate_interviews(
                    response_data.get("failed_interviews"), payload_obj,
                    request.real_candidate_ids, _created_ids,
                )
                dup_items = {
                    _cid: {k: item.get(k) for k in ("error_type", "error", "existing_interview_id", "resume_index")}
                    for _cid, item in _dups.items()
                }
                dup_existing = {
                    _cid: str(item.get("existing_interview_id") or "").strip() for _cid, item in _dups.items()
                }
                if dup_existing:
                    rec = await reconcile_with_pairbot(
                        (str(job_id_from_payload or ""), str(payload_obj.get("jd", {}).get("jobdiva_id") or "")),
                        list(dup_existing),
                        existing_interview_ids=dup_existing,
                    )
                    dup_proven = dict(rec.get("sent") or {})
                    dup_unrecorded = dict(rec.get("unrecorded") or {})
                    _missing = set(rec.get("missing") or [])
                    _twins = set(rec.get("duplicate_of") or {})
                    for _cid in dup_existing:
                        if _cid in dup_proven or _cid in dup_unrecorded:
                            continue
                        if _cid in _twins:
                            dup_blocked[_cid] = LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB
                        elif _cid in _missing:
                            dup_blocked[_cid] = LAUNCH_BLOCK_DUPLICATE_OTHER_JOB
                        else:
                            dup_blocked[_cid] = LAUNCH_BLOCK_DUPLICATE_UNVERIFIED
                    logger.warning(
                        "pairbot_duplicate_interviews bulk_id=%s duplicates=%d proven_this_job=%d "
                        "unrecorded=%d blocked=%d",
                        response_data.get("bulk_id", "?"), len(dup_existing), len(dup_proven),
                        len(dup_unrecorded), len(dup_blocked),
                    )
            except Exception as _dup_err:  # noqa: BLE001
                # Mapping failed: unplaced rows fall through as plain failures,
                # as before. Anything reconcile already proved stays proven
                # (it is written 'sent'; the loop must not overwrite it).
                logger.error("pairbot_duplicate_mapping_failed err=%s", _dup_err)
                dup_blocked = {}

        # Save audit log for each candidate
        conn = _get_db_connection()
        cur = conn.cursor()

        def _write_candidate_engage_status(
            candidate_id: str,
            status_value: str,
            job_id_value: str,
            interview_id_value: str = "",
            response_fragment: Optional[Dict[str, Any]] = None,
            block_reason: Optional[str] = None,
        ) -> None:
            """Write-through status sync for rank-list source of truth.

            Rank-list reads engage_status from sourced_candidates.data, so we
            must update that blob whenever engage send state changes.
            ``block_reason`` also stamps data.engage_block_reason (never
            re-posted by any launch path).
            """
            now_iso = datetime.now(timezone.utc).isoformat()
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data =
                    jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                COALESCE(data, '{}'::jsonb),
                                '{engage_status}',
                                to_jsonb(%s::text),
                                true
                            ),
                            '{engage_updated_at}',
                            to_jsonb(%s::text),
                            true
                        ),
                        '{engage_interview_id}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = %s
                  AND (
                    jobdiva_id = %s
                    OR jobdiva_id = %s
                  )
                """,
                (
                    status_value,
                    now_iso,
                    interview_id_value,
                    candidate_id,
                    str(job_id_value or ""),
                    str(job_id_from_payload or ""),
                ),
            )

            # Preserve last external response snippet for support/debugging.
            if response_fragment is not None:
                cur.execute(
                    """
                    UPDATE sourced_candidates
                    SET data = jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{engage_last_response}',
                            %s::jsonb,
                            true
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s
                      AND (
                        jobdiva_id = %s
                        OR jobdiva_id = %s
                      )
                    """,
                    (
                        json.dumps(response_fragment),
                        candidate_id,
                        str(job_id_value or ""),
                        str(job_id_from_payload or ""),
                    ),
                )

            if block_reason:
                cur.execute(
                    """
                    UPDATE sourced_candidates
                    SET data = jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{engage_block_reason}',
                            to_jsonb(%s::text),
                            true
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s
                      AND (
                        jobdiva_id = %s
                        OR jobdiva_id = %s
                      )
                    """,
                    (
                        block_reason,
                        candidate_id,
                        str(job_id_value or ""),
                        str(job_id_from_payload or ""),
                    ),
                )

        interview_results = []

        if is_success and isinstance(response_data, dict):
            # Extract interview data from response
            data_list = response_data.get("data", [])
            if not isinstance(data_list, list):
                data_list = [response_data] if response_data else []

            # Index Pairbot's creation_completed data[] by source_candidate_id
            # (PAI-155: Pairbot always echoes it). candidate_email is never
            # present in Pairbot response items, so the email index is only
            # populated when Pairbot happens to include it (legacy/fallback).
            interview_by_source_id: Dict[str, Dict[str, Any]] = {}
            interview_by_email: Dict[str, Dict[str, Any]] = {}
            for item in data_list:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("source_candidate_id") or "").strip()
                if src and src not in interview_by_source_id:
                    interview_by_source_id[src] = item
                item_email = str(item.get("candidate_email") or "").lower().strip()
                if item_email and item_email not in interview_by_email:
                    interview_by_email[item_email] = item

            # Log any interviews Pairbot explicitly failed on its side.
            failed_interviews_from_pairbot = response_data.get("failed_interviews") or []
            # A candidate Pairbot named in failed_interviews was rejected (a
            # definite 'failed', retryable). One it is silent about — neither
            # created in data[] nor failed — may well have an interview whose
            # echo did not match: parked 'processing' (in flight for every
            # launch path, never re-posted) until Pairbot's by-job record
            # settles it (settle_parked_legacy_rows / the session's settle).
            pairbot_rejected_ids = _pairbot_rejected_ids(
                failed_interviews_from_pairbot, payload_obj, request.real_candidate_ids,
            )
            if failed_interviews_from_pairbot:
                failed_ids = [item.get("candidate_id") or item.get("source_candidate_id") or "?" for item in failed_interviews_from_pairbot]
                logger.warning(
                    "pairbot_failed_interviews bulk_id=%s count=%d sample=%s",
                    response_data.get("bulk_id", "?"),
                    len(failed_interviews_from_pairbot),
                    failed_ids[:5],
                )

            # Build email lookup from the sent payload resumes (keyed by
            # source_candidate_id == candidate_id).  Used as email fallback
            # when Pairbot response items don't carry source_candidate_id.
            # IMPORTANT: source_candidate_id in generate-payload is always set
            # to the candidate's own candidate_id, so we look up by candidate_id
            # directly — this is position-independent and alignment-safe after
            # idempotency/exclusion filtering.
            payload_resumes = payload_obj.get("resumes") or []
            candidate_id_to_email: Dict[str, str] = {
                str(r.get("source_candidate_id") or "").strip(): str(r.get("email") or "").lower().strip()
                for r in payload_resumes
                if isinstance(r, dict) and r.get("source_candidate_id")
            }

            # ── TRIGGER PROVISIONING (JobDiva Application) ─────────────
            # Background-fire batch provisioning so it doesn't block the
            # HTTP response. The batch function fetches the applicants list
            # ONCE and creates all missing candidates concurrently under
            # _PROVISION_CONCURRENCY, which is far more efficient than the
            # old per-candidate approach (N JobDiva round-trips → 1).
            # A launch session (provision_inline=False) runs it itself, awaited
            # and claim-stamped (provision_sent_candidates). Rows Pairbot held
            # as a duplicate (another job's, or a twin of a person this job
            # already interviews) were not launched here: not provisioned.
            _batch_cids = [c for c in request.real_candidate_ids if c not in dup_blocked]
            if _batch_cids and provision_inline:
                _batch_job_id = job_id_from_payload

                async def _run_batch_provisioning() -> None:
                    async with _trace_span("jobdiva_provisioning", n=len(_batch_cids)):
                        await _provision_batch_to_jobdiva(_batch_cids, _batch_job_id)

                _spawn_task(_run_batch_provisioning())

            for idx, candidate_id in enumerate(request.real_candidate_ids):
                if str(candidate_id) in dup_proven:
                    # Pairbot already has this person's interview for THIS job
                    # (by-job proof); reconcile wrote 'sent' + id + audit row.
                    skipped_already_sent.append(candidate_id)
                    continue
                if str(candidate_id) in dup_unrecorded:
                    # Proven the same way, but reconcile's write failed: record
                    # it here ('sent' + the existing id + an audit row).
                    _existing_iid = dup_unrecorded[str(candidate_id)]
                    _job_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")
                    _dup_item = dict(dup_items.get(str(candidate_id)) or {}, reconciled_from="pairbot_by_job")
                    cur.execute("""
                        INSERT INTO engage_interview_audit
                            (candidate_id, jobdiva_id, interview_id, payload, response, status)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        candidate_id,
                        _job_resolved,
                        _existing_iid,
                        json.dumps(payload_obj),
                        json.dumps(_dup_item),
                        "Initiated",
                    ))
                    _write_candidate_engage_status(
                        candidate_id=candidate_id,
                        status_value="sent",
                        job_id_value=_job_resolved,
                        interview_id_value=_existing_iid,
                        response_fragment=_dup_item,
                    )
                    skipped_already_sent.append(candidate_id)
                    continue
                if str(candidate_id) in dup_blocked:
                    _reason = dup_blocked[str(candidate_id)]
                    _dup_item = dup_items.get(str(candidate_id)) or {}
                    _job_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")
                    cur.execute("""
                        INSERT INTO engage_interview_audit
                            (candidate_id, jobdiva_id, interview_id, payload, response, status)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        candidate_id,
                        _job_resolved,
                        "",
                        json.dumps(payload_obj),
                        json.dumps(_dup_item),
                        "duplicate_this_job" if _reason == LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB
                        else "duplicate_other_job",
                    ))
                    _write_candidate_engage_status(
                        candidate_id=candidate_id,
                        status_value="failed",
                        job_id_value=_job_resolved,
                        interview_id_value="",
                        response_fragment=_dup_item,
                        block_reason=_reason,
                    )
                    excluded_records.append({
                        "candidate_id": str(candidate_id),
                        "name": "",
                        "reason": _launch_block_reason_text(_reason),
                    })
                    continue
                # Direct lookup by candidate_id == source_candidate_id (always
                # identical — see generate-payload).  Falls back to email index
                # for legacy Pairbot responses that include candidate_email.
                # NOTE: positional fallback (data_list[idx]) is intentionally
                # absent — Pairbot may omit skipped candidates from data[], so a
                # position-based grab would assign the wrong interview_id.
                submitted_source_id = str(candidate_id)
                submitted_email = candidate_id_to_email.get(str(candidate_id), "")
                interview_info: Dict[str, Any] = {}
                if submitted_source_id in interview_by_source_id:
                    interview_info = interview_by_source_id.pop(submitted_source_id, {}) or {}
                elif submitted_email:
                    interview_info = interview_by_email.pop(submitted_email, {}) or {}

                interview_id = str(interview_info.get("interview_id") or "")
                candidate_name = interview_info.get("candidate_name", "")
                candidate_email = interview_info.get("candidate_email", submitted_email)

                if not interview_id:
                    # Equal lengths + no match = keying broke for the whole batch, not a skip.
                    _log = logger.error if len(data_list) == len(request.real_candidate_ids) else logger.warning
                    _log(
                        "engagement_no_interview_match candidate_id=%s submitted_source_id=%s submitted_email=%s response_data_count=%d request_count=%d",
                        candidate_id,
                        submitted_source_id,
                        submitted_email,
                        len(data_list),
                        len(request.real_candidate_ids),
                    )

                # Extract job_id from payload (prefer reference jobdiva_id for UI consistency)
                job_id_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")

                unconfirmed = (
                    not interview_id
                    and not request.dry_run
                    and str(candidate_id) not in pairbot_rejected_ids
                )
                if interview_id:
                    audit_status, engage_status = "Initiated", "sent"
                elif unconfirmed:
                    audit_status, engage_status = "unconfirmed", "processing"
                else:
                    audit_status, engage_status = "failed", "failed"

                cur.execute("""
                    INSERT INTO engage_interview_audit
                        (candidate_id, jobdiva_id, interview_id, candidate_name, candidate_email, payload, response, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    candidate_id,
                    job_id_resolved,
                    interview_id,
                    candidate_name,
                    candidate_email,
                    json.dumps(payload_obj),
                    json.dumps(interview_info),
                    audit_status
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value=engage_status,
                    job_id_value=job_id_resolved,
                    interview_id_value=interview_id,
                    response_fragment=interview_info,
                )

                interview_results.append({
                    "candidate_id": candidate_id,
                    "interview_id": interview_id,
                    "candidate_name": candidate_name,
                    "candidate_email": candidate_email,
                    "links": interview_info.get("links", {}),
                    "session_token": interview_info.get("session_token", ""),
                    "created_at": interview_info.get("created_at", ""),
                    # Parked 'processing': Pairbot was silent about this one.
                    **({"unconfirmed": True} if unconfirmed else {}),
                })

            # Any items left in interview_by_source_id were created by Pairbot
            # but couldn't be matched back to a submitted candidate_id.  This
            # can happen if source_candidate_id was not echoed or was mutated.
            if interview_by_source_id:
                logger.warning(
                    "pairbot_unmatched_created_interviews bulk_id=%s count=%d orphaned_ids=%s",
                    response_data.get("bulk_id", "?"),
                    len(interview_by_source_id),
                    list(interview_by_source_id.keys())[:10],
                )
        else:
            # Still log the failed attempt
            for candidate_id in request.real_candidate_ids:
                job_id_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")
                cur.execute("""
                    INSERT INTO engage_interview_audit
                        (candidate_id, jobdiva_id, payload, response, status)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    candidate_id,
                    job_id_resolved,
                    json.dumps(payload_obj),
                    json.dumps(response_data),
                    "failed"
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value="failed",
                    job_id_value=job_id_resolved,
                    interview_id_value="",
                    response_fragment=response_data if isinstance(response_data, dict) else {"response": response_data},
                )

        conn.commit()
        cur.close()
        conn.close()

        # T2 (job's first interview created) for every launch path; the stamp
        # is COALESCE'd in launch_timing, so per-batch calls are idempotent.
        if not request.dry_run and (
            any(r.get("interview_id") for r in interview_results) or dup_unrecorded
        ):
            _t2_job_key = (
                payload_obj.get("jd", {}).get("jobdiva_id")
                or payload_obj.get("jd", {}).get("job_id", "")
                or job_id_from_payload
            )
            try:
                await asyncio.to_thread(_stamp_first_launch, str(_t2_job_key or ""))
            except Exception as _t2_err:  # noqa: BLE001
                logger.warning("launch T2 stamp failed job=%s: %s", _t2_job_key, _t2_err)

        if is_success:
            # People this call actually created an interview for — not every
            # row it answered for (a 'failed' / parked row is not a launch).
            _created_count = sum(1 for r in interview_results if r.get("interview_id"))
            # ── Applicant sync + launch email (non-blocking) ───────────────
            # Manual rankings Screen sends create the interview only; wizard
            # Launch/re-source flows pass notify_recruiters=True. The
            # first-launch effects (applicant sync, job-posting email) go
            # through the fire-once ledger like every other launch path.
            # (Pre-fix, this gate was `is_initial_launch or dry_run` — but
            # dry_run also skipped the pairbot call, so wizard launches never
            # actually created interviews.)
            if request.is_initial_launch or request.notify_recruiters:
                send_job_posting = (
                    request.send_job_posting_email
                    if request.send_job_posting_email is not None
                    else request.is_initial_launch
                )
                fire_launch_side_effects(
                    job_id_from_payload,
                    sent_count=_created_count,
                    notify_recruiters=bool(request.notify_recruiters),
                    send_job_posting_email=bool(send_job_posting),
                    is_initial_launch=bool(request.is_initial_launch),
                    app_base_url=request.app_base_url,
                    launch_started_at=_core_started_at,
                )
            try:
                from core.newrelic import record_custom_event
                record_custom_event("EngageSendBulkInterview", {
                    "success": True,
                    "bulk_id": response_data.get("bulk_id"),
                    "count": _created_count,
                    "answered_count": len(interview_results),
                    "skipped_count": len(skipped_already_sent),
                })
            except Exception:
                pass
            return {
                "success": True,
                "message": "Interview(s) sent successfully",
                "bulk_id": response_data.get("bulk_id"),
                "data": interview_results,
                "skipped_already_sent": skipped_already_sent,
                "excluded_candidates": excluded_records,
                "employer_unverified": unverified_employer_records,
                "raw_response": response_data
            }
        else:
            try:
                from core.newrelic import record_custom_event
                record_custom_event("EngageSendBulkInterview", {
                    "success": False,
                    "status_code": response.status_code,
                })
            except Exception:
                pass
            return {
                "success": False,
                "message": _extract_pair_error_message(response_data, response.status_code),
                "data": [],
                "skipped_already_sent": skipped_already_sent,
                "excluded_candidates": excluded_records,
                "employer_unverified": unverified_employer_records,
                "raw_response": response_data
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ send-bulk-interview failed: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Server error: {str(e)}",
            "data": []
        }
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

@router.get("/engage/bulk-status/stream")
async def stream_engagement_status(
    request: Request,
    bulk_id: Optional[str] = None,
    launch_id: Optional[str] = None,
):
    """Proxy Pairbot's per-bulk SSE stream, or (Fix 3) replay the progress
    events of an async launch keyed by `launch_id`.

    The launch_id branch requires an authenticated user who requested the
    launch, is an admin, or otherwise has access to the launch's job. The
    bulk_id branch is unchanged."""
    from fastapi.responses import StreamingResponse
    import httpx

    _nr_suppress_apdex()
    if launch_id:
        await _authorize_launch_stream(request, launch_id)
        return StreamingResponse(
            _launch_progress_stream(launch_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    if not bulk_id:
        raise HTTPException(status_code=400, detail="bulk_id or launch_id is required")

    external_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews/{bulk_id}/stream"

    async def proxy_stream():
        async with httpx.AsyncClient() as client:
            try:
                async with client.stream("GET", external_url, timeout=None) as response:
                    async for chunk in response.aiter_bytes():
                        yield chunk
            except Exception as e:
                logger.error(f"Error proxying SSE stream for bulk_id {bulk_id}: {e}")
                yield b"data: {\"status\": \"error\"}\n\n"

    return StreamingResponse(proxy_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Backend-owned Launch PAIR orchestration (single call + SSE)
# ---------------------------------------------------------------------------
class LaunchRequest(BaseModel):
    """IDs-only launch request. Résumés do NOT ride here — they are persisted
    separately via /candidates/save — so this body stays small and is not
    nginx body-limit sensitive."""
    job_id: str
    candidate_ids: List[str]
    is_initial_launch: bool = False
    notify_recruiters: bool = False
    send_job_posting_email: Optional[bool] = None
    app_base_url: str = ""
    batch_size: Optional[int] = None
    # Fix 3: frontend `launchAsync` opt-in. Honoured only when the server's
    # LAUNCH_ASYNC flag is on; otherwise the SSE response is returned as before.
    async_launch: bool = False
    # Optional link to a server launch session (launch timings for the legacy
    # client-driven flow). ASYNC PATH ONLY: stored on
    # launch_runs.launch_session_id, and launch_runs rows exist only for async
    # launches. The default SSE path (LAUNCH_ASYNC off) has no run row to
    # carry it, so there it is only logged. No other behaviour change.
    launch_session_id: Optional[str] = None
    # Recruiter warm handoff: see SendBulkInterviewRequest.
    recruiter_handoff: bool = False
    handoff_timeout_sec: Optional[int] = Field(default=None, ge=60, le=1800)


# Backend batch size for the launch orchestrator's per-batch Pairbot calls.
# Decoupled from the FE /candidates/save batch (which IS nginx body-limit
# bound because it carries resume_text): the /engage/launch request carries
# only candidate_ids, so this is tuned for Pairbot throughput + failure
# granularity, not payload size.
_LAUNCH_PAIRBOT_BATCH_SIZE = 75
_LAUNCH_BATCH_DELAY_SECONDS = 0.35


def _launch_flag(name: str, default: Any) -> Any:
    """Read a launch flag from core.config at call time (monkeypatchable)."""
    try:
        import core.config as _cfg
        return getattr(_cfg, name, default)
    except Exception:  # noqa: BLE001
        return default


# --- Fix 1: employer gate once per launch ----------------------------------

async def _precompute_launch_signals(
    job_id: str, candidate_ids: List[str]
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Resolve employer signals + resume freshness ONCE for the whole launch.

    Returns {candidate_id: signals} with a key for EVERY row it loaded (empty
    dict when nothing was resolved), so per-batch gates know which ids are
    covered. Returns None when the rows could not be loaded — batches then
    fall back to the per-batch pass. A resolution failure fails open exactly
    like the per-batch gate: covered ids gate on stored signals.
    """
    def _load() -> List[Dict[str, Any]]:
        conn = _get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT job_id, jobdiva_id FROM monitored_jobs
                WHERE job_id = %s OR jobdiva_id = %s
                ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
                LIMIT 1
                """,
                (job_id, job_id),
            )
            jrow = cur.fetchone()
            jid = str((jrow[0] if jrow else None) or job_id)
            jdid = str((jrow[1] if jrow else None) or "")
            cur.execute(
                """
                SELECT candidate_id, data, name, headline, resume_text, source
                FROM sourced_candidates
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                """,
                (list(candidate_ids), jid, jdid),
            )
            rows = cur.fetchall() or []
            signals = _fetch_stored_employer_signals(conn, [str(r[0]) for r in rows])
            out: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                cdata = r[1] or {}
                if isinstance(cdata, str):
                    try:
                        cdata = json.loads(cdata)
                    except Exception:
                        cdata = {}
                cdata = _merge_employer_signals(
                    cdata, headline=str(r[3] or ""), signals=signals.get(str(r[0]))
                )
                cdata.setdefault("candidate_id", str(r[0]))
                if r[4] and not cdata.get("resume_text"):
                    cdata["resume_text"] = str(r[4])
                if r[5] and not cdata.get("source"):
                    cdata["source"] = str(r[5])
                out.setdefault(str(r[0]), cdata)
            cur.close()
            return list(out.values())
        finally:
            conn.close()

    try:
        cands = await asyncio.to_thread(_load)
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch gate preload failed for job %s (per-batch fallback): %s", job_id, exc)
        return None

    result: Dict[str, Dict[str, Any]] = {str(c["candidate_id"]): {} for c in cands}
    if not cands:
        return result
    try:
        from services.employer_resolution import resolve_employer_signals
        try:
            import core.sourcing_config as _sc
            conc = int(getattr(_sc, "EMPLOYER_RESOLUTION_CONCURRENCY", 6) or 6)
        except Exception:  # noqa: BLE001
            conc = 6
        timeout = min(600.0, 30.0 + 2.0 * len(cands) / max(1, conc))
        resolved = await asyncio.wait_for(resolve_employer_signals(cands), timeout=timeout) or {}
        for cid, sig in resolved.items():
            if cid in result and sig:
                result[cid] = dict(sig)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "employer resolution failed for job %s (gating on stored signals): %s", job_id, exc
        )
    try:
        from services.employer_resolution import stamp_resume_freshness
        merged = [
            _merge_employer_signals(c, signals=result.get(str(c["candidate_id"])) or None)
            for c in cands
        ]
        await asyncio.wait_for(stamp_resume_freshness(merged), timeout=20.0)
        for c in merged:
            ts = c.get("resume_updated_at")
            if ts:
                result[str(c["candidate_id"])]["resume_updated_at"] = ts
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume-freshness check failed for job %s: %s", job_id, exc)
    return result


# --- Fix 3: launch_runs / launch_batches -------------------------------------

_LAUNCH_TABLES_READY = {"done": False}

# Statements mirror migrations/20261009_pair_launch_pipeline.sql. Run once at
# startup (main.py lifespan -> init_launch_tables), never per request.
_LAUNCH_DDL = (
    """
    CREATE TABLE IF NOT EXISTS launch_runs (
        id uuid PRIMARY KEY,
        job_id text NOT NULL,
        requested_by text,
        status text NOT NULL,
        events jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz DEFAULT now(),
        finished_at timestamptz,
        last_event_at timestamptz DEFAULT now()
    )
    """,
    "ALTER TABLE launch_runs ADD COLUMN IF NOT EXISTS last_event_at timestamptz DEFAULT now()",
    # Server launch sessions: the session a run belongs to. Runs a session
    # creates use status 'session', which the stale sweep and _launch_finalize
    # (both `WHERE status = 'running'`) never touch.
    "ALTER TABLE launch_runs ADD COLUMN IF NOT EXISTS launch_session_id uuid NULL",
    """
    CREATE INDEX IF NOT EXISTS ix_launch_runs_running
    ON launch_runs (last_event_at) WHERE status = 'running'
    """,
    """
    CREATE TABLE IF NOT EXISTS launch_events (
        launch_id uuid NOT NULL REFERENCES launch_runs(id) ON DELETE CASCADE,
        seq int NOT NULL,
        payload jsonb NOT NULL,
        created_at timestamptz DEFAULT now(),
        PRIMARY KEY (launch_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS launch_batches (
        id uuid PRIMARY KEY,
        launch_id uuid REFERENCES launch_runs(id),
        idx int,
        candidate_ids text[],
        pairbot_bulk_id text,
        status text,
        error text,
        creation_event jsonb,
        idempotency_key text UNIQUE,
        updated_at timestamptz DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_launch_batches_bulk ON launch_batches (pairbot_bulk_id)",
    "CREATE INDEX IF NOT EXISTS ix_launch_batches_launch ON launch_batches (launch_id)",
)


def _ensure_launch_tables() -> None:
    """CREATE IF NOT EXISTS for the async-launch tables."""
    if _LAUNCH_TABLES_READY["done"]:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for stmt in _LAUNCH_DDL:
                cur.execute(stmt)
            conn.commit()
    _LAUNCH_TABLES_READY["done"] = True


async def init_launch_tables() -> None:
    """Startup hook (main.py lifespan). Fail-soft: the caller logs errors."""
    await asyncio.to_thread(_ensure_launch_tables)


def _launch_batch_idempotency_key(launch_id: str, idx: int, candidate_ids: List[str]) -> str:
    raw = f"{launch_id}:{idx}:{','.join(sorted(str(c) for c in candidate_ids))}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _launch_db(sql: str, params: tuple, fetch: bool = False) -> Any:
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        cur.close()
        return rows
    finally:
        conn.close()


def _launch_batch_set_bulk(idem_key: str, bulk_id: str, *, launch_id: Optional[str] = None,
                           idx: Optional[int] = None, candidate_ids: Optional[List[str]] = None) -> None:
    """Record the Pairbot bulk_id for an async-launch batch (fail-open)."""
    try:
        _launch_db(
            """
            INSERT INTO launch_batches (id, launch_id, idx, candidate_ids, pairbot_bulk_id,
                                        status, idempotency_key, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'creating', %s, now())
            ON CONFLICT (idempotency_key) DO UPDATE
              SET pairbot_bulk_id = EXCLUDED.pairbot_bulk_id,
                  status = 'creating', updated_at = now()
            """,
            (str(uuid.uuid4()), launch_id, idx, list(candidate_ids or []), bulk_id, idem_key),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch_batch_set_bulk failed key=%s: %s", idem_key, exc)


async def _poll_launch_batch_creation(bulk_id: str, *, timeout_seconds: float = 600.0) -> Dict[str, Any]:
    """Jittered DB poll for the creation status the Pairbot webhook writes."""
    import random
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        await asyncio.sleep(2.0 + random.random() * 2.0)
        try:
            rows = await asyncio.to_thread(
                _launch_db,
                "SELECT status, creation_event, error FROM launch_batches "
                "WHERE pairbot_bulk_id = %s ORDER BY updated_at DESC LIMIT 1",
                (bulk_id,),
                True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("launch batch poll failed bulk_id=%s: %s", bulk_id, exc)
            continue
        if not rows:
            continue
        status, event, error = rows[0]
        if status == "creation_completed":
            if isinstance(event, str):
                try:
                    event = json.loads(event)
                except Exception:
                    event = None
            return event if isinstance(event, dict) else {"status": status}
        if status == "creation_failed":
            raise RuntimeError(error or f"Pairbot creation_failed for bulk_id={bulk_id}")
    raise TimeoutError(f"Timed out waiting for Pairbot creation webhook bulk_id={bulk_id}")


async def _wait_for_creation_webhook_or_sse(bulk_id: str, *, timeout_seconds: float = 600.0) -> Dict[str, Any]:
    """Race the webhook-backed poll against the legacy SSE wait. The first
    success wins; if one fails, the other gets the rest of the budget."""
    sse = asyncio.ensure_future(_wait_for_pairbot_creation(bulk_id, timeout_seconds=timeout_seconds))
    hook = asyncio.ensure_future(_poll_launch_batch_creation(bulk_id, timeout_seconds=timeout_seconds))
    pending = {sse, hook}
    first_error: Optional[BaseException] = None
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is None:
                    return t.result()
                if first_error is None:
                    first_error = t.exception()
        assert first_error is not None
        raise first_error
    finally:
        for t in (sse, hook):
            if not t.done():
                t.cancel()
        await asyncio.gather(sse, hook, return_exceptions=True)


_LAUNCH_TERMINAL_STATUSES = ("completed", "failed", "aborted")


def _launch_stale_minutes() -> int:
    try:
        return max(1, int(_launch_flag("LAUNCH_STALE_MINUTES", 20) or 20))
    except (TypeError, ValueError):
        return 20


def _launch_stream_max_seconds() -> float:
    try:
        return float(_launch_flag("LAUNCH_STREAM_MAX_SECONDS", 45 * 60) or 45 * 60)
    except (TypeError, ValueError):
        return 45 * 60.0


def _launch_append_event(launch_id: str, seq: int, evt: Dict[str, Any]) -> None:
    """Append one event row and bump launch_runs.last_event_at (one statement)."""
    _launch_db(
        """
        WITH ins AS (
            INSERT INTO launch_events (launch_id, seq, payload)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (launch_id, seq) DO NOTHING
            RETURNING 1
        )
        UPDATE launch_runs SET last_event_at = now() WHERE id = %s
        """,
        (launch_id, seq, json.dumps(evt), launch_id),
    )


def _launch_finalize(launch_id: str, status: str) -> None:
    """Write the terminal status, only if the run is still 'running'."""
    _launch_db(
        "UPDATE launch_runs SET status = %s, finished_at = now() "
        "WHERE id = %s AND status = 'running'",
        (status, launch_id),
    )


def _sweep_stale_launch_runs(launch_id: Optional[str] = None) -> int:
    """Mark 'running' launches with no event for LAUNCH_STALE_MINUTES as
    'failed' (worker died / was killed without writing a final status).
    Scoped to one launch when `launch_id` is given. Returns rows swept."""
    sql = (
        "UPDATE launch_runs SET status = 'failed', finished_at = now() "
        "WHERE status = 'running' "
        "AND COALESCE(last_event_at, created_at) < now() - make_interval(mins => %s)"
    )
    params: tuple = (_launch_stale_minutes(),)
    if launch_id:
        sql += " AND id = %s"
        params = params + (launch_id,)
    rows = _launch_db(sql + " RETURNING id", params, True)
    n = len(rows or [])
    if n:
        logger.warning("launch_runs stale sweep marked %d run(s) failed", n)
    return n


# launch_runs.status of a run a server launch session drives. The session owns
# its lifecycle; the stale sweep and _launch_finalize only ever touch 'running'.
LAUNCH_RUN_SESSION_STATUS = "session"


def _normalise_launch_session_id(value: Any) -> Optional[str]:
    """A launch session id as a canonical uuid string, or None (never raises)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        logger.warning("ignoring malformed launch_session_id=%r", raw[:64])
        return None


def register_session_launch_run(
    launch_id: str,
    job_id: str,
    *,
    requested_by: Optional[str] = None,
    launch_session_id: Optional[str] = None,
) -> None:
    """Create the launch_runs row a session-driven ``run_launch`` uses as its
    ``launch_id`` (launch_batches has a foreign key to it). Status 'session':
    never swept, never finalized by the legacy code. Idempotent on id. Sync."""
    _launch_db(
        "INSERT INTO launch_runs (id, job_id, requested_by, status, last_event_at, launch_session_id) "
        "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (id) DO NOTHING",
        (
            str(launch_id), str(job_id),
            (requested_by or "").strip().lower() or None,
            LAUNCH_RUN_SESSION_STATUS,
            _normalise_launch_session_id(launch_session_id),
        ),
    )


async def _authorize_launch_stream(request: Request, launch_id: str) -> None:
    """Requester, admins, or users with access to the launch's job only."""
    user = await asyncio.to_thread(get_current_user, request, None)
    try:
        rows = await asyncio.to_thread(
            _launch_db,
            "SELECT job_id, requested_by FROM launch_runs WHERE id = %s",
            (launch_id,),
            True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch auth lookup failed launch_id=%s: %s", launch_id, exc)
        raise HTTPException(status_code=503, detail="Launch lookup failed")
    if not rows:
        raise HTTPException(status_code=404, detail="Launch not found")
    job_id, requested_by = rows[0]
    if user.is_admin:
        return
    if requested_by and str(requested_by).strip().lower() == (user.email or "").strip().lower():
        return
    await asyncio.to_thread(_verify_job_access_by_id, str(job_id), user)


async def _launch_progress_stream(launch_id: str):
    """SSE replay of an async launch's events (launch_events, by seq).

    Each connection replays from seq 0 (the frontend skips already-seen
    events by count) and then reads only rows with seq > last seen. Ends on a
    terminal status, a stale run (swept to 'failed'), or after
    LAUNCH_STREAM_MAX_SECONDS."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _launch_stream_max_seconds()
    last_seq = -1
    last_type: Optional[str] = None
    idle = 0
    while True:
        if loop.time() >= deadline:
            yield f"data: {json.dumps({'type': 'error', 'message': 'launch progress stream timed out; refresh to check status'})}\n\n"
            return
        try:
            # Status first: events are written before the final status, so a
            # terminal status read here means every event is already visible.
            rows = await asyncio.to_thread(
                _launch_db,
                "SELECT status, COALESCE(last_event_at, created_at) "
                "< now() - make_interval(mins => %s) FROM launch_runs WHERE id = %s",
                (_launch_stale_minutes(), launch_id),
                True,
            )
            events = []
            if rows:
                events = await asyncio.to_thread(
                    _launch_db,
                    "SELECT seq, payload FROM launch_events "
                    "WHERE launch_id = %s AND seq > %s ORDER BY seq",
                    (launch_id, last_seq),
                    True,
                ) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch progress read failed launch_id=%s: %s", launch_id, exc)
            rows = None
            events = []
        if not rows:
            idle += 1
            if idle > 30:
                yield f"data: {json.dumps({'type': 'error', 'message': 'launch not found'})}\n\n"
                return
            await asyncio.sleep(1.0)
            continue
        status, stale = rows[0]
        for seq, ev in events:
            if isinstance(ev, str):
                ev = json.loads(ev)
            last_seq = seq
            last_type = ev.get("type") if isinstance(ev, dict) else None
            yield f"data: {json.dumps(ev)}\n\n"
        if status == "running" and stale:
            try:
                if await asyncio.to_thread(_sweep_stale_launch_runs, launch_id):
                    status = "failed"
            except Exception as exc:  # noqa: BLE001
                logger.warning("launch stale sweep failed launch_id=%s: %s", launch_id, exc)
        if status in _LAUNCH_TERMINAL_STATUSES:
            if status == "failed" and last_type not in ("error", "done"):
                yield f"data: {json.dumps({'type': 'error', 'message': 'launch did not complete'})}\n\n"
            return
        yield ": keepalive\n\n"
        await asyncio.sleep(1.5)


# --- Launch side effects -----------------------------------------------------

def _decide_launch_effects(job_id: str, **kwargs: Any) -> Tuple[bool, bool, bool]:
    """services.launch_effects_ledger.decide (a seam the tests stub)."""
    from services import launch_effects_ledger

    return launch_effects_ledger.decide(job_id, **kwargs)


def fire_launch_side_effects(
    job_id: str,
    *,
    sent_count: int,
    notify_recruiters: bool,
    send_job_posting_email: bool,
    is_initial_launch: bool,
    app_base_url: str,
    launch_started_at: Optional[datetime] = None,
) -> bool:
    """The once-per-launch side effects, spawned as tracked background tasks:
    the applicant sync (initial launch only) and the recruiter launch email
    (initial launch or ``notify_recruiters``), whose job-posting part goes out
    only when ``send_job_posting_email`` — the caller decides that (today:
    requested AND the launch ran clean: not aborted, no failed batch).

    The FIRST-launch effects (applicant sync, job-posting email) are then
    checked against the job's fire-once ledger (services/launch_effects_ledger):
    they fire only for the launch that produced the job's first interview,
    once per job version, whatever the browser's flags say. The recruiter
    email still goes out for every launch that sent someone, worded as a
    relaunch when the job's first interview predates ``launch_started_at``.

    Nothing fires when ``sent_count <= 0``. The caller owns "once" per launch:
    /engage/launch keeps an in-memory flag; a launch session claims a DB guard
    first. Returns True only when every requested effect was spawned (False on
    an error, which is logged, never raised). Must be called with a running
    event loop."""
    if sent_count <= 0:
        return False
    if not (is_initial_launch or notify_recruiters):
        return False
    initial, job_posting, relaunch = _decide_launch_effects(
        job_id,
        is_initial_launch=bool(is_initial_launch),
        send_job_posting_email=bool(send_job_posting_email),
        launch_started_at=launch_started_at,
    )
    fired = False
    try:
        if initial:
            logger.info("launch: initial launch for job %s -> applicant sync", job_id)
            try:
                _spawn_task(auto_assign_service.synchronize_job_applicants(job_id))
            except Exception:
                from services import launch_effects_ledger

                launch_effects_ledger.release(job_id, launch_effects_ledger.INITIAL)
                raise
            fired = True
        _spawn_task(
            _send_pair_launch_email(
                job_id=job_id,
                candidate_count=sent_count,
                send_job_posting=bool(job_posting),
                app_base_url=app_base_url,
                relaunch=bool(relaunch),
            )
        )
        fired = True
    except Exception as _se_err:
        logger.warning("launch: side-effect firing failed: %s", _se_err)
        return False
    return fired


# --- Fix 2: one launch batch -----------------------------------------------

async def _run_one_batch(
    idx: int,
    batch: List[str],
    *,
    request: "LaunchRequest",
    precomputed_signals: Optional[Dict[str, Dict[str, Any]]],
    abort_flag: Dict[str, bool],
    launch_id: Optional[str] = None,
    claimed_session_id: Optional[str] = None,
    provision_inline: bool = True,
    bulk_id_for: Optional[Callable[..., Any]] = None,
    recruiter_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one launch batch and RETURN its outcome; never mutates shared
    launch state (only the consumer aggregates). Keys:

      status            completed | failed | aborted (409) | not_run (after abort)
      event             SSE event dict for this batch (None for aborted/not_run)
      sent, no_interview, already_sent, excluded_count, employer_unverified_count
      skipped, excluded_records, employer_unverified, failed_ids
      candidate_ids     ids this batch tried to send (after DNC + contact gate)
      sent_ids, no_interview_ids, bulk_id   per-candidate outcome (completed)
    """
    res_out: Dict[str, Any] = {
        "idx": idx, "status": "completed", "event": None,
        "sent": 0, "no_interview": 0, "already_sent": 0,
        "excluded_count": 0, "employer_unverified_count": 0,
        "skipped": [], "excluded_records": [], "employer_unverified": [], "failed_ids": [],
        "candidate_ids": list(batch), "sent_ids": [], "no_interview_ids": [], "bulk_id": None,
    }
    # Ids actually sent to Pairbot for this batch: batch minus DNC-blocked
    # minus contact-gate exclusions. Failure paths must report THESE ids —
    # reporting the original batch would re-record rows already surfaced as
    # exclusions, putting permanently uncontactable candidates into the retry
    # set forever.
    sendable_ids: List[str] = batch

    def _failed(error: str) -> Dict[str, Any]:
        res_out["status"] = "failed"
        res_out["failed_ids"] = list(sendable_ids)
        res_out["event"] = {
            "type": "batch", "index": idx, "status": "failed",
            "error": error, "candidate_ids": list(sendable_ids),
        }
        return res_out

    try:
        async with _trace_span("payload_generation", batch_idx=idx, n=len(batch)):
            gp = await _generate_payload_for(
                GeneratePayloadRequest(candidate_ids=batch, job_id=request.job_id)
            )
        payload_str = gp.get("payload") if isinstance(gp, dict) else None
        if not payload_str:
            raise RuntimeError("generate-payload returned no payload")

        # Keep real_candidate_ids aligned with the payload's resumes:
        # generate drops DNC-stopped candidates from resumes, so drop them
        # from real_candidate_ids too — otherwise the positional matching in
        # _send_bulk_interview_core misaligns.
        dnc_blocked = set(gp.get("dnc_blocked_ids") or []) if isinstance(gp, dict) else set()
        real_ids = [c for c in batch if c not in dnc_blocked] if dnc_blocked else batch
        sendable_ids = real_ids
        # Reported on the internal channel only (the wire format is pinned):
        # a launch session must see these as excluded, or it re-plans them
        # every pass and ends "made no progress".
        res_out["dnc_blocked_ids"] = [c for c in batch if c in dnc_blocked]

        # Per-candidate contact gate. _send_bulk_interview_core's payload
        # validation 400s the ENTIRE batch when any single resume has no
        # usable phone/email — including the "Unknown Candidate" stub
        # generate-payload emits for an id with no sourced_candidates row.
        # Drop those rows here and surface them as exclusions.
        try:
            payload_obj = json.loads(payload_str)
        except Exception:
            payload_obj = None
        resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
        if isinstance(resumes, list) and len(resumes) == len(real_ids):
            launchable: List[tuple] = []
            batch_dropped: List[Dict[str, str]] = []
            for cid, resume in zip(real_ids, resumes):
                if not isinstance(resume, dict):
                    batch_dropped.append({
                        "candidate_id": cid, "name": "",
                        "reason": "invalid resume payload at launch",
                    })
                    continue
                clean_email = _sanitize_pair_candidate_email(str(resume.get("email") or ""))
                resume["email"] = clean_email
                has_contact = (
                    len(_pair_phone_digits(resume.get("phone"))) >= 7 or bool(clean_email)
                )
                if has_contact:
                    launchable.append((cid, resume))
                    continue
                name = str(resume.get("name") or "")
                batch_dropped.append({
                    "candidate_id": cid,
                    "name": name,
                    "reason": (
                        "no candidate record found at launch"
                        if name == "Unknown Candidate"
                        else "no usable phone or email at launch"
                    ),
                })
            if batch_dropped:
                logger.warning(
                    "launch batch %d: excluding %d contactless/missing rows pre-send: %s",
                    idx, len(batch_dropped), [d["candidate_id"] for d in batch_dropped],
                )
                res_out["excluded_records"].extend(batch_dropped)
                res_out["excluded_count"] += len(batch_dropped)
                real_ids = [cid for cid, _ in launchable]
                sendable_ids = real_ids
                payload_obj["resumes"] = [r for _, r in launchable]
                payload_str = json.dumps(payload_obj)
            if not real_ids:
                # Whole batch was uncontactable — nothing to send.
                res_out["candidate_ids"] = []
                res_out["event"] = {
                    "type": "batch", "index": idx, "status": "completed",
                    "sent": 0, "no_interview": 0, "already_sent": 0,
                    "excluded": len(batch_dropped), "bulk_id": None,
                }
                return res_out
        res_out["candidate_ids"] = list(sendable_ids)

        send_req = SendBulkInterviewRequest(
            payload=payload_str,
            real_candidate_ids=real_ids,
            is_initial_launch=False,       # fired once after the loop
            dry_run=False,
            notify_recruiters=False,       # fired once after the loop
            send_job_posting_email=False,  # fired once after the loop
            app_base_url=request.app_base_url,
            recruiter_handoff=request.recruiter_handoff,
            handoff_timeout_sec=request.handoff_timeout_sec,
        )
        # Launch-session hooks ride along only when set, so the legacy call
        # shape (and anything that stands in for the core) is unchanged.
        session_kwargs: Dict[str, Any] = {}
        if claimed_session_id is not None:
            session_kwargs["claimed_session_id"] = claimed_session_id
        if not provision_inline:
            session_kwargs["provision_inline"] = False
        if bulk_id_for is not None:
            session_kwargs["bulk_id_for"] = bulk_id_for
        if recruiter_email:
            session_kwargs["recruiter_email"] = recruiter_email
        if launch_id is not None or bulk_id_for is not None:
            res = await _send_bulk_interview_core(
                send_req, precomputed_signals=precomputed_signals,
                launch_id=launch_id, batch_idx=idx, **session_kwargs,
            )
        else:
            res = await _send_bulk_interview_core(
                send_req, precomputed_signals=precomputed_signals, **session_kwargs,
            )

        skipped = res.get("skipped_already_sent") or []
        res_out["skipped"] = list(skipped)
        # Server-side exclusions (employed by hiring client, offer extended,
        # …) are surfaced on the stream so the modal reports server skips.
        batch_excluded = res.get("excluded_candidates") or []
        res_out["excluded_records"].extend(batch_excluded)
        # Candidates that passed the gate with weak/unverified employer data.
        # Aggregated only for SUCCESSFUL sends.
        batch_unverified = res.get("employer_unverified") or []

        if res.get("success"):
            rows = res.get("data") or []
            # Only rows PAIR actually created an interview for count as "sent".
            sent = sum(1 for r in rows if isinstance(r, dict) and r.get("interview_id"))
            # Pairbot was silent about these: parked 'processing' until its
            # by-job record settles them — neither sent nor a failure yet.
            unconfirmed_ids = [
                str(r.get("candidate_id")) for r in rows
                if isinstance(r, dict) and not r.get("interview_id") and r.get("unconfirmed")
            ]
            no_interview = len(rows) - sent - len(unconfirmed_ids)
            res_out.update({
                "sent": sent,
                "no_interview": no_interview,
                "unconfirmed": len(unconfirmed_ids),
                "unconfirmed_ids": unconfirmed_ids,
                "already_sent": len(skipped),
                "employer_unverified": list(batch_unverified),
                "employer_unverified_count": len(batch_unverified),
                "sent_ids": [
                    str(r.get("candidate_id")) for r in rows
                    if isinstance(r, dict) and r.get("interview_id")
                ],
                "no_interview_ids": [
                    str(r.get("candidate_id")) for r in rows
                    if isinstance(r, dict) and not r.get("interview_id") and not r.get("unconfirmed")
                ],
                "bulk_id": res.get("bulk_id"),
            })
            res_out["excluded_count"] += len(batch_excluded)
            res_out["event"] = {
                "type": "batch",
                "index": idx,
                "status": "completed",
                "sent": sent,
                "no_interview": no_interview,
                # Only when non-zero: the wire shape is unchanged otherwise.
                **({"unconfirmed": len(unconfirmed_ids)} if unconfirmed_ids else {}),
                "already_sent": len(skipped),
                "excluded": len(batch_excluded),
                "employer_unverified": len(batch_unverified),
                "bulk_id": res.get("bulk_id"),
            }
            return res_out
        return _failed(res.get("message") or "send failed")
    except HTTPException as he:
        # 409 = outreach stopped for this job -> abort the whole launch.
        # Flag it immediately so batches not yet started don't run.
        if getattr(he, "status_code", None) == 409:
            abort_flag["aborted"] = True
            res_out["status"] = "aborted"
            res_out["failed_ids"] = list(sendable_ids)
            res_out["message"] = str(he.detail)
            return res_out
        return _failed(str(he.detail))
    except Exception as e:
        logger.error("launch batch %d failed: %s", idx, e, exc_info=True)
        return _failed(str(e))


def _adopt_trace_run(run_id: str, job_id: Any) -> None:
    """Make ``run_id`` the pipeline-trace run of this task (core.pipeline_trace
    ``adopt_run`` when present; fail-open no-op otherwise)."""
    try:
        from core import pipeline_trace as _pt
        fn = getattr(_pt, "adopt_run", None)
        if callable(fn):
            fn(run_id, job_id)
    except Exception:  # noqa: BLE001 — tracing never breaks a launch
        pass


def _strip_internal(evt: Dict[str, Any]) -> Dict[str, Any]:
    """The wire (SSE / launch_events) form of a run_launch event."""
    if "_internal" not in evt:
        return evt
    return {k: v for k, v in evt.items() if k != "_internal"}


async def run_launch(
    request: "LaunchRequest",
    *,
    launch_id: Optional[str] = None,
    run_id: Optional[str] = None,
    side_effects_guard: Optional[Callable[[], Any]] = None,
    fire_side_effects: bool = True,
    should_stop: Optional[Callable[[], Any]] = None,
    provision_inline: bool = True,
    claimed_session_id: Optional[str] = None,
    bulk_id_for: Optional[Callable[..., Any]] = None,
    recruiter_email: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """The launch pipeline behind /engage/launch, as an async generator of
    event dicts: ``start`` → ``batch``* (completion order) → optional
    ``error`` (409 outreach stopped) → ``done``.

    Each event is the exact wire event plus one ``_internal`` key that the
    endpoint strips (`_strip_internal`), so the SSE / launch_events format is
    unchanged. ``_internal`` carries what a server-side consumer needs:

      start  {"batches": [[ids]…]}
      batch  {"status", "candidate_ids" (tried to send), "sent_ids",
              "no_interview_ids", "failed_ids", "skipped_ids" (already sent),
              "excluded_ids", "bulk_id"}
      done   {"sent_ids", "no_interview_ids", "skipped_ids", "excluded_ids",
              "failed_ids", "not_run_ids", "stopped"}

    Session hooks (defaults = the endpoint's behaviour):
      run_id              adopt this pipeline-trace run id (else a new one).
      fire_side_effects   False = never fire the recruiter email / applicant
                          sync here (a session fires them once itself).
      side_effects_guard  sync ``() -> bool`` consulted once, right before the
                          side effects would fire; falsy or raising = skip.
      should_stop         sync ``() -> bool``, checked next to the 409 abort
                          flag before each batch starts (raising = stop).
                          Batches not started come back in done's
                          ``_internal.not_run_ids`` with ``stopped`` True.
      provision_inline / claimed_session_id / bulk_id_for
                          passed to `_send_bulk_interview_core` per batch.

    Consume it with ``contextlib.aclosing(run_launch(...))``. Helpers
    (`_run_one_batch`, `_send_bulk_interview_core`, `_precompute_launch_signals`,
    `_generate_payload_for`) are looked up as module globals at call time.
    """
    # Fix 0: run id for per-stage timing. Set in this task's context BEFORE
    # batch tasks are created so they inherit it.
    if run_id is None:
        run_id = _trace_new_run(request.job_id)
    else:
        _adopt_trace_run(run_id, request.job_id)
    # Fix 5: pause background JobDiva hydration while launching.
    _clear_active = None
    try:
        from services.jobdiva_activity import mark_job_active, clear_job_active
        await mark_job_active(str(request.job_id))
        _clear_active = clear_job_active
    except ImportError:
        pass
    except Exception as _mark_err:  # noqa: BLE001
        logger.warning("launch: mark_job_active failed: %s", _mark_err)

    ids = [str(c) for c in (request.candidate_ids or []) if str(c).strip()]
    batch_size = max(1, int(request.batch_size or _LAUNCH_PAIRBOT_BATCH_SIZE))
    batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    parallelism = max(1, int(_launch_flag("LAUNCH_BATCH_PARALLELISM", 1) or 1))

    start_evt: Dict[str, Any] = {
        "type": "start",
        "total_candidates": len(ids),
        "total_batches": len(batches),
        "batch_size": batch_size,
        "run_id": run_id,
    }
    if launch_id:
        start_evt["launch_id"] = launch_id

    totals = {"sent": 0, "already_sent": 0, "failed_batches": 0, "no_interview": 0, "excluded": 0, "employer_unverified": 0}
    all_skipped: List[str] = []
    all_excluded: List[Dict[str, str]] = []
    all_dnc_blocked: List[str] = []
    all_employer_unverified: List[Dict[str, str]] = []
    failed_candidate_ids: List[str] = []
    all_sent_ids: List[str] = []
    all_no_interview_ids: List[str] = []
    not_run_ids: List[str] = []
    stopped = False
    aborted = False
    side_effects_fired = {"done": False}
    # The fire-once ledger's "this launch" boundary: the job's first interview
    # at or after this moment came from THIS launch (launch_effects_ledger).
    launch_started_at = datetime.now(timezone.utc)
    abort_flag = {"aborted": False}
    tasks: List[asyncio.Task] = []

    def _stop_requested() -> bool:
        if should_stop is None:
            return False
        try:
            return bool(should_stop())
        except Exception as exc:  # noqa: BLE001 — unsure → start nothing new
            logger.warning("launch: should_stop raised (stopping): %s", exc)
            return True

    def _fire_once_side_effects() -> None:
        # Recruiter launch email + applicant sync fire ONCE per launch.
        # Invoked from the finally below so a client disconnect mid-stream
        # still fires them for candidates already engaged.
        if side_effects_fired["done"]:
            return
        side_effects_fired["done"] = True
        if not fire_side_effects or totals["sent"] <= 0:
            return
        if side_effects_guard is not None:
            try:
                if not side_effects_guard():
                    return
            except Exception as exc:  # noqa: BLE001 — at most once: unsure → skip
                logger.warning("launch: side-effects guard failed (not firing): %s", exc)
                return
        requested_job_posting = (
            request.send_job_posting_email
            if request.send_job_posting_email is not None
            else request.is_initial_launch
        )
        fire_launch_side_effects(
            request.job_id,
            sent_count=totals["sent"],
            notify_recruiters=request.notify_recruiters,
            # Public job-board posting only on a fully clean launch.
            send_job_posting_email=bool(
                requested_job_posting and not aborted and totals["failed_batches"] == 0
            ),
            is_initial_launch=request.is_initial_launch,
            app_base_url=request.app_base_url,
            launch_started_at=launch_started_at,
        )

    # Per-batch session hooks ride along only when set (legacy call shape).
    batch_kwargs: Dict[str, Any] = {}
    if claimed_session_id is not None:
        batch_kwargs["claimed_session_id"] = claimed_session_id
    if not provision_inline:
        batch_kwargs["provision_inline"] = False
    if bulk_id_for is not None:
        batch_kwargs["bulk_id_for"] = bulk_id_for
    if recruiter_email:
        batch_kwargs["recruiter_email"] = recruiter_email

    try:
        yield {**start_evt, "_internal": {"batches": [list(b) for b in batches]}}

        precomputed: Optional[Dict[str, Dict[str, Any]]] = None
        if ids and _launch_flag("LAUNCH_GATE_ONCE", False):
            async with _trace_span("employer_gate", scope="launch", n=len(ids)):
                precomputed = await _precompute_launch_signals(request.job_id, ids)

        sem = asyncio.Semaphore(parallelism)

        async def _guarded(i: int, b: List[str]) -> Dict[str, Any]:
            async with sem:
                if abort_flag["aborted"] or _stop_requested():
                    return {"idx": i, "status": "not_run", "failed_ids": list(b)}
                if i > 0:
                    await asyncio.sleep(_LAUNCH_BATCH_DELAY_SECONDS)  # stagger
                if abort_flag["aborted"] or _stop_requested():
                    return {"idx": i, "status": "not_run", "failed_ids": list(b)}
                async with _trace_span("launch_batch", batch_idx=i, n=len(b)):
                    return await _run_one_batch(
                        i, b, request=request, precomputed_signals=precomputed,
                        abort_flag=abort_flag, launch_id=launch_id, **batch_kwargs,
                    )

        tasks = [asyncio.ensure_future(_guarded(i, b)) for i, b in enumerate(batches)]
        abort_result: Optional[Dict[str, Any]] = None
        not_run: List[Dict[str, Any]] = []
        # Only this consumer loop touches the shared totals — no lock.
        for fut in asyncio.as_completed(tasks):
            r = await fut
            status = r.get("status")
            if status == "not_run":
                not_run.append(r)
                continue
            all_skipped.extend(r["skipped"])
            all_excluded.extend(r["excluded_records"])
            all_dnc_blocked.extend(str(c) for c in (r.get("dnc_blocked_ids") or []))
            totals["excluded"] += r["excluded_count"]
            if status == "aborted":
                abort_result = r
                continue
            if status == "completed":
                totals["sent"] += r["sent"]
                totals["already_sent"] += r["already_sent"]
                totals["no_interview"] += r["no_interview"]
                if r.get("unconfirmed"):
                    # Only present when non-zero (wire shape unchanged otherwise).
                    totals["unconfirmed"] = totals.get("unconfirmed", 0) + int(r["unconfirmed"])
                totals["employer_unverified"] += r["employer_unverified_count"]
                all_employer_unverified.extend(r["employer_unverified"])
                all_sent_ids.extend(r.get("sent_ids") or [])
                all_no_interview_ids.extend(r.get("no_interview_ids") or [])
            else:
                totals["failed_batches"] += 1
                failed_candidate_ids.extend(r["failed_ids"])
            if r.get("event"):
                yield {
                    **r["event"],
                    "_internal": {
                        "status": status,
                        "candidate_ids": list(r.get("candidate_ids") or []),
                        "sent_ids": list(r.get("sent_ids") or []),
                        "no_interview_ids": list(r.get("no_interview_ids") or []),
                        "failed_ids": list(r.get("failed_ids") or []),
                        "skipped_ids": list(r.get("skipped") or []),
                        "excluded_ids": [
                            str(e.get("candidate_id")) for e in (r.get("excluded_records") or [])
                            if isinstance(e, dict) and e.get("candidate_id")
                        ] + [str(c) for c in (r.get("dnc_blocked_ids") or [])],
                        "bulk_id": r.get("bulk_id"),
                    },
                }

        if abort_result is not None:
            # Same shape as the sequential flow: the aborting batch plus
            # every batch that never ran, in batch order.
            aborted = True
            remaining = list(abort_result["failed_ids"]) + [
                c for nr in sorted(not_run, key=lambda x: x["idx"]) for c in nr["failed_ids"]
            ]
            totals["failed_batches"] += 1
            failed_candidate_ids.extend(remaining)
            yield {
                "type": "error",
                "status_code": 409,
                "message": abort_result.get("message") or "",
                "index": abort_result["idx"],
                "candidate_ids": remaining,
            }
        elif not_run:
            # should_stop: batches never started — resumable, not failed.
            stopped = True
            not_run_ids = [
                c for nr in sorted(not_run, key=lambda x: x["idx"]) for c in nr["failed_ids"]
            ]
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Fire once even if the client disconnected mid-stream.
        _fire_once_side_effects()
        if _clear_active is not None:
            try:
                await _clear_active(str(request.job_id))
            except Exception as _clr_err:  # noqa: BLE001
                logger.warning("launch: clear_job_active failed: %s", _clr_err)

    yield {
        "type": "done",
        "aborted": aborted,
        "totals": totals,
        "skipped_already_sent": all_skipped,
        "excluded_candidates": all_excluded,
        "employer_unverified": all_employer_unverified,
        "failed_candidate_ids": failed_candidate_ids,
        "_internal": {
            "sent_ids": all_sent_ids,
            "no_interview_ids": all_no_interview_ids,
            "skipped_ids": list(all_skipped),
            "excluded_ids": [
                str(e.get("candidate_id")) for e in all_excluded
                if isinstance(e, dict) and e.get("candidate_id")
            ] + all_dnc_blocked,
            "failed_ids": list(failed_candidate_ids),
            "not_run_ids": not_run_ids,
            "stopped": stopped,
        },
    }


@router.post("/engage/launch")
async def launch_bulk_interviews(request: LaunchRequest, http_request: Request = None):
    """Single-call, backend-owned Launch PAIR orchestration.

    Replaces the frontend's per-batch generate -> send -> SSE loop: the browser
    sends candidate_ids + job/options ONCE, and this endpoint streams aggregated
    progress (SSE) while batching to Pairbot server-side. It reuses the exact
    per-batch logic (`_generate_payload_for` + `_send_bulk_interview_core`), so
    idempotency / DNC / audit / status write-through behave identically to the
    old flow. The pipeline itself is `run_launch` (shared with server launch
    sessions); this endpoint only formats its events.

    The employer gate is resolved once for the whole launch (LAUNCH_GATE_ONCE),
    batches run LAUNCH_BATCH_PARALLELISM at a time (1 = old sequential order),
    and with LAUNCH_ASYNC + `async_launch` the work runs in a tracked
    background task and this returns 202 {launch_id}.

    Difference from the old flow (intentional de-dup): the recruiter launch
    email and applicant sync fire ONCE per launch here, instead of once per
    batch.
    """
    from fastapi.responses import JSONResponse, StreamingResponse

    _nr_suppress_apdex()

    def _sse(obj: Dict[str, Any]) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    # The recruiter on the Pairbot payload comes from the auth token only.
    requested_by: Optional[str] = None
    if http_request is not None:
        try:
            _user = await asyncio.to_thread(get_current_user, http_request, None)
            requested_by = (_user.email or "").strip().lower() or None
        except HTTPException as exc:
            logger.warning(
                "launch: no authenticated requester (status=%s); Pairbot payload will omit recruiter_email",
                exc.status_code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch: could not resolve requester: %s", exc)

    async def _work(launch_id: Optional[str] = None, run_id: Optional[str] = None):
        async with contextlib.aclosing(
            run_launch(request, launch_id=launch_id, run_id=run_id, recruiter_email=requested_by)
        ) as events:
            async for evt in events:
                yield _sse(_strip_internal(evt))

    # --- Fix 3: async path -------------------------------------------------
    if request.async_launch and _launch_flag("LAUNCH_ASYNC", False):
        launch_id = str(uuid.uuid4())
        run_id = _trace_new_run(request.job_id)
        try:
            # Lazy safety net for runs whose worker died without a final write.
            await asyncio.to_thread(_sweep_stale_launch_runs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_runs stale sweep failed: %s", exc)
        launch_session_id = _normalise_launch_session_id(request.launch_session_id)
        recorded = False
        if launch_session_id:
            try:
                await asyncio.to_thread(
                    _launch_db,
                    "INSERT INTO launch_runs (id, job_id, requested_by, status, last_event_at, launch_session_id) "
                    "VALUES (%s, %s, %s, 'running', now(), %s)",
                    (launch_id, str(request.job_id), requested_by, launch_session_id),
                )
                recorded = True
            except Exception as exc:  # noqa: BLE001 — column missing on this DB: link dropped
                logger.warning("async launch: launch_session_id not recorded: %s", exc)
        if not recorded:
            try:
                await asyncio.to_thread(
                    _launch_db,
                    "INSERT INTO launch_runs (id, job_id, requested_by, status, last_event_at) "
                    "VALUES (%s, %s, %s, 'running', now())",
                    (launch_id, str(request.job_id), requested_by),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("async launch could not be recorded: %s", exc, exc_info=True)
                raise HTTPException(status_code=503, detail="Could not start launch")

        async def _run_async() -> None:
            final = "completed"
            seq = {"n": 0}
            cancelled = False

            def _append_sync(evt: Dict[str, Any]) -> None:
                _launch_append_event(launch_id, seq["n"], evt)
                seq["n"] += 1

            try:
                async for chunk in _work(launch_id=launch_id, run_id=run_id):
                    evt = json.loads(chunk[len("data: "):].strip())
                    if evt.get("type") == "error":
                        final = "aborted"
                    await asyncio.to_thread(_append_sync, evt)
            except asyncio.CancelledError:
                # Shutdown drain (or similar) cancelled us. Awaiting a thread
                # here could itself be cancelled, so write synchronously.
                final = "failed"
                cancelled = True
                logger.warning("async launch %s cancelled", launch_id)
                try:
                    _append_sync({"type": "error", "message": "launch interrupted (server restart)"})
                except Exception:  # noqa: BLE001
                    pass
                raise
            except BaseException as exc:
                final = "failed"
                logger.error("async launch %s crashed: %s", launch_id, exc, exc_info=True)
                try:
                    await asyncio.shield(asyncio.to_thread(
                        _append_sync, {"type": "error", "message": str(exc)}
                    ))
                except BaseException:  # noqa: BLE001
                    pass
                raise
            finally:
                try:
                    if cancelled:
                        _launch_finalize(launch_id, final)
                    else:
                        await asyncio.shield(asyncio.to_thread(_launch_finalize, launch_id, final))
                except BaseException as exc:  # noqa: BLE001
                    if not cancelled:
                        # Shield was interrupted: make sure the row is final.
                        try:
                            _launch_finalize(launch_id, final)
                        except Exception:  # noqa: BLE001
                            pass
                    logger.warning("launch_runs final status write failed %s: %s", launch_id, exc)

        _spawn_task(_run_async(), name=f"launch:{launch_id}")
        return JSONResponse(status_code=202, content={"launch_id": launch_id, "run_id": run_id})

    if request.launch_session_id:
        # SSE path: no launch_runs row exists to store the link on.
        logger.info(
            "launch_sse job=%s launch_session_id=%s (not stored: async launches only)",
            request.job_id, _normalise_launch_session_id(request.launch_session_id),
        )

    async def _gen():
        # Heartbeat pump. A single batch can hold the stream silent for many
        # minutes (Pairbot creation wait alone is up to 600s) — long enough for
        # proxies/browsers to idle the connection out and lose the rest of the
        # launch. Run the work in a task and emit an SSE comment (ignored by
        # the client parser) whenever no real event shows up for 15s.
        queue: asyncio.Queue = asyncio.Queue()
        _sentinel = object()

        async def _pump():
            error: Optional[Exception] = None
            try:
                async for chunk in _work():
                    await queue.put(chunk)
            except Exception as exc:
                error = exc
            finally:
                # put_nowait: unbounded queue, and safe even mid-cancellation.
                queue.put_nowait(error if error is not None else _sentinel)

        pump_task = asyncio.create_task(_pump())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is _sentinel:
                    break
                if isinstance(item, Exception):
                    # Preserve pre-pump semantics: a crash in the worker aborts
                    # the stream instead of ending it silently.
                    raise item
                yield item
        finally:
            # Client disconnect cancels this generator — cancel the worker so
            # its finally (fire-once side effects) runs, mirroring the old
            # direct-generator cancellation semantics.
            if not pump_task.done():
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            # Tell nginx not to buffer this stream (it has no SSE-specific
            # config of its own) and caches to keep hands off.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# Hard cap so a single sync run can never blast more than this many
# candidates at pairbot, even if the job's filters are loose. The auto-sync
# cron path uses this to avoid an unbounded blast radius.
AUTO_LAUNCH_BATCH_CAP = 25


async def auto_launch_for_candidates(candidate_ids: List[str], job_id: str) -> None:
    """Auto-launch pairbot interviews for newly-synced JobDiva applicants.

    Called from `auto_assign_service.synchronize_job_applicants` after it
    upserts new rows into `sourced_candidates`. Closes the gap where the
    15-min cron created candidate rows but never actually launched them
    to pairbot — so recruiters had to manually click Engage on every new
    applicant.

    Guards:
      - Only runs for jobs that have already been launched at least once
        (≥1 row with engage_status in sent/pending/completed). This stops
        us from auto-blasting brand-new jobs whose recruiter hasn't yet
        clicked "Launch PAIR".
      - Skips candidates that already have an engage_status set, are on
        the DNC list, or whose job has outreach_stopped_at set.
      - Hard-capped at AUTO_LAUNCH_BATCH_CAP per call.

    Failures are logged and swallowed — this runs as a background task and
    must not crash the calling sync cycle.
    """
    if not candidate_ids or not job_id:
        return
    try:
        eligible_ids: List[str] = []
        gate_rows: List[Tuple[str, Dict[str, Any]]] = []
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            # Job-level gating: outreach must not be stopped, and the
            # recruiter must have launched PAIR at least once before — we
            # treat any prior engage event as the signal that auto-launch
            # is wanted for incoming applicants on this job.
            cur.execute(
                """
                SELECT outreach_stopped_at
                FROM monitored_jobs
                WHERE job_id = %s OR jobdiva_id = %s
                LIMIT 1
                """,
                (str(job_id), str(job_id)),
            )
            job_row = cur.fetchone()
            if job_row and job_row.get("outreach_stopped_at") is not None:
                logger.info(
                    "auto_launch_skip job_id=%s reason=outreach_stopped",
                    job_id,
                )
                return

            cur.execute(
                """
                SELECT 1 FROM sourced_candidates
                WHERE (jobdiva_id = %s OR jobdiva_id = %s)
                  AND COALESCE(data->>'engage_status', '') != ''
                LIMIT 1
                """,
                (str(job_id), str(job_id)),
            )
            if not cur.fetchone():
                logger.info(
                    "auto_launch_skip job_id=%s reason=no_prior_launch count=%d",
                    job_id,
                    len(candidate_ids),
                )
                return

            # Filter to candidates that haven't already been engaged and
            # aren't DNC-blocked. We re-check at the DB layer rather than
            # trusting the caller's list, since the sync run could overlap
            # with a manual Engage click.
            cur.execute(
                """
                SELECT customer_name
                FROM monitored_jobs
                WHERE job_id = %s OR jobdiva_id = %s
                LIMIT 1
                """,
                (str(job_id), str(job_id)),
            )
            client_row = cur.fetchone()
            client_name = str(client_row["customer_name"]) if client_row and client_row.get("customer_name") else ""

            cur.execute(
                """
                SELECT candidate_id, data, headline, resume_text, source
                FROM sourced_candidates
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                  AND dnc_stopped_at IS NULL
                  AND COALESCE(data->>'engage_status', '') = ''
                  AND NOT (
                      -- Skip jobdiva.local candidates with no verified phone in the DB.
                      email ILIKE '%%jobdiva.local%%'
                      AND REGEXP_REPLACE(COALESCE(phone, ''), '\\D', '', 'g') = ''
                  )
                  AND NOT (
                      -- Exclude jobdiva.local applicants that are phone-duplicates of a real candidate
                      email ILIKE '%%jobdiva.local%%'
                      AND REGEXP_REPLACE(SPLIT_PART(COALESCE(email, ''), '@', 1), '\\D', '', 'g') <> ''
                      AND EXISTS (
                          SELECT 1 FROM sourced_candidates sc2
                          WHERE (sc2.jobdiva_id = %s OR sc2.jobdiva_id = %s)
                            AND sc2.candidate_id != sourced_candidates.candidate_id
                            AND sc2.email NOT ILIKE '%%jobdiva.local%%'
                            AND REGEXP_REPLACE(COALESCE(sc2.phone, ''), '\\D', '', 'g') <> ''
                            AND REGEXP_REPLACE(COALESCE(sc2.phone, ''), '\\D', '', 'g')
                                = REGEXP_REPLACE(SPLIT_PART(COALESCE(sourced_candidates.email, ''), '@', 1), '\\D', '', 'g')
                      )
                  )
                """,
                (list(candidate_ids), str(job_id), str(job_id), str(job_id), str(job_id)),
            )
            candidate_rows = cur.fetchall() or []
            # This is the path that contacted a Bank of America employee for a
            # Bank of America req: the applicant sync persists `data` before
            # resume extraction, so every employer field the gate reads is
            # absent from it. Hydrate from candidate_enhanced_info (+ the
            # headline column) so the gate judges the same candidate the
            # recruiter sees, not an empty row.
            employer_signals = _fetch_stored_employer_signals(
                conn, [str(r["candidate_id"]) for r in candidate_rows]
            )
            for r in candidate_rows:
                c_data = r.get("data") or {}
                if isinstance(c_data, str):
                    try:
                        c_data = json.loads(c_data)
                    except Exception:
                        c_data = {}
                c_data = _merge_employer_signals(
                    c_data,
                    headline=str(r.get("headline") or ""),
                    signals=employer_signals.get(str(r["candidate_id"])),
                )
                c_data.setdefault("candidate_id", str(r["candidate_id"]))
                # Stored resume + source ride along so the resolution pass
                # can parse without re-fetching from JobDiva.
                if r.get("resume_text") and not c_data.get("resume_text"):
                    c_data["resume_text"] = str(r.get("resume_text"))
                if r.get("source") and not c_data.get("source"):
                    c_data["source"] = str(r.get("source"))
                gate_rows.append((str(r["candidate_id"]), c_data))
        finally:
            cur.close()
            conn.close()

        # Employer resolution (policy 2026-09-02): auto-launch runs unattended,
        # so this is the last point where a missing/unconfident employer can be
        # resolved before outreach — fetch the resume and parse it now, with
        # JobDiva's profile work history as corroboration. Runs after the DB
        # connection is released (it can await resume fetches + LLM parses),
        # and fails open: a resolution error gates on stored signals as before.
        _emp_state_fn = None
        resolved_signals: Dict[str, Dict[str, Any]] = {}
        try:
            from services.employer_resolution import (
                employer_verification_state as _emp_state_fn,
                resolve_employer_signals as _resolve_fn,
            )
        except Exception:  # noqa: BLE001
            _resolve_fn = None
        if _resolve_fn is not None:
            try:
                resolved_signals = await asyncio.wait_for(
                    _resolve_fn([c for _, c in gate_rows]),
                    timeout=240.0
                )
            except Exception as res_err:  # noqa: BLE001
                logger.warning(
                    "auto_launch employer resolution failed job_id=%s (gating on stored signals): %s",
                    job_id, res_err,
                )

        gate_rows = [
            (
                cid,
                _merge_employer_signals(c_data, signals=resolved_signals[cid])
                if resolved_signals.get(cid) else c_data,
            )
            for cid, c_data in gate_rows
        ]
        # Resume freshness (phase 3) — advisory classification only; fails open.
        try:
            from services.employer_resolution import stamp_resume_freshness
            # Same 20s advisory cap as the manual launch site.
            await asyncio.wait_for(
                stamp_resume_freshness([c for _, c in gate_rows]), timeout=20.0
            )
        except Exception as fresh_err:  # noqa: BLE001
            logger.warning(
                "auto_launch resume-freshness check failed job_id=%s: %s", job_id, fresh_err
            )

        unverified_count = 0
        for cid, c_data in gate_rows:
            excluded, reason = is_candidate_excluded_from_pair(c_data, client_name)
            if excluded:
                logger.info("auto_launch_skip candidate=%s job_id=%s reason=%s", cid, job_id, reason)
                continue
            eligible_ids.append(cid)
            if _emp_state_fn is not None:
                emp_state = _emp_state_fn(c_data)
                if emp_state != "verified":
                    unverified_count += 1
                    logger.info(
                        "launch_employer_unverified candidate=%s job_id=%s state=%s client=%r",
                        cid, job_id, emp_state, client_name,
                    )
        if unverified_count:
            logger.warning(
                "auto_launch_employer_unverified job_id=%s count=%d of %d eligible — "
                "weak or unverified employer data (no history, JobDiva profile "
                "only, or a stale resume); see per-candidate state logs above",
                job_id, unverified_count, len(eligible_ids),
            )

        if not eligible_ids:
            logger.info(
                "auto_launch_skip job_id=%s reason=no_eligible_candidates",
                job_id,
            )
            return

        if len(eligible_ids) > AUTO_LAUNCH_BATCH_CAP:
            logger.warning(
                "auto_launch_truncated job_id=%s requested=%d cap=%d",
                job_id, len(eligible_ids), AUTO_LAUNCH_BATCH_CAP,
            )
            eligible_ids = eligible_ids[:AUTO_LAUNCH_BATCH_CAP]

        # Generate payload via the existing builder so JD context, rubric
        # and pre-screen questions stay in sync with manual launches.
        payload_result = await generate_engage_payload(
            GeneratePayloadRequest(candidate_ids=eligible_ids, job_id=job_id)
        )
        payload_str = payload_result.get("payload", "")
        if not payload_str:
            logger.warning(
                "auto_launch_skip job_id=%s reason=empty_payload candidate_count=%d",
                job_id, len(eligible_ids),
            )
            return

        logger.info(
            "auto_launch_dispatch job_id=%s candidate_count=%d",
            job_id, len(eligible_ids),
        )
        result = await send_bulk_interview(
            SendBulkInterviewRequest(
                payload=payload_str,
                real_candidate_ids=eligible_ids,
                is_initial_launch=False,
                dry_run=False,
                notify_recruiters=False,
            )
        )
        if not (result or {}).get("success"):
            logger.warning(
                "auto_launch_pairbot_unsuccessful job_id=%s message=%s",
                job_id, (result or {}).get("message"),
            )
    except Exception as exc:  # noqa: BLE001 — background task, must not crash caller
        logger.error(
            "auto_launch_failed job_id=%s candidate_count=%d error=%s",
            job_id, len(candidate_ids), exc,
            exc_info=True,
        )


@router.get("/latest-interview/by-id/{candidate_id}")
async def get_latest_interview(candidate_id: str):
    """
    Look up the latest interview_id for a candidate from the audit table.
    Used by the Assess button to determine which interview to display.
    """
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT interview_id, candidate_name, candidate_email, jobdiva_id, status, created_at
            FROM engage_interview_audit
            WHERE candidate_id = %s AND interview_id IS NOT NULL AND interview_id::text != ''
            ORDER BY id DESC
            LIMIT 1
        """, (candidate_id,))

        row = cur.fetchone()
        cur.close()
        conn.close()

        if row:
            return {
                "success": True,
                "interview_id": row["interview_id"],
                "candidate_name": row.get("candidate_name", ""),
                "candidate_email": row.get("candidate_email", ""),
                "job_id": row.get("job_id", ""),
                "status": row.get("status", ""),
                "created_at": str(row.get("created_at", ""))
            }
        else:
            return {
                "success": False,
                "interview_id": None,
                "message": "No interview found for this candidate"
            }

    except Exception as e:
        logger.error(f"❌ latest-interview lookup failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))




# ---------------------------------------------------------------------------
# 5. GET /assess/{interview_id}  (Proxy for PAIR dashboard data)
# ---------------------------------------------------------------------------
@router.get("/assess/{interview_id}")
async def get_assessment_data(interview_id: str):
    """
    Aggregates data from multiple PAIR dashboard endpoints into a single response
    for the Assess modal:
      - Interview info (status, score, progress)
      - Evaluation (per-question Q&A with scores)
      - Transcriptions (conversation messages)
      - Outreach status (communication timeline)
    """
    base_url = EXTERNAL_INTERVIEW_API_URL
    headers = get_pair_auth_headers()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fire all 4 requests in parallel
        interview_task = client.get(f"{base_url}/api/interviews/{interview_id}", headers=headers)
        evaluation_task = client.get(f"{base_url}/api/interviews/{interview_id}/evaluation", headers=headers)
        transcription_task = client.get(f"{base_url}/api/interviews/{interview_id}/transcriptions", headers=headers)
        outreach_task = client.get(f"{base_url}/api/interviews/{interview_id}/outreach-status", headers=headers)

        # Await all
        interview_res = await interview_task
        evaluation_res = await evaluation_task
        transcription_res = await transcription_task
        outreach_res = await outreach_task

    # Parse responses (gracefully handle failures)
    def safe_json(response, label):
        try:
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"⚠️ {label} returned {response.status_code}")
                return None
        except Exception as e:
            logger.warning(f"⚠️ Failed to parse {label}: {e}")
            return None

    interview_data = safe_json(interview_res, "interview")
    evaluation_data = safe_json(evaluation_res, "evaluation")
    transcription_data = safe_json(transcription_res, "transcriptions")
    outreach_data = safe_json(outreach_res, "outreach-status")

    # Handle the case where the response wraps data in a "data" key
    if interview_data and "data" in interview_data:
        interview_data = interview_data["data"]
    if evaluation_data and "data" in evaluation_data:
        evaluation_data = evaluation_data["data"]
    if outreach_data and "data" in outreach_data:
        outreach_data = outreach_data["data"]
    if transcription_data and "data" in transcription_data:
        transcription_data = transcription_data["data"]

    # Prefer Curate's resolved pass/fail status when available so assessment
    # surfaces the effective engagement result instead of Pair Bot's raw
    # completion state.
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT status
                    FROM engage_interview_audit
                    WHERE interview_id = %s
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (interview_id,),
                )
                audit_row = cur.fetchone()
                if audit_row and interview_data:
                    effective_status = str(audit_row.get("status") or "").strip()
                    if effective_status:
                        interview_data["status"] = effective_status
    except Exception as e:
        logger.warning(f"⚠️ Failed to overlay effective interview status for {interview_id}: {e}")

    return {
        "success": True,
        "interview_id": interview_id,
        "interview": interview_data,
        "evaluation": evaluation_data,
        "transcriptions": transcription_data if isinstance(transcription_data, list) else (transcription_data or []),
        "outreach": outreach_data
    }


@router.get("/mock-recording.wav")
async def get_mock_recording():
    """Dev-only stand-in audio for the recordings player (MOCK_CALL_RECORDINGS=true)."""
    from fastapi.responses import Response
    from services import call_recordings

    if not call_recordings.mock_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=call_recordings.mock_wav(), media_type="audio/wav")


def _recording_audit_row(interview_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT jobdiva_id, created_at
                FROM engage_interview_audit
                WHERE interview_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (interview_id,),
            )
            return cur.fetchone()


# Cap sequential S3 list_objects_v2 calls per request: when a session has no
# started_at, each one lists up to 6 prefixes (unknown-date plus a ±days
# fallback window around the interview's creation day).
_RECORDINGS_MAX_SESSIONS = 20


@router.get("/interviews/{interview_id}/recordings")
async def get_interview_recordings(
    interview_id: str,
    user: UserIdentity = Depends(get_current_user),
):
    """Call recordings (S3) of an interview, with presigned playback URLs.

    Requires a signed-in user and only serves interviews Curate itself
    launched (present in engage_interview_audit), scoped to the same
    recruiter/team access as the rest of the job's /interviews/{id}/...
    routes. Disabled unless ENVIRONMENT=production and S3_RECORDINGS_BUCKET
    is set, matching when pair-bot uploads recordings.
    """
    from services import call_recordings

    if not call_recordings.is_enabled():
        if call_recordings.mock_enabled():
            return {
                "success": True,
                "enabled": True,
                "unavailable": False,
                "recordings": call_recordings.mock_recordings(),
            }
        return {"success": True, "enabled": False, "unavailable": False, "recordings": []}

    try:
        row = await asyncio.to_thread(_recording_audit_row, interview_id)
        if not row:
            raise HTTPException(status_code=404, detail="Interview not found")
        jobdiva_id = (row.get("jobdiva_id") or "").strip() or None

        # Scope to the same job-level recruiter/team access as other
        # /interviews/{id}/... routes. An interview with no jobdiva_id on its
        # audit row can't be scoped to any job, so only admins may read it.
        #
        # _verify_job_access_by_id is deliberately called WITHOUT
        # allow_not_found=True: if the job's monitored_jobs draft has been
        # deleted (e.g. an old or cleaned-up job), a non-admin recruiter gets
        # 403 here rather than access. There is no other source of truth to
        # fall back to — engage_interview_audit does not carry recruiter
        # assignment — and recordings are candidate call audio, so failing
        # closed on an unresolvable ownership check is intentional. An admin
        # can always still retrieve the recording if that happens.
        if jobdiva_id:
            await asyncio.to_thread(_verify_job_access_by_id, jobdiva_id, user)
        elif not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Access denied. You do not have permission to access this interview.",
            )

        created = row.get("created_at")
        fallback_day = created.date() if created else None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(
                    f"{EXTERNAL_INTERVIEW_API_URL}/api/interviews/{interview_id}/detail",
                    headers=get_pair_auth_headers(),
                )
                res.raise_for_status()
                detail = res.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Upstream PAIR outage: degrade the same way an S3 outage does
            # (unavailable: true) instead of surfacing a 500, so the modal
            # shows the "temporarily unavailable" hint rather than an error.
            # A 200 with unavailable:true is invisible to status-code-based
            # monitoring, so also emit a New Relic event an alert can fire on.
            logger.warning(
                "recordings detail lookup failed for interview %s: %s", interview_id, exc
            )
            _log_recordings_upstream_failure(interview_id, exc)
            return {"success": True, "enabled": True, "recordings": [], "unavailable": True}

        if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
            detail = detail["data"]
        all_sessions = [
            {"id": s.get("id"), "started_at": s.get("started_at")}
            for s in (detail.get("sessions") or [])
            if isinstance(s, dict)
        ]
        sessions = all_sessions[:_RECORDINGS_MAX_SESSIONS]
        if len(all_sessions) > len(sessions):
            logger.warning(
                "recordings session list truncated interview=%s total=%d kept=%d",
                interview_id, len(all_sessions), len(sessions),
            )

        result = await asyncio.to_thread(
            call_recordings.find_recordings, sessions, jobdiva_id, fallback_day
        )
        # The raw S3 key is internal detail the client never needs — only an
        # opaque, stable id (used as a React list key) and the presigned url.
        recordings = []
        for rec in result.get("recordings", []):
            rec = dict(rec)
            raw_key = rec.pop("key", "")
            rec["id"] = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
            recordings.append(rec)
        result = {**result, "recordings": recordings}
        return {"success": True, "enabled": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ recordings lookup failed for interview {interview_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load call recordings")


# ---------------------------------------------------------------------------
# 6. Outreach API Proxies
# ---------------------------------------------------------------------------
async def _proxy_get(path: str, params: dict = None):
    try:
        headers = get_pair_auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(f"{EXTERNAL_INTERVIEW_API_URL}{path}", params=params, headers=headers)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logger.error(f"❌ Proxy GET {path} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

async def _proxy_post(path: str, json_data: dict = None):
    try:
        headers = get_pair_auth_headers(json_content_type=True)
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(f"{EXTERNAL_INTERVIEW_API_URL}{path}", json=json_data, headers=headers)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logger.error(f"❌ Proxy POST {path} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/dashboard/pair-outreach")
async def get_pair_outreach(status: Optional[str] = None, phase: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, jd_id: Optional[str] = None, search: Optional[str] = None):
    params = {k: v for k, v in {"status": status, "phase": phase, "date_from": date_from, "date_to": date_to, "jd_id": jd_id, "search": search}.items() if v is not None}
    return await _proxy_get("/api/dashboard/pair-outreach", params=params)

@router.get("/dashboard/pair-outreach/{jd_id}")
async def get_pair_outreach_jd(jd_id: str):
    return await _proxy_get(f"/api/dashboard/pair-outreach/{jd_id}")

@router.get("/dashboard/pair-metrics")
async def get_pair_metrics():
    return await _proxy_get("/api/dashboard/pair-metrics")

@router.get("/dashboard/pair-passed")
async def get_pair_passed(score_threshold: Optional[int] = None):
    params = {"score_threshold": score_threshold} if score_threshold is not None else {}
    return await _proxy_get("/api/dashboard/pair-passed", params=params)

@router.get("/interviews/{interview_id}/outreach-status")
async def get_outreach_status(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/outreach-status")

@router.post("/outreach/start-scheduler")
async def start_scheduler():
    return await _proxy_post("/api/outreach/start-scheduler")

@router.post("/interviews/{interview_id}/trigger-phase2")
async def trigger_phase2(interview_id: str):
    return await _proxy_post(f"/api/interviews/{interview_id}/trigger-phase2")


# ---------------------------------------------------------------------------
# 7. Retrieval of Transcripts API Proxies
# ---------------------------------------------------------------------------
@router.get("/interviews/{interview_id}/transcriptions")
async def get_transcriptions(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/transcriptions")

@router.get("/interviews/{interview_id}/evaluation")
async def get_interview_evaluation(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/evaluation")

@router.get("/interviews/{interview_id}/score-summary")
async def get_interview_score_summary(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/score-summary")

@router.get("/interviews/{interview_id}/activity-logs")
async def get_activity_logs(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/activity-logs")

from fastapi.responses import StreamingResponse

@router.get("/interviews/{interview_id}/transcriptions/download")
async def download_transcriptions(interview_id: str):
    try:
        client = httpx.AsyncClient(timeout=30.0)
        req = client.build_request("GET", f"{EXTERNAL_INTERVIEW_API_URL}/api/interviews/{interview_id}/transcriptions/download")
        res = await client.send(req, stream=True)
        return StreamingResponse(res.aiter_bytes(), headers=res.headers)
    except Exception as e:
        logger.error(f"❌ download_transcriptions failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

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
        conn = _get_db_connection()
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
        _spawn_task(
            jobdiva_service.update_candidate_qualification(
                candidate_id=jd_candidate_id,
                qualification_name=JOBDIVA_PAIR_QUALIFICATION_NAME,
                value=JOBDIVA_PASS_QUALIFICATION_VALUE,
                recruiter_id=JOBDIVA_PAIR_RECRUITER_ID,
                update_date=completion_ts,
                qualification_type_id=JOBDIVA_PAIR_QUALIFICATION_ID
            )
        )

        # Create JobDiva Note: PAIR Pass Candidate Report
        # Note: We use the job title from job_row for the message
        base_url = resolve_app_base_url(request.app_base_url if 'request' in locals() else "")
        pair_job_title = job_row.get("enhanced_title") or job_row.get("title") or "the"
        report_link = candidate_report_link(base_url, jd_job_id, candidate_id)
        safe_report_link = html.escape(report_link, quote=True)
        safe_pair_job_title = html.escape(str(pair_job_title))
        note_text = f"Candidate completed Phone Screen for {safe_pair_job_title} position. <a href=\"{safe_report_link}\" target=\"_blank\">Click Here</a> to view the report."
        
        async def create_and_pin_note():
            note_res = await jobdiva_service.create_candidate_note(
                candidate_id=jd_candidate_id,
                job_id=jd_job_id,
                action=JOBDIVA_PASS_ACTION_NAME,
                note_text=note_text,
                recruiter_id=JOBDIVA_PAIR_RECRUITER_ID
            )
            if note_res.get("status") == "success":
                note_id = note_res.get("data")
                if note_id:
                    await jobdiva_service.pin_candidate_note(note_id=note_id, is_pinned=True)

        _spawn_task(create_and_pin_note())

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
            _spawn_task(auto_assign_service.refresh_job_performance_metrics(job_id))
        else:
            logger.warning(f"⚠️ Email send failed for candidate {candidate_id} / job {job_id}. Rolling back dedup flag.")
            _rollback_passed_email_flag()

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"❌ Failed to process Candidate Passed notification: {e}", exc_info=True)
        if 'updated' in locals() and updated and '_rollback_passed_email_flag' in locals():
            _rollback_passed_email_flag()


# ---------------------------------------------------------------------------
# Re-Provision endpoint
# Backfills all launched (engage_status='sent') candidates for a job that do
# not yet have a jobdiva_candidate_id.  Call this after a bulk launch to
# ensure all candidates are registered as JobDiva applicants so Reject/Submit
# and action-notes work correctly.
# ---------------------------------------------------------------------------
class ReProvisionRequest(BaseModel):
    job_id: str
    # If True, provisions ALL launched candidates (even those already provisioned).
    # Defaults to False = only candidates missing a jobdiva_candidate_id.
    force_all: bool = False


@router.post("/engage/re-provision")
async def re_provision_candidates(request: ReProvisionRequest):
    """
    Re-runs JobDiva provisioning for all launched candidates for a job.

    By default only candidates that are missing a jobdiva_candidate_id in
    their data blob are processed (the ones that were silently dropped by the
    old fire-and-forget approach).  Pass force_all=true to re-run for every
    launched candidate (useful to fix 'Unknown Unknown' names).

    Returns counts of { success, skipped, failed, total }.
    """
    job_id = (request.job_id or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")

    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Resolve both forms of the job ID so we can match audit records
        numeric_job_id, ref_job_id = await _resolve_provisioning_job_ids(job_id)
        j1 = job_id
        j2 = numeric_job_id or job_id
        j3 = ref_job_id or job_id

        if request.force_all:
            # All launched candidates for this job
            cur.execute("""
                SELECT DISTINCT sc.candidate_id
                FROM sourced_candidates sc
                JOIN engage_interview_audit eia ON eia.candidate_id = sc.candidate_id
                WHERE eia.jobdiva_id IN (%s, %s, %s)
                  AND sc.jobdiva_id IN (%s, %s, %s)
            """, (j1, j2, j3, j1, j2, j3))
        else:
            # Only those missing a jobdiva_candidate_id
            cur.execute("""
                SELECT DISTINCT sc.candidate_id
                FROM sourced_candidates sc
                JOIN engage_interview_audit eia ON eia.candidate_id = sc.candidate_id
                WHERE eia.jobdiva_id IN (%s, %s, %s)
                  AND sc.jobdiva_id IN (%s, %s, %s)
                  AND (
                    sc.data->>'jobdiva_candidate_id' IS NULL
                    OR sc.data->>'jobdiva_candidate_id' = ''
                  )
            """, (j1, j2, j3, j1, j2, j3))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        candidate_ids = [str(r["candidate_id"]) for r in rows]
        total = len(candidate_ids)
        logger.info(
            f"🔄 [Re-Provision] job={job_id} force_all={request.force_all} "
            f"candidates_to_process={total}"
        )

        if not candidate_ids:
            return {
                "success": True,
                "message": "No candidates require provisioning.",
                "total": 0,
                "success_count": 0,
                "skipped": 0,
                "failed": 0,
            }

        # Run batch provisioning (awaited so the HTTP response carries the result)
        results = await _provision_batch_to_jobdiva(
            candidate_ids,
            job_id,
            label="Re-Provision",
        )

        return {
            "success": True,
            "job_id": job_id,
            "total": total,
            "success_count": results.get("success", 0),
            "skipped": results.get("skipped", 0),
            "failed": results.get("failed", 0),
            "duplicate_suspected": results.get("duplicate_suspected", 0),
            # Included in `failed`: rows too empty to create a JobDiva profile from.
            "blank_profile_refused": results.get("blank_profile_refused", 0),
            "message": (
                f"Re-provisioning complete. "
                f"{results.get('success', 0)} created, "
                f"{results.get('skipped', 0)} already existed, "
                f"{results.get('failed', 0)} failed."
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [Re-Provision] error for job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
    numeric_job_id, ref_job_id = await _resolve_provisioning_job_ids(job_id)
    return [job_id] + [str(x) for x in (numeric_job_id, ref_job_id) if x and str(x) != job_id]


def _audit_job_filter_sql(job_ids: List[str], alias: str) -> str:
    """``" AND <alias>.jobdiva_id = ANY(%s)"`` for a scoped audit, ``""`` for all jobs.
    The matching param is ``[job_ids]`` (one list param) or none -- see callers."""
    return f" AND {alias}.jobdiva_id = ANY(%s)" if job_ids else ""


async def _jobdiva_profile_audit_job_filter(job_id: Optional[str]) -> Tuple[str, list]:
    """SQL fragment + params scoping a profile-audit query (alias ``sc``) to one job, or none."""
    job_ids = await _audit_job_ids(job_id)
    return _audit_job_filter_sql(job_ids, "sc"), ([job_ids] if job_ids else [])


class JobDivaProfileAuditRepairRequest(BaseModel):
    job_id: Optional[str] = None


@router.get("/engage/jobdiva-profile-audit")
async def jobdiva_profile_audit(
    job_id: Optional[str] = None,
    limit: int = 200,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: JobDiva-sourced rows whose stored jobdiva_candidate_id is not their own id.

    Each row is a suspected duplicate JobDiva profile: `real_profile_id` is the
    profile the recruiter's search returned, `duplicate_profile_id` the one the
    provisioner persisted after JobDiva minted or linked something else. Merge
    the pair in JobDiva, then call the repair endpoint (or re-launch) to re-stamp.
    """
    _require_admin(user)
    limit = max(1, min(int(limit or 200), 2000))
    job_sql, job_params = await _jobdiva_profile_audit_job_filter(job_id)
    conn = _get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM sourced_candidates sc
            WHERE {_JOBDIVA_PROFILE_AUDIT_WHERE}{job_sql}
            """,
            job_params,
        )
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            f"""
            SELECT sc.jobdiva_id AS job_id,
                   sc.candidate_id AS real_profile_id,
                   sc.data->>'jobdiva_candidate_id' AS duplicate_profile_id,
                   sc.source, sc.name, sc.updated_at
            FROM sourced_candidates sc
            WHERE {_JOBDIVA_PROFILE_AUDIT_WHERE}{job_sql}
            ORDER BY sc.updated_at DESC NULLS LAST
            LIMIT %s
            """,
            job_params + [limit],
        )
        rows = [dict(r) for r in (cur.fetchall() or [])]
        cur.close()
    finally:
        conn.close()
    return {"success": True, "job_id": job_id or None, "total": total, "returned": len(rows), "rows": rows}


@router.post("/engage/jobdiva-profile-audit/repair")
async def jobdiva_profile_audit_repair(
    request: JobDivaProfileAuditRepairRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: re-stamp every JobDiva-sourced row's jobdiva_candidate_id with its own id.

    Idempotent; touches only rows where the stored id is missing or differs.
    Merging the duplicate profiles inside JobDiva stays a recruiter action —
    run the audit first to get the list.
    """
    _require_admin(user)
    job_sql, job_params = await _jobdiva_profile_audit_job_filter(request.job_id)
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE sourced_candidates sc
            SET data = COALESCE(sc.data, '{{}}'::jsonb)
                       || jsonb_build_object('jobdiva_candidate_id', sc.candidate_id)
            WHERE lower(sc.source) LIKE 'jobdiva%%'
              AND sc.candidate_id ~ '^[0-9]+$'
              AND sc.data->>'jobdiva_candidate_id' IS DISTINCT FROM sc.candidate_id{job_sql}
            """,
            job_params,
        )
        repaired = cur.rowcount
        conn.commit()
        cur.close()
    finally:
        conn.close()
    logger.info(
        "jobdiva profile audit repair: job=%s rows=%s by=%s",
        request.job_id or "ALL", repaired, user.email,
    )
    return {"success": True, "job_id": request.job_id or None, "repaired": repaired}


class JobDivaBlankProfileBackfillRequest(BaseModel):
    job_id: Optional[str] = None
    jobdiva_ids: Optional[List[str]] = None
    # Default True: report what each profile WOULD get. Pass false to write.
    dry_run: bool = True
    # Profiles per call (max 50), processed one at a time: JobDiva's BI résumé
    # endpoints rate-limit hard.
    limit: int = 10


@router.post("/engage/jobdiva-blank-profile-backfill")
async def jobdiva_blank_profile_backfill(
    request: JobDivaBlankProfileBackfillRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: repair JobDiva profiles Launch PAIR created blank before 2026-09-25.

    Per profile (services/jobdiva_profile_backfill.py): upload the résumé PAIR
    can build when every résumé JobDiva holds is empty, then fill only blank
    fields -- name, email (or the alternate slot when another JobDiva record
    holds it: a likely duplicate, reported as email_held_by_another_profile),
    phones, city / state / zip / country, LinkedIn and other social links.
    Nothing entered in JobDiva is overwritten; JobDiva-sourced people are never
    touched. Dry run by default. With dry_run=false each settled profile is
    stamped (jobdiva_backfill_checked_at), so repeated calls walk the list.
    """
    _require_admin(user)
    from services.jobdiva_profile_backfill import (
        BACKFILL_CANDIDATES_SQL,
        BACKFILL_STAMP_SQL,
        backfill_blank_profile,
        settles,
    )

    limit = max(1, min(int(request.limit or 10), 50))
    job_ids = await _audit_job_ids(request.job_id) if request.job_id else []
    conn = _get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(BACKFILL_CANDIDATES_SQL, {
            "include_checked": bool(request.jobdiva_ids),
            "jobdiva_ids": [str(i).strip() for i in request.jobdiva_ids or [] if str(i).strip()] or None,
            "job_ids": job_ids or None,
            "limit": limit,
        })
        rows = [dict(r) for r in (cur.fetchall() or [])]
        cur.close()
    finally:
        conn.close()

    reports: List[Dict[str, Any]] = []
    for row in rows:
        report = await backfill_blank_profile(row, apply=not request.dry_run)
        reports.append(report)
        if settles(report):
            stamp = {"jobdiva_backfill_checked_at": _utc_now_iso(), "jobdiva_backfill_status": report["status"]}
            conn = _get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute(BACKFILL_STAMP_SQL, {
                    "delta": json.dumps(stamp), "jobdiva_id": report["jobdiva_candidate_id"],
                })
                conn.commit()
                cur.close()
            finally:
                conn.close()

    counts: Dict[str, int] = {}
    for report in reports:
        counts[report["status"]] = counts.get(report["status"], 0) + 1
    logger.info(
        "jobdiva blank-profile backfill: dry_run=%s job=%s processed=%s %s by=%s",
        request.dry_run, request.job_id or "ALL", len(reports), counts, user.email,
    )
    return {
        "success": True,
        "dry_run": request.dry_run,
        "processed": len(reports),
        "statuses": counts,
        "likely_duplicates": [r["jobdiva_candidate_id"] for r in reports if r.get("email_held_by_another_profile")],
        "reports": reports,
    }


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


class ApplicantOriginRepairRequest(BaseModel):
    job_id: Optional[str] = None
    # Default True: report what WOULD be merged/deleted. Pass false to apply.
    dry_run: bool = True


@router.get("/engage/applicant-origin-audit")
async def applicant_origin_audit(
    job_id: Optional[str] = None,
    limit: int = 200,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: JobDiva-labelled rows that are twins of a provisioned origin row.

    A pair is `origin` (non-JobDiva source, e.g. LinkedIn-Exa, whose stored
    jobdiva_candidate_id is the profile Launch PAIR created or attached) and
    `twin` (a JobDiva-* row whose candidate_id IS that profile id) on the same
    job. The twin is the sync's or a re-launch's re-import of the same person;
    the origin row is the one that should survive, keeping its source label.
    One origin can have several twins (an Applicants row from the sync and a
    TalentSearch/JobAgent row from a re-launch); each pair is one row here.
    """
    _require_admin(user)
    limit = max(1, min(int(limit or 200), 2000))
    job_ids = await _audit_job_ids(job_id)
    job_sql = _audit_job_filter_sql(job_ids, "t")
    job_params: list = [job_ids] if job_ids else []
    conn = _get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            SELECT COUNT(*) AS n
            {_APPLICANT_TWIN_JOIN}{job_sql}
            """,
            job_params,
        )
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            f"""
            SELECT t.jobdiva_id AS job_id,
                   o.candidate_id AS origin_candidate_id,
                   o.source AS origin_source,
                   o.data->>'jobdiva_application_origin' AS origin_application_origin,
                   o.data->>'engage_status' AS origin_engage_status,
                   t.candidate_id AS jobdiva_candidate_id,
                   t.source AS twin_source,
                   t.data->>'engage_status' AS twin_engage_status,
                   COALESCE(NULLIF(o.name, ''), t.name) AS name,
                   t.created_at AS twin_created_at
            {_APPLICANT_TWIN_JOIN}{job_sql}
            ORDER BY t.created_at DESC NULLS LAST
            LIMIT %s
            """,
            job_params + [limit],
        )
        rows = [dict(r) for r in (cur.fetchall() or [])]
        cur.close()
    finally:
        conn.close()
    return {"success": True, "job_id": job_id or None, "total": total, "returned": len(rows), "rows": rows}


@router.post("/engage/applicant-origin-audit/repair")
async def applicant_origin_audit_repair(
    request: ApplicantOriginRepairRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: fold every JobDiva-labelled twin into its origin row, then delete the twins.

    Per origin row: engage bookkeeping the origin lacks is taken from its twins
    -- ALL of them, key by key, the most recently updated twin winning a key
    that several carry -- the merge is noted in data.jobdiva_twin_merged_at /
    jobdiva_twin_sources / jobdiva_twin_count, and the twins are deleted. The
    origin keeps its `source` -- that is the whole point. `dry_run` (default)
    only counts. Idempotent: a repaired job has no pairs left.

    The fold is a correlated subquery over the origin, not an ``UPDATE ... FROM``
    join: with a join, an origin with two twins is matched twice and Postgres
    applies SET from one arbitrary match, so the other twin's engage state would
    be dropped -- and then deleted with the twin.
    """
    _require_admin(user)
    job_ids = await _audit_job_ids(request.job_id)
    job_params: list = [job_ids] if job_ids else []
    conn = _get_db_connection()
    try:
        cur = conn.cursor()
        if request.dry_run:
            cur.execute(
                f"""
                SELECT COUNT(*)
                {_APPLICANT_TWIN_JOIN}{_audit_job_filter_sql(job_ids, "t")}
                """,
                job_params,
            )
            row = cur.fetchone()
            would = int((row[0] if isinstance(row, (tuple, list)) else (row or {}).get("count")) or 0)
            cur.close()
            return {"success": True, "job_id": request.job_id or None, "dry_run": True, "would_merge": would}

        fold_keys_sql = ", ".join("%s" for _ in _APPLICANT_TWIN_FOLD_KEYS)
        cur.execute(
            f"""
            UPDATE sourced_candidates AS o
            SET data = COALESCE(o.data, '{{}}'::jsonb)
                       || COALESCE((
                            -- Every engage key the origin lacks, from whichever twin
                            -- carries it; when several do, the most recently updated
                            -- twin's value wins (deterministic, no state dropped).
                            SELECT jsonb_object_agg(folded.k, folded.v)
                            FROM (
                                SELECT DISTINCT ON (e.k) e.k, e.v
                                FROM sourced_candidates AS t
                                CROSS JOIN LATERAL jsonb_each(COALESCE(t.data, '{{}}'::jsonb)) AS e(k, v)
                                WHERE {_TWIN_OF_ORIGIN}
                                  AND e.k IN ({fold_keys_sql})
                                  AND (o.data -> e.k) IS NULL
                                ORDER BY e.k, t.updated_at DESC NULLS LAST, t.id DESC
                            ) AS folded
                          ), '{{}}'::jsonb)
                       || jsonb_build_object(
                            'jobdiva_twin_merged_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                            'jobdiva_twin_sources', (
                                SELECT COALESCE(jsonb_agg(DISTINCT t.source), '[]'::jsonb)
                                FROM sourced_candidates AS t
                                WHERE {_TWIN_OF_ORIGIN}
                            ),
                            'jobdiva_twin_count', (
                                SELECT COUNT(*)
                                FROM sourced_candidates AS t
                                WHERE {_TWIN_OF_ORIGIN}
                            )
                          ),
                updated_at = CURRENT_TIMESTAMP
            WHERE lower(o.source) NOT LIKE 'jobdiva%%'
              AND EXISTS (
                  SELECT 1
                  FROM sourced_candidates AS t
                  WHERE {_TWIN_OF_ORIGIN}
              ){_audit_job_filter_sql(job_ids, "o")}
            """,
            list(_APPLICANT_TWIN_FOLD_KEYS) + job_params,
        )
        origins_updated = cur.rowcount
        cur.execute(
            f"""
            DELETE FROM sourced_candidates AS t
            USING sourced_candidates AS o
            WHERE {_TWIN_OF_ORIGIN}
              AND lower(o.source) NOT LIKE 'jobdiva%%'{_audit_job_filter_sql(job_ids, "t")}
            """,
            job_params,
        )
        twins_deleted = cur.rowcount
        conn.commit()
        cur.close()
    finally:
        conn.close()
    logger.info(
        "applicant origin audit repair: job=%s origins_updated=%s twins_deleted=%s by=%s",
        request.job_id or "ALL", origins_updated, twins_deleted, user.email,
    )
    return {
        "success": True, "job_id": request.job_id or None, "dry_run": False,
        "origins_updated": origins_updated, "twins_deleted": twins_deleted,
    }
