"""Engagement package: payloads."""

import asyncio
from fastapi import HTTPException
from typing import Optional, List, Any, Dict, Tuple
import psycopg2.extras
import json
import re
from routers._helpers import (
    get_article_for_title,
    is_legacy_default_bot_intro,
    normalize_bot_intro_tokens,
)
from services.gender_logic import normalize_gender_prediction, infer_gender_from_name_ai
from utils.email_utils import is_placeholder_email
from core import (
    DEFAULT_SCREENING_LEVEL,
)
from utils.email_utils import _EMAIL_RE

from .common import _log_generate_payload_response_to_newrelic, logger
from .models import GeneratePayloadRequest
from . import common


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


_LIVE_SOURCED_ROWS_SQL = """
    SELECT DISTINCT ON (candidate_id)
        candidate_id, name, email, phone, headline, location, data, resume_match_percentage
    FROM sourced_candidates
    WHERE candidate_id = ANY(%s)
      AND dnc_stopped_at IS NULL
    ORDER BY candidate_id, updated_at DESC
"""


def _fetch_live_sourced_rows(cur, candidate_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Newest non-DNC sourced_candidates row per id, in one round-trip.

    `resume_match_percentage` is guaranteed by
    migrations/20261019_sourced_candidates_resume_match_percentage.sql.
    """
    ids = [c for c in dict.fromkeys(candidate_ids) if c]
    if not ids:
        return {}
    cur.execute(_LIVE_SOURCED_ROWS_SQL, (ids,))
    rows = cur.fetchall()
    return {r["candidate_id"]: r for r in rows}


async def _generate_payload_for(request: GeneratePayloadRequest):
    """
    Generate an interview payload for a candidate.
    Fetches candidate data from sourced_candidates and job data from monitored_jobs,
    then assembles it into the samplepayload.json structure.
    """
    try:
        conn = common._get_db_connection()
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
